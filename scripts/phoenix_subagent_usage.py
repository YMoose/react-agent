#!/usr/bin/env python3
"""Query Phoenix (localhost:6006) for subagent/actor trigger + context-bandwidth analytics.

Fetches all spans of a project via the GraphQL API (or reads the Phoenix SQLite
file directly), classifies them by kind (llm / tool / agent turn), and reports:

Trigger analytics (original behaviour, unchanged):
- per-session stats: agent role, LLM turns, tool-call distribution
- every ``subagent`` tool call with its target agent, prompt preview and
  resume sessionID
- sessions whose ``opencode.agent`` marks them as spawned child sessions
  (e.g. ``actor``)
- trigger rate: parent sessions that dispatched at least one subagent

Context-bandwidth economics (new):
- Metric 1 NCR: noise compression ratio per dispatch, in chars and in exact
  tokens (Actor context absorbed / report tokens injected into Reasoner),
  plus compounded prefill savings Saved_k = N_rem_ep,k * dT_noise,k with
  dT_noise,k = dT_work,k - T_report,k (spec §4.2.1)
- Metric 2: warm session hit rate (sessionID reuse), an overlap-aware
  redundant file-read rate (disjoint pagination vs true re-reads), and the
  outbound self-sufficiency ratios T_obs / T_reload / RAR / DSR per
  dispatch (spec §4.2.4)
- Metric 3: boundary status counts (Completed / Yielded / Blocked / Empty)
  and handoff rework ratio, including task_* control-plane events
  (task_cancel = explicit plan invalidation, task_reply / waiting_input =
  mid-flight escalations, task_status polling probes)
- Metric 4: per-session token & latency bill (context peak, cache_read %,
  visible vs reasoning output), the Handoff Essay Friction Share, and the
  decode-priced net savings NetSaved_k = Saved_k - beta * (T_dispatch_k +
  T_report_k) with amortization horizon N*_rem,k (beta = c_out / c_in,
  spec §4.2.3)
- Per-dispatch Reasoner<->Actor boundary inspector with economic verdicts
  (HIGH_GAIN / MODERATE_GAIN / MARGINAL_OVERHEAD / HANDOFF_RETRY /
  FAILED_DISPATCH)

Sync and async delegation are both supported. Async launches
(``subagent(background=true)`` or any result matching omos'
``The subagent is working in the background (sessionID: …)`` /
``<task id="…" state="running">`` patterns) are detected from the tool
result — the ``background`` parameter alone is unreliable. For async the
child execution window runs from launch to the report delivery point
(native ``<subagent state=…>`` notification in a later parent request, a
``task_status(result=true)`` pull, the next re-dispatch, or the child's
last span), and Δt_parallel = Σ child_exec − merged window union.

Native-notification delivery is resolved from the parent's untruncated wire
JSONL: inside serialized request bodies the wrapper quotes are JSON-escaped
(a plain-quote regex never matches), and the wrapper persists in every later
request, so delivery = the first post-launch frame whose parsed-and-deduped
occurrence count for the child exceeds the count at launch (a warm re-dispatch
of the same child adds a second wrapper). Compaction drops older wrappers
from the parent context mid-session, so the per-child occurrence counter can
go DOWN between adjacent frames; the claim ratchet resets to the visible
count at that point instead of chasing an unreachable index. A warm-resume
boundary whose child emitted one shared terminal wrapper (merged report)
that an earlier boundary already claimed resolves to ``shared_wrapper``
instead of an undeliverable ``none``. The same wire log rescues completed
dispatches whose parent ``execute_tool subagent`` TOOL span was dropped by
the exporter (they would otherwise be miscounted as orphan waste), alongside
the self-acquired context growth ΔPeak_self = Peak − in(R_1) that separates
sessions born with a huge first prompt from self-bloated ones.

All formulas are grounded in OTel GenAI semconv attributes
(``gen_ai.usage.*``, ``gen_ai.tool.call.*``, ``gen_ai.output.messages``
reasoning parts) plus the OpenInference/OpenCode bridge attributes
(``input.value``, ``input.value_original_length``, ``output.value``,
``opencode.wire.seq``, ``opencode.session.parent_id``; legacy
``opencode.reasoning`` is kept only as the flat-mirror fallback of the
native reasoning parts).  When a span body was truncated by the
plugin's 60 KiB attribute budget the script falls back to the untruncated
per-session wire JSONL (``~/.local/share/opencode/raw-requests/``), matching
frames via ``opencode.wire.seq``.

Stdlib only (urllib + sqlite3), no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "http://localhost:6006"
PAGE_SIZE = 200

# Span attribute roles that mark a session as a spawned child session.
CHILD_AGENT_ROLES = {"actor", "worker"}

# Untruncated wire bodies (opencode-genai-obs "wire" sink), one JSONL per session.
DEFAULT_WIRE_DIR = Path.home() / ".local/share/opencode/raw-requests"

# Actor report contract: an explicit "Status:" line wins, <subagent state="…"> fallback.
STATUS_LINE_RE = re.compile(r"Status\s*[:：]\s*[`*]*(Completed|Yielded|Blocked)", re.IGNORECASE)
STATE_TAG_RE = re.compile(r'state="([a-z_]+)"')

# --- async delegation patterns (mirrors oh-my-opencode-slim src/utils/task.ts) ---
# v2 native background launch sentence; the authoritative async marker even when
# the `background` parameter is absent (empirically bg=None launches run async).
NATIVE_BACKGROUND_RE = re.compile(
    r"The subagent is working in the background \(sessionID:\s*([^\s)]+)\)", re.IGNORECASE
)
# v1 plugin running-placeholder (byte-stable across requests)
RUNNING_TASK_TAG_RE = re.compile(r'<task id="([^"]+)" state="running">')
# host-authored terminal wrapper (sync results AND async completion notifications)
SUBAGENT_WRAPPER_RE = re.compile(
    r'<subagent sessionID="([^"]+)" state="(\w+)"(?:\s+description="([^"]*)")?\s*>\s*'
    r"([\s\S]*?)\s*</subagent>"
)
# Same wrapper as it appears inside JSON-serialized request bodies (wire
# frames / `input.value`), where every content quote is backslash-escaped:
# `<subagent sessionID=\"ses_…\" state=\"completed\">…</subagent>`. The plain
# regex above never matches this form — this variant does, and its report
# group is unescaped via a JSON string round-trip.
SUBAGENT_WRAPPER_ESCAPED_RE = re.compile(
    r'<subagent sessionID=\\"([^\\"]+)\\" state=\\"(\w+)\\"'
    r'(?:\s+description=\\"([^\\"]*)\\")?\s*>\s*'
    r"([\s\S]*?)\s*</subagent>"
)
WAITING_INPUT_RE = re.compile(r"waiting_input:\s*true\s*\((\w+)\s+(\S+)\)")

# omos task-control tools surfaced as TOOL spans by the host event stream
TASK_CONTROL_TOOLS = {
    "task_status",
    "task_reply",
    "task_cancel",
    "task_message",
    "task_revive",
    "wait_for_user",
}

# Economic verdict thresholds on NCR_ctx (fallback: NCR_char when actor side unknown).
VERDICT_HIGH_NCR = 8.0
VERDICT_MODERATE_NCR = 2.0

# Blind Spot D (2026-10-31): child-session saturation waterline τ_rotate. Warm
# reuse is optimal only while the child's peak context stays below it; at or
# above it a forced warm dispatch risks context overflow / crash (ses_e799a9c6
# k=5 died at child Peak(A)=274,198 tok) and the child should be rotated —
# carry a compact anchor summary into a fresh child session.
DEFAULT_CHILD_PEAK_WARN = 120_000
# Blind Spot E: an episode ends at the next `compaction` op in the parent or
# an inter-turn idle gap longer than τ_ep minutes; N_rem^ep bounds the
# compound-savings multiplier to the current episode (raw cross-episode N_rem
# once counted 37 unrelated later turns for k=1).
DEFAULT_EPISODE_GAP_MIN = 30
# Blind Spot F: a child with recent activity whose parent wrote no newer spans
# is "in flight" (parent blocked inside an open sync subagent call whose TOOL
# span is not yet flushed to Phoenix); older uncovered children are orphans
# (parent span dropped by crash/restart).
DEFAULT_IN_FLIGHT_WINDOW_SEC = 600
# Liveness guard: an uncovered child whose last activity lies within this
# window of the trace horizon is still executing — or its covering parent
# `subagent` span has not flushed to Phoenix yet (async launches return
# immediately, so the parent legitimately keeps writing newer spans while
# the child runs). Such children are `in_flight`, never dropped waste.
DEFAULT_CHILD_LIVE_WINDOW_SEC = 300
# Parent inline tools that constitute a post-failure escape streak (C_escape).
INLINE_TOOLS = {"read", "edit", "write", "shell", "bash", "grep", "glob"}

# §4.2.3: output/input price ratio β = c_out / c_in for NetSaved_k / N*_rem,k.
DEFAULT_BETA = 4.0
# §4.2.4 [M2]: MEDIUM_OBSERVE tool results counted into T_obs,k.
OBSERVE_TOOLS = {"read", "grep", "glob"}

INF = float("inf")


# ---------------------------------------------------------------------------
# transport (GraphQL API or direct SQLite read) — unchanged from v1
# ---------------------------------------------------------------------------

def gql(endpoint: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST one GraphQL request and return the ``data`` payload."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def list_projects(endpoint: str) -> list[dict[str, Any]]:
    """Return [{id, name, traceCount}] for every Phoenix project."""
    data = gql(
        endpoint,
        """
        {
          projects {
            edges {
              node { id name traceCount }
            }
          }
        }
        """,
    )
    return [e["node"] for e in data["projects"]["edges"]]


def resolve_project(endpoint: str, name_or_id: str) -> dict[str, Any]:
    """Resolve a project by name or raw/base64 id."""
    for proj in list_projects(endpoint):
        if name_or_id in (proj["name"], proj["id"]):
            return proj
    raise SystemExit(f"project not found: {name_or_id!r}")


SPANS_QUERY = """
query ($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Project {
      spans(first: $first, after: $after) {
        edges { node { id name spanKind startTime endTime attributes } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def fetch_spans(endpoint: str, project_id: str) -> list[dict[str, Any]]:
    """Fetch every span of a project, following cursor pagination."""
    spans: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = gql(
            endpoint,
            SPANS_QUERY,
            {"id": project_id, "first": PAGE_SIZE, "after": after},
        )
        conn = data["node"]["spans"]
        spans.extend(e["node"] for e in conn["edges"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return spans


SQLITE_SPAN_SQL = """
SELECT s.name, s.span_kind, s.start_time, s.end_time, s.attributes
FROM spans s
JOIN traces t ON s.trace_rowid = t.id
JOIN projects p ON t.project_rowid = p.id
WHERE p.name = ?
ORDER BY s.start_time
"""


def fetch_spans_sqlite(db_path: Path, project_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read spans straight from the Phoenix SQLite file (read-only).

    Works while the Phoenix server is down; span_kind arrives as an
    uppercase enum (``TOOL``/``LLM``/``AGENT``) and is normalized to the
    lowercase form the GraphQL API returns.
    """
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        row = con.execute(
            "SELECT id, name FROM projects WHERE name = ?", (project_name,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"project not found in db: {project_name!r}")
        project_id, name = row
        trace_count = con.execute(
            "SELECT count(*) FROM traces WHERE project_rowid = ?", (project_id,)
        ).fetchone()[0]
        spans = [
            {
                "name": name_,
                "spanKind": (kind or "").lower(),
                "startTime": start or "",
                "endTime": end or "",
                "attributes": attrs,
            }
            for name_, kind, start, end, attrs in con.execute(
                SQLITE_SPAN_SQL, (project_name,)
            )
        ]
    return {"id": f"sqlite:{project_id}", "name": name, "traceCount": trace_count}, spans


def parse_attrs(span: dict[str, Any]) -> dict[str, Any]:
    """Span.attributes arrives as a JSON string; parse defensively."""
    raw = span.get("attributes") or "{}"
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def tool_name_of(span: dict[str, Any], attrs: dict[str, Any]) -> str:
    """Best-effort tool name: attributes first, then span-name prefix."""
    for path in (
        ("tool", "name"),
        ("gen_ai", "tool", "name"),
    ):
        node: Any = attrs
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node:
            return node
    name = span.get("name", "")
    if name.startswith("execute_tool "):
        return name[len("execute_tool ") :]
    return name


def session_id_of(attrs: dict[str, Any]) -> str:
    for path in (
        ("session", "id"),
        ("gen_ai", "conversation", "id"),
    ):
        node: Any = attrs
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node:
            return node
    return "unknown"


def offered_tools(attrs: dict[str, Any]) -> list[str] | None:
    """Extract tool names from the raw LLM request payload (first span only)."""
    raw = (attrs.get("input") or {}).get("value")
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    schemas: list[Any] = []
    if isinstance(payload.get("tools"), list):
        for entry in payload["tools"]:
            if isinstance(entry, dict):
                fn = entry.get("function") or entry
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    schemas.append(fn["name"])
                    continue
                for decl in entry.get("functionDeclarations") or []:
                    if isinstance(decl, dict) and isinstance(decl.get("name"), str):
                        schemas.append(decl["name"])
    return schemas or None


@dataclass
class SessionStat:
    """Aggregated view of one opencode session found in the spans."""

    session_id: str
    first_time: str = ""
    last_time: str = ""
    roles: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    llm_turns: int = 0
    tool_counts: Counter[str] = field(default_factory=Counter)
    user_turns: list[str] = field(default_factory=list)
    subagent_calls: list[dict[str, Any]] = field(default_factory=list)
    tools_offered: list[str] | None = None

    def touch(self, time: str) -> None:
        if not self.first_time or time < self.first_time:
            self.first_time = time
        if time > self.last_time:
            self.last_time = time

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "roles": sorted(self.roles),
            "models": sorted(self.models),
            "first_time": self.first_time,
            "last_time": self.last_time,
            "llm_turns": self.llm_turns,
            "tool_calls": dict(sorted(self.tool_counts.items())),
            "subagent_calls": self.subagent_calls,
            "user_turns": self.user_turns,
            "tools_offered": self.tools_offered,
        }


def analyze(spans: list[dict[str, Any]]) -> dict[str, SessionStat]:
    """Group spans by session and aggregate per-session statistics."""
    sessions: dict[str, SessionStat] = {}
    for span in sorted(spans, key=lambda s: s.get("startTime", "")):
        attrs = parse_attrs(span)
        sid = session_id_of(attrs)
        stat = sessions.setdefault(sid, SessionStat(session_id=sid))
        stat.touch(span.get("startTime", ""))
        kind = span.get("spanKind", "")
        name = span.get("name", "")

        if kind == "llm" or name.startswith("chat "):
            stat.llm_turns += 1
            oc = attrs.get("opencode") or {}
            if isinstance(oc.get("agent"), str):
                stat.roles.add(oc["agent"])
            model = (attrs.get("llm") or {}).get("model_name")
            if isinstance(model, str) and model:
                stat.models.add(model)
            if stat.tools_offered is None:
                offered = offered_tools(attrs)
                if offered:
                    stat.tools_offered = offered

        elif kind == "tool" or name.startswith("execute_tool"):
            tname = tool_name_of(span, attrs)
            stat.tool_counts[tname] += 1
            if tname == "subagent":
                params = (attrs.get("tool") or {}).get("parameters") or {}
                stat.subagent_calls.append(
                    {
                        "time": span.get("startTime", ""),
                        "target_agent": params.get("agent"),
                        "description": params.get("description"),
                        "background": params.get("background"),
                        "resume_session_id": params.get("sessionID"),
                        "prompt": (params.get("prompt") or "")[:160],
                    }
                )

        elif kind == "agent":
            label = name.strip()
            if label and label != "(auto)" and len(stat.user_turns) < 8:
                stat.user_turns.append(label[:80])
    return sessions


def render_report(project: dict[str, Any], sessions: dict[str, SessionStat]) -> str:
    """Render the human-readable markdown summary."""
    lines: list[str] = []
    lines.append(f"# Phoenix subagent usage — {project['name']}")
    lines.append("")
    lines.append(f"- project id: `{project['id']}`  traces: {project['traceCount']}")
    lines.append(f"- sessions: {len(sessions)}  (times are UTC)")

    parents = [s for s in sessions.values() if not (s.roles & CHILD_AGENT_ROLES)]
    children = [s for s in sessions.values() if s.roles & CHILD_AGENT_ROLES]
    triggered = [s for s in parents if s.subagent_calls]
    total_subagent = sum(len(s.subagent_calls) for s in sessions.values())
    lines.append(
        f"- parent sessions: {len(parents)}  |  dispatched subagent: "
        f"{len(triggered)}  |  child sessions (actor/worker role): {len(children)}"
    )
    lines.append(f"- total subagent calls: {total_subagent}")
    if parents:
        rate = len(triggered) / len(parents) * 100
        lines.append(f"- trigger rate: **{rate:.0f}%** ({len(triggered)}/{len(parents)})")
    lines.append("")

    lines.append("## Sessions")
    lines.append("")
    lines.append("| session | role | model | llm turns | subagent | other tools | span |")
    lines.append("|---|---|---|---|---|---|---|")
    for stat in sorted(sessions.values(), key=lambda s: s.first_time):
        other = ", ".join(f"{k}×{v}" for k, v in stat.tool_counts.items() if k != "subagent")
        lines.append(
            f"| `{stat.session_id[:20]}…` | {','.join(sorted(stat.roles)) or '-'} "
            f"| {','.join(sorted(stat.models)) or '-'} | {stat.llm_turns} "
            f"| {len(stat.subagent_calls)} | {other or '-'} "
            f"| {stat.first_time[11:19]}–{stat.last_time[11:19]} |"
        )
    lines.append("")

    if total_subagent:
        lines.append("## Subagent call detail")
        lines.append("")
        for stat in sorted(sessions.values(), key=lambda s: s.first_time):
            for call in stat.subagent_calls:
                resume = call["resume_session_id"] or "-"
                lines.append(
                    f"- `{stat.session_id[:16]}…` {call['time'][11:19]} → "
                    f"agent={call['target_agent']} bg={call['background']} "
                    f"resume=`{resume}` — {call['description']}"
                )
        lines.append("")

    untriggered = [s for s in parents if not s.subagent_calls and s.llm_turns > 1]
    if untriggered:
        lines.append("## Parent sessions with zero subagent dispatch")
        lines.append("")
        for stat in sorted(untriggered, key=lambda s: s.first_time):
            first_user = stat.user_turns[0] if stat.user_turns else "(auto)"
            lines.append(
                f"- `{stat.session_id}` ({','.join(sorted(stat.roles)) or '-'}) "
                f"{stat.llm_turns} llm turns — “{first_user}”"
            )
        lines.append("")

    missing_tool = [
        s
        for s in parents
        if s.tools_offered is not None and "subagent" not in s.tools_offered
    ]
    if missing_tool:
        lines.append("> ⚠ subagent tool missing from LLM toolset in: " + ", ".join(
            f"`{s.session_id[:16]}…`" for s in missing_tool
        ))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# context-bandwidth economics (v2)
# ---------------------------------------------------------------------------

def dig(node: Any, *path: str) -> Any:
    """Traverse nested dicts; None on any miss."""
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def tso(value: str) -> datetime | None:
    """Parse GraphQL/wire timestamps and Phoenix's timezone-less SQLite UTC values."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in value else value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        # Phoenix stores UTC in SQLite without an offset; delivery matching also
        # consumes offset-aware wire timestamps and must compare the same clock.
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def usage_of(attrs: dict[str, Any]) -> dict[str, int]:
    """gen_ai.usage.* semconv counters (all optional, default 0)."""
    def u(*path: str) -> int:
        raw = dig(attrs, "gen_ai", "usage", *path)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "input_tokens": u("input_tokens"),
        "output_tokens": u("output_tokens"),
        "reasoning_output_tokens": u("reasoning", "output_tokens"),
        "cache_read_input_tokens": u("cache_read", "input_tokens"),
        "cache_write_input_tokens": u("cache_write", "input_tokens"),
    }


def is_compaction_span(span: dict[str, Any], attrs: dict[str, Any]) -> bool:
    """True for `compaction` operations (spec §1.1: gen_ai.operation.name)."""
    op = dig(attrs, "gen_ai", "operation", "name")
    if isinstance(op, str) and "compact" in op.lower():
        return True
    return "compact" in (span.get("name") or "").lower()


def tool_result_text(attrs: dict[str, Any]) -> str:
    """Tool result: gen_ai.tool.call.result (semconv) with output.value fallback."""
    raw = dig(attrs, "gen_ai", "tool", "call", "result")
    if not isinstance(raw, str):
        raw = dig(attrs, "output", "value")
    return raw if isinstance(raw, str) else ""


def decode_tool_result(raw: str) -> tuple[str, str | None]:
    """Unwrap the JSON-array wrapper opencode uses for tool results.

    Returns (text, wrapper_state). The subagent tool wraps reports as
    ``["<subagent sessionID=… state=…>report</subagent>"]``.
    """
    state: str | None = None
    text = raw
    if raw.startswith("[") or raw.startswith('"'):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
                text = parsed[0]
            elif isinstance(parsed, str):
                text = parsed
        except json.JSONDecodeError:
            pass
    m = STATE_TAG_RE.search(text[:200])
    if m:
        state = m.group(1)
    return text, state


def parse_status(report: str) -> str:
    """Boundary status: explicit 'Status:' line wins, <subagent state=…> fallback."""
    m = STATUS_LINE_RE.search(report)
    if m:
        label = m.group(1).lower()
        return "yielded" if label == "yielded" else label
    _, state = decode_tool_result(report)
    if state in ("completed", "yielded", "blocked"):
        return state
    return "unknown"


def subagent_wrappers(body: str, child_session: str | None = None) -> list[dict[str, str]]:
    """Ordered ``<subagent sessionID=… state=…>…</subagent>`` hits in a body.

    Handles both plain text (tool results, parsed message content) and
    JSON-serialized request bodies (``input.value`` / wire frames), where the
    wrapper quotes appear as ``\\"`` escapes; escaped hits are unescaped via a
    JSON string round-trip. ``child_session`` filters the hits to one child;
    ``None`` returns every wrapper (any session). Each hit is
    ``{"sid", "state", "description", "report"}`` in document order, so
    ``len(hits)`` is the child's wrapper occurrence count in that request —
    wrappers persist in every later request once delivered.
    """
    hits: list[dict[str, str]] = []
    if not body:
        return hits
    chunks: list[str] = [body]
    parsed_messages = False
    if body.lstrip().startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            parsed_messages = True
            chunks = []
            for msg in payload["messages"]:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if isinstance(part.get("text"), str):
                            chunks.append(part["text"])
                        # Bug J completion (2026-11-03): sync dispatch results
                        # carry their wrapper inside a tool_result part —
                        # ``content: [{"type":"tool_result", content: …}]``
                        # (string or nested text parts). Without digging into
                        # that slot the parser was blind to every sync-result
                        # wrapper, so the parsed occurrence counts under-
                        # counted (and Fix A's consume-frame binding could not
                        # see the sync delivery frame at all).
                        if part.get("type") == "tool_result":
                            inner = part.get("content")
                            if isinstance(inner, str):
                                chunks.append(inner)
                            elif isinstance(inner, list):
                                for sub in inner:
                                    if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                        chunks.append(sub["text"])
    for chunk in chunks:
        for m in SUBAGENT_WRAPPER_RE.finditer(chunk):
            hits.append(
                {
                    "sid": m.group(1),
                    "state": m.group(2),
                    "description": m.group(3) or "",
                    "report": m.group(4),
                }
            )
    if not parsed_messages:
        # raw body that did not parse into messages (truncated span bodies,
        # non-request JSON): also scan the escaped form directly. Never both:
        # a body that DID parse already yields plain-quote hits from the
        # parsed chunks, and scanning the escaped raw form too would
        # double-count every tool_result-nested wrapper.
        for m in SUBAGENT_WRAPPER_ESCAPED_RE.finditer(body):
            try:
                report = json.loads('"' + m.group(4) + '"')
            except json.JSONDecodeError:
                report = m.group(4)
            hits.append(
                {
                    "sid": m.group(1),
                    "state": m.group(2),
                    "description": m.group(3) or "",
                    "report": report,
                }
            )
    if child_session:
        hits = [h for h in hits if h["sid"] == child_session]
    return hits


def wrapper_identity(hit: dict[str, str]) -> tuple[str, str, str, str]:
    """Stable identity of one wrapper occurrence (Bug I/J hardening).

    The same terminal wrapper can appear more than once inside a single wire
    frame — an escaped ``\\"``-quoted tool_result copy plus a plain-quote
    re-serialized copy (compaction frames), or a truncation-recovered echo.
    Identity = (sid, state, description, whitespace-normalized report), so
    duplicate copies collapse to one occurrence while genuinely distinct
    wrappers (different reports, even with the same description) stay apart.
    """
    return (
        hit["sid"],
        hit["state"],
        hit["description"] or "",
        " ".join((hit["report"] or "").split()),
    )


def frame_wrapper_scan(wire_records: list[dict[str, Any]]) -> list[list[dict[str, str]]]:
    """Per-frame deduplicated wrapper occurrence lists for one parent.

    One pass over the parent's wire records (record order): every frame is
    parsed once by ``subagent_wrappers`` and the hits are deduplicated by
    ``wrapper_identity``. The parsed, closed ``</subagent>`` hit list is the
    ONLY claim currency (Bug J): raw open-tag ``str.count`` also counts
    truncated remnants, which silently offset baselines and skip frames that
    already contain the target child's wrapper.
    """
    out: list[list[dict[str, str]]] = []
    for wr in wire_records:
        seen: set[tuple[str, str, str, str]] = set()
        dedup: list[dict[str, str]] = []
        for hit in subagent_wrappers(wr["frame"], None):
            key = wrapper_identity(hit)
            if key in seen:
                continue
            seen.add(key)
            dedup.append(hit)
        out.append(dedup)
    return out


def frame_kill_notice(frame: str, needle: str) -> bool:
    """True when the kill notice sits in a tool-result slot the host emitted.

    Fix B hardening (Bug H revision, 2026-11-03): a bare substring scan of the
    whole frame also matched plain-text discussions (an eval session quoted
    the ``Tool execution interrupted`` notice string 125+ times in its own
    reasoning) and the byte-identical tool_result a user interrupt produces.
    Only ``messages[*].content[*].type == "tool_result"`` parts with
    ``is_error == true`` and the needle inside count as kill notices.

    Bug H follow-up (2026-11-03): OpenAI-style frames carry tool results as
    content-part lists, but Anthropic-style frames carry them as
    ``{"role": "tool", "content": "<json-string>"}`` — the structured-only
    gate missed those, so host-killed children stayed
    ``orphan_aborted_or_dropped`` (ses_e7063a7… → ses_e706375e…, whose wire
    content is the JSON envelope
    ``{"error":{"type":"aborted","message":"Tool execution interrupted
    (sessionID: …)"},"content":[]}``). A string content counts ONLY when it
    parses to a JSON object carrying a non-empty ``error`` object with the
    needle inside it — the string twin of ``is_error == true`` + needle.
    Still no bare substring scan: a tool result that merely quotes the
    notice text (log dumps, reports) has no error envelope and must not
    match. The cancelled-wrapper exclusion stays as-is (user interrupts).
    """
    try:
        payload = json.loads(frame)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return False
    for msg in payload["messages"]:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                if part.get("is_error") is not True:
                    continue
                try:
                    body = json.dumps(part, ensure_ascii=False)
                except (TypeError, ValueError):
                    continue
                if needle in body:
                    return True
        elif msg.get("role") == "tool" and isinstance(content, str):
            # Anthropic-style string tool result: require a JSON error
            # envelope (the host's own error semantics), never the bare text
            try:
                inner = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(inner, dict) or not isinstance(inner.get("error"), dict):
                continue
            try:
                err_body = json.dumps(inner["error"], ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            if needle in err_body:
                return True
    return False


def child_has_cancelled_wrapper(
    wire_records: list[dict[str, Any]], child_session: str
) -> bool:
    """True when the parent's wire log carries a ``state="cancelled"`` wrapper.

    A user interrupt of a running child produces a byte-identical error
    tool_result to a host-restart kill (no reason code), but the host also
    emits a cancelled terminal wrapper for the interrupted child. Such
    children are NOT booked as ``killed_by_host_restart``.
    """
    for wr in wire_records:
        for hit in subagent_wrappers(wr["frame"], child_session):
            if hit["state"] == "cancelled":
                return True
    return False


def normalized_desc(text: str | None) -> str:
    """Casefolded, punctuation-stripped, whitespace-flattened description."""
    flat = " ".join((text or "").lower().split())
    return re.sub(r"[^\w\s]", "", flat)[:40]


def classify_launch(result_text: str) -> tuple[str, str | None, str | None]:
    """(mode, child_sessionID_from_output, protocol_state) from the tool result.

    Async launches are detected from omos' actual output patterns — NOT from the
    ``background`` parameter, which is empirically absent on async launches too.
    Patterns mirror ``oh-my-opencode-slim/src/utils/task.ts``: the v2 native
    background sentence and the v1 byte-stable running placeholder.

    Anchoring (Bug A fix, 2026-10-31): a ``<subagent sessionID=… state=…>``
    wrapper at position 0 is the authoritative *terminal* marker of a sync
    dispatch.  Quoted examples of async patterns deeper inside the report body
    (e.g. an actor's ``task_*`` scouting report literally documenting
    ``<task id="…" state="running">``) must NOT flip the classification to
    async — so the async patterns are matched only at the start of the result,
    where a real launch handle lives, and the outer wrapper is checked first.
    """
    text = result_text.lstrip()
    # outer terminal wrapper at position 0 wins outright
    m = SUBAGENT_WRAPPER_RE.match(text)
    if m:
        return "sync", m.group(1), m.group(2)
    if text.startswith("<subagent"):
        # wrapper present but truncated/malformed — still a terminal sync result
        _, state = decode_tool_result(text)
        return "sync", None, state
    # async launch handles occupy the start of the result text
    m = NATIVE_BACKGROUND_RE.match(text)
    if m:
        return "async", m.group(1), "running"
    m = RUNNING_TASK_TAG_RE.match(text)
    if m:
        return "async", m.group(1), "running"
    # terminal sync wrapper anywhere else in the text (unusual prefix)
    m = SUBAGENT_WRAPPER_RE.search(text)
    if m:
        return "sync", m.group(1), m.group(2)
    return "sync", None, None


def task_status_state(result_text: str) -> str | None:
    """Protocol state line from a task_status render (``state: <word>``)."""
    m = re.search(r"^state:\s*(\w+)", result_text, re.MULTILINE)
    return m.group(1) if m else None


def reasoning_text_of(attrs: dict[str, Any]) -> str:
    """Chain-of-thought text for one LLM span.

    Native source first: ``gen_ai.output.messages`` follows the GenAI semconv
    ChatMessage schema, where thinking content is a standard part type —
    ``[{"role":"assistant","parts":[{"type":"reasoning","content":…}, …]}]``
    (reasoning parts precede text parts; the exporter never fabricates an
    interleaving). Falls back to the legacy ``opencode.reasoning`` flat mirror
    for older traces — and whenever the native JSON string was truncated past
    parseability by the 60 KiB attribute budget.
    """
    raw = dig(attrs, "gen_ai", "output", "messages")
    if isinstance(raw, str) and raw:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            messages = None
        if isinstance(messages, list):
            chunks = [
                part["content"]
                for msg in messages
                if isinstance(msg, dict)
                for part in (msg.get("parts") or [])
                if isinstance(part, dict)
                and part.get("type") == "reasoning"
                and isinstance(part.get("content"), str)
            ]
            if chunks:
                return "\n\n".join(chunks)
    legacy = attrs.get("opencode") or {}
    value = legacy.get("reasoning") if isinstance(legacy, dict) else None
    return value if isinstance(value, str) else ""


def snippet(text: str, limit: int) -> str:
    """Single-line reasoning snippet, markdown-table safe."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def load_wire_records(wire_dir: Path | None, session_id: str) -> list[dict[str, Any]]:
    """Chronological wire records for one session: ``[{seq, ts, ts_dt, frame}]``.

    One record per untruncated request frame from the plugin's JSONL sink.
    Records are ordered by their own ``ts`` — NOT by ``seq``: the JSONL seq
    counter resets across host restarts (and is a different namespace than
    the spans' ``opencode.wire.seq`` attribute), so file order + ``ts`` is
    the only reliable chronology.
    """
    if not wire_dir:
        return []
    path = wire_dir / f"{session_id}.jsonl"
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                frame = rec.get("frame")
                if not isinstance(frame, str):
                    continue
                ts = rec.get("ts")
                records.append(
                    {
                        "seq": rec.get("seq"),
                        "ts": ts if isinstance(ts, str) else "",
                        "ts_dt": tso(ts) if isinstance(ts, str) else None,
                        "frame": frame,
                    }
                )
    except OSError:
        return []
    records.sort(key=lambda r: r["ts"])
    return records


def wire_body(attrs: dict[str, Any], frames: dict[str, str]) -> tuple[str, bool, str]:
    """Full request body for an LLM span, preferring the untruncated JSONL frame.

    Returns (body, was_truncated, source). The plugin caps attributes at
    ``tracing.contentMaxLength`` (default 61440 UTF-8 bytes) and marks cuts
    with ``input.value_original_length``; the wire JSONL keeps whole frames.
    """
    raw = dig(attrs, "input", "value")
    original = dig(attrs, "input", "value_original_length")
    seq = dig(attrs, "opencode", "wire", "seq")
    if original is not None and frames:
        frame = frames.get(str(seq))
        if frame is not None:
            return frame, True, f"wire.seq={seq}"
    if isinstance(raw, str):
        return raw, original is not None, "span"
    return "", False, "-"


@dataclass
class ReadCall:
    """One ``read`` tool call with its requested line range."""

    ts: str
    path: str
    offset: int | None
    limit: int | None
    out_chars: int


def read_interval(rc: ReadCall) -> tuple[float, float]:
    """Half-open [start, end) line interval of a ``read`` (whole-file ⇒ huge end)."""
    start = float((rc.offset or 1) - 1)
    return start, start + (rc.limit if rc.limit is not None else 10**9)


def read_call_of(span: dict[str, Any], attrs: dict[str, Any]) -> ReadCall:
    """Build a ReadCall (path + requested line range) from a `read` TOOL span."""
    params = dig(attrs, "tool", "parameters") or {}
    off = params.get("offset")
    lim = params.get("limit")
    return ReadCall(
        ts=span.get("startTime", ""),
        path=str(params.get("path") or "?"),
        offset=int(off)
        if isinstance(off, (int, float, str)) and str(off).lstrip("-").isdigit()
        else None,
        limit=int(lim)
        if isinstance(lim, (int, float, str)) and str(lim).lstrip("-").isdigit()
        else None,
        out_chars=len(tool_result_text(attrs)),
    )


def classify_reads(reads: list[ReadCall]) -> dict[str, int]:
    """Overlap-aware redundancy: disjoint pagination vs true re-reads.

    A call is ``redundant`` when its [offset, offset+limit) range is fully
    covered by earlier reads of the same path (a whole-file read covers
    everything); ``partial`` when it overlaps but extends coverage;
    ``disjoint`` when it touches new lines only.
    """
    covered: dict[str, list[tuple[float, float]]] = defaultdict(list)
    stats = {"total": len(reads), "disjoint": 0, "partial": 0, "redundant": 0, "paths": 0}
    seen_paths: set[str] = set()
    for rc in reads:
        seen_paths.add(rc.path)
        start, end = read_interval(rc)
        prior = covered[rc.path]
        if prior and any(a <= start and end <= b for a, b in prior):
            stats["redundant"] += 1
        elif prior and any(start < b and a < end for a, b in prior):
            stats["partial"] += 1
        else:
            stats["disjoint"] += 1
        prior.append((start, end))
    stats["paths"] = len(seen_paths)
    return stats


@dataclass
class SessionBill:
    """Metric 4 bill for one session (LLM spans only)."""

    session_id: str
    roles: set[str] = field(default_factory=set)
    llm_n: int = 0
    peak_input: int = 0
    first_turn_input: int = 0  # in(R_1): input tokens of the first LLM call
    tot_input: int = 0
    tot_cache_read: int = 0
    tot_output: int = 0
    tot_reasoning: int = 0
    first: str = ""
    last: str = ""
    reads: list[ReadCall] = field(default_factory=list)
    tool_counts: Counter[str] = field(default_factory=Counter)

    @property
    def visible_output(self) -> int:
        return self.tot_output - self.tot_reasoning

    @property
    def delta_peak_self(self) -> int:
        """ΔPeak_self = Peak − in(R_1): context the session grew itself.

        Separates sessions born with a huge first user prompt (e.g. a 200k
        spec — Peak ≈ in(R_1), Δ ≈ 0) from sessions that bloated through
        their own inline tool noise (Peak ≫ in(R_1)).
        """
        return max(self.peak_input - self.first_turn_input, 0)

    @property
    def cache_hit_pct(self) -> float:
        return self.tot_cache_read / self.tot_input * 100 if self.tot_input else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "roles": sorted(self.roles),
            "llm_calls": self.llm_n,
            "peak_input_tokens": self.peak_input,
            "first_turn_input_tokens": self.first_turn_input,
            "delta_peak_self": self.delta_peak_self,
            "total_input_tokens": self.tot_input,
            "total_cache_read_input_tokens": self.tot_cache_read,
            "cache_hit_pct": round(self.cache_hit_pct, 1),
            "total_output_tokens": self.tot_output,
            "total_reasoning_output_tokens": self.tot_reasoning,
            "visible_output_tokens": self.visible_output,
            "reads": classify_reads(self.reads),
            "tool_counts": dict(self.tool_counts),
        }


@dataclass
class DispatchBoundary:
    """Everything measurable at one Reasoner→Actor→Reasoner boundary k."""

    k: int
    parent_session: str
    child_session: str | None = None
    start: str = ""
    end: str = ""
    latency_s: float = 0.0
    warm: bool = False
    # sync vs async: classified from the tool result, not the background param
    mode: str = "sync"  # sync | async
    launch_latency_s: float = 0.0  # span duration (async: handle return only)
    child_exec_s: float | None = None  # async: real child execution window
    child_start: str = ""
    child_end: str = ""
    report_delivery: str = "sync_tool_result"
    # sync_tool_result | native_notification | task_status_pull | none
    protocol_state: str | None = None  # completed|error|cancelled|running (wrapper/task_status)
    target_agent: str | None = None
    description: str | None = None
    background: Any = None
    # dispatch prompt (Reasoner's tool-call arguments)
    prompt_chars: int = 0
    t_dispatch_exact: int | None = None  # exact tokens measured on the child side
    t_dispatch_est: int = 0
    # Reasoner spans around the boundary
    r_k_in: int | None = None
    r_k_out: int = 0
    r_k_reasoning: int | None = None
    r_k_seq: str | None = None
    r_next_in: int | None = None
    # async: consuming span where the report actually entered the parent context
    r_consume_in: int | None = None
    r_consume_start: str = ""
    pre_snippet: str = ""
    post_snippet: str = ""
    snippet_source: str = ""  # consume span | post-restart span (consume span dropped)
    # Fix A (2026-11-03): True when the first surviving post-dispatch parent
    # LLM span does NOT carry the wrapper-bearing wire frame (a host restart
    # dropped the true consume span) ⇒ exact T_report measured across the
    # restart gap is untrustworthy; the char-based estimate stays effective.
    restart_interleaved: bool = False
    # Bug K (2026-11-03): True when a warm-resume boundary consumed no wrapper
    # of its own because the child's single terminal wrapper (merged report)
    # was already claimed by an earlier boundary of the same parent —
    # delivery="shared_wrapper", verdict SHARED_DELIVERY (informative; the
    # report was absorbed by the earlier boundary, T_report/NCR unknown).
    shared_desc_match: bool = False
    body_source_k: str = ""
    body_truncated_k: bool = False
    # actor execution inside the dispatch window
    actor_llm_n: int = 0
    actor_tool_n: int = 0
    actor_raw_result_chars: int = 0
    actor_absorbed_tokens: int | None = None
    actor_cold_start: bool = False
    # report back
    report_chars: int = 0
    report_state: str | None = None
    status: str = "unknown"
    has_issues_section: bool = False
    t_report_exact: int | None = None  # Δin(R_k+1) − in(R_k) − out_vis(R_k)
    t_report_est: int = 0
    # Bug C fix (2026-10-31): Δin between R_prev and R_consume can absorb host
    # injections (server-restart recovery prompts, <system-update> catalog
    # swaps, dropped-span turn churn).  When exact wildly exceeds the report's
    # own char-based estimate, mark it polluted and fall back to the estimate.
    t_report_polluted: bool = False
    t_report_effective: int = 0  # exact when trustworthy, est otherwise
    # economics
    ncr_char: float | None = None
    ncr_ctx: float | None = None
    n_rem: int = 0  # raw: every parent LLM call after the report (cross-episode)
    n_rem_ep: int = 0  # episode-bounded: stops at next compaction or idle gap > τ_ep
    episode_end_reason: str | None = None  # compaction | idle_gap | session_end
    dt_noise_tokens: int = 0  # raw_tok − report_tok, kept negative when the report exceeds the raw material
    compounded_saved: int = 0  # Saved_k = N_rem^ep × ΔT_noise (episode-bounded; negative when ΔT_noise < 0)
    # §4.2.3 [M4]: decode-priced net savings + amortization horizon (β = c_out/c_in)
    net_saved_tokens: int | None = None
    n_rem_star: float | None = None
    verdict: str = "UNKNOWN"
    # Blind Spot D: session-level peak context of the child (waterline τ_rotate)
    child_peak_input: int | None = None
    # §4.2.4 [M2]: outbound self-sufficiency (T_obs decomposition, RAR / DSR)
    t_obs_tokens: int = 0
    t_reload_tokens: int = 0
    rar: float | None = None
    dsr: float | None = None
    # Blind Spot E: post-failure parent inline escape streak (C_escape)
    inline_escape_ctx_delta: int | None = None
    inline_escape_tools: int = 0
    inline_escape_duration_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "parent_session": self.parent_session,
            "child_session": self.child_session,
            "start": self.start,
            "end": self.end,
            "latency_s": round(self.latency_s, 1),
            "mode": self.mode,
            "launch_latency_s": round(self.launch_latency_s, 1),
            "child_exec_s": round(self.child_exec_s, 1) if self.child_exec_s else None,
            "report_delivery": self.report_delivery,
            "protocol_state": self.protocol_state,
            "warm": self.warm,
            "target_agent": self.target_agent,
            "description": self.description,
            "prompt_chars": self.prompt_chars,
            "t_dispatch_exact": self.t_dispatch_exact,
            "t_dispatch_est": self.t_dispatch_est,
            "r_k_in": self.r_k_in,
            "r_k_out": self.r_k_out,
            "r_k_reasoning": self.r_k_reasoning,
            "r_k_seq": self.r_k_seq,
            "r_next_in": self.r_next_in,
            "r_consume_in": self.r_consume_in,
            "r_consume_start": self.r_consume_start,
            "actor_llm_n": self.actor_llm_n,
            "actor_tool_n": self.actor_tool_n,
            "actor_raw_result_chars": self.actor_raw_result_chars,
            "actor_absorbed_tokens": self.actor_absorbed_tokens,
            "actor_cold_start": self.actor_cold_start,
            "report_chars": self.report_chars,
            "status": self.status,
            "report_state": self.report_state,
            "has_issues_section": self.has_issues_section,
            "t_report_exact": self.t_report_exact,
            "t_report_est": self.t_report_est,
            "t_report_polluted": self.t_report_polluted,
            "t_report_effective": self.t_report_effective,
            "restart_interleaved": self.restart_interleaved,
            "shared_desc_match": self.shared_desc_match,
            "snippet_source": self.snippet_source,
            "ncr_char": round(self.ncr_char, 2) if self.ncr_char else None,
            "ncr_ctx": round(self.ncr_ctx, 2) if self.ncr_ctx else None,
            "n_rem": self.n_rem,
            "n_rem_ep": self.n_rem_ep,
            "episode_end_reason": self.episode_end_reason,
            "dt_noise_tokens": self.dt_noise_tokens,
            "compounded_saved": self.compounded_saved,
            "net_saved_tokens": self.net_saved_tokens,
            "n_rem_star": self.n_rem_star,
            "child_peak_input": self.child_peak_input,
            "t_obs_tokens": self.t_obs_tokens,
            "t_reload_tokens": self.t_reload_tokens,
            "rar": self.rar,
            "dsr": self.dsr,
            "inline_escape_ctx_delta": self.inline_escape_ctx_delta,
            "inline_escape_tools": self.inline_escape_tools,
            "inline_escape_duration_s": round(self.inline_escape_duration_s, 1)
            if self.inline_escape_duration_s
            else None,
            "pre_snippet": self.pre_snippet,
            "post_snippet": self.post_snippet,
            "verdict": self.verdict,
        }


def _role_of(attrs: dict[str, Any]) -> str | None:
    """Session's own role. Only ``opencode.agent`` on LLM spans is authoritative:
    ``gen_ai.agent.name`` on TOOL spans is the dispatch *target* role (e.g. the
    parent's ``execute_tool subagent`` span carries agent.name=actor)."""
    oc = attrs.get("opencode") or {}
    if isinstance(oc.get("agent"), str):
        return oc["agent"]
    return None


def _parent_id_of(attrs: dict[str, Any]) -> str | None:
    return dig(attrs, "opencode", "session", "parent_id") or dig(attrs, "session", "parent_id")


def build_economics(
    spans: list[dict[str, Any]],
    wire_dir: Path | None,
    chars_per_token: float,
    snippet_len: int,
    child_peak_warn: int = DEFAULT_CHILD_PEAK_WARN,
    episode_gap_min: int = DEFAULT_EPISODE_GAP_MIN,
    in_flight_window_sec: int = DEFAULT_IN_FLIGHT_WINDOW_SEC,
    child_live_window_sec: int = DEFAULT_CHILD_LIVE_WINDOW_SEC,
    beta: float = DEFAULT_BETA,
) -> dict[str, Any]:
    """Compute the 4-metric scorecard and every dispatch boundary."""
    parsed = []
    for span in spans:
        attrs = parse_attrs(span)
        parsed.append((span, attrs, session_id_of(attrs)))

    by_session: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    child_parent: dict[str, str] = {}
    sessions_roles: dict[str, set[str]] = defaultdict(set)
    for span, attrs, sid in parsed:
        by_session[sid].append((span, attrs))
        role = _role_of(attrs)
        if role:
            sessions_roles[sid].add(role)
        pid = _parent_id_of(attrs)
        if pid:
            child_parent[sid] = pid

    bills: dict[str, SessionBill] = {}
    for sid, items in by_session.items():
        bill = SessionBill(session_id=sid, roles=set(sessions_roles.get(sid, set())))
        items.sort(key=lambda p: p[0].get("startTime", ""))
        for span, attrs in items:
            kind = (span.get("spanKind") or "").lower()
            if kind == "llm" or span.get("name", "").startswith("chat "):
                u = usage_of(attrs)
                bill.llm_n += 1
                if bill.llm_n == 1:
                    bill.first_turn_input = u["input_tokens"]
                bill.peak_input = max(bill.peak_input, u["input_tokens"])
                bill.tot_input += u["input_tokens"]
                bill.tot_cache_read += u["cache_read_input_tokens"]
                bill.tot_output += u["output_tokens"]
                bill.tot_reasoning += u["reasoning_output_tokens"]
                if not bill.first:
                    bill.first = span.get("startTime", "")
                bill.last = span.get("endTime", "")
            elif kind == "tool" or span.get("name", "").startswith("execute_tool"):
                tname = tool_name_of(span, attrs)
                bill.tool_counts[tname] += 1
                if tname == "read":
                    bill.reads.append(read_call_of(span, attrs))
        bills[sid] = bill

    # ---- untruncated wire records (one JSONL per session, cached once) ------
    wire_cache: dict[str, list[dict[str, Any]]] = {}

    def wire_records_of(session: str) -> list[dict[str, Any]]:
        if session not in wire_cache:
            wire_cache[session] = load_wire_records(wire_dir, session)
        return wire_cache[session]

    # ---- task_* control-plane events (polls, pulls, escalations, cancels) ----
    task_events: dict[str, dict[str, Any]] = {}
    for sid, items in by_session.items():
        events: dict[str, Any] = {
            "polls": 0,
            "poll_result_chars": 0,
            "diagnostic_reads": 0,
            "pulls": [],  # task_status(result=true): async report injection points
            "escalations": [],  # waiting_input / task_reply: mid-flight yields
            "cancels": [],  # task_cancel: explicit plan invalidation
            "steers": 0,  # task_message
            "revives": 0,  # task_revive
            "waits": 0,  # wait_for_user
        }
        for span, attrs in items:
            kind = (span.get("spanKind") or "").lower()
            if kind != "tool" and not span.get("name", "").startswith("execute_tool"):
                continue
            tname = tool_name_of(span, attrs)
            if tname not in TASK_CONTROL_TOOLS:
                continue
            params = dig(attrs, "tool", "parameters") or {}
            result_text, _ = decode_tool_result(tool_result_text(attrs))
            target = params.get("sessionID") or params.get("task_id") or params.get("taskID")
            ts = span.get("startTime", "")
            if tname == "task_status":
                if params.get("diagnostic") is True or str(params.get("diagnostic")).lower() == "true":
                    events["diagnostic_reads"] += 1
                elif (
                    (params.get("result") is True or str(params.get("result")).lower() == "true")
                    and task_status_state(result_text) != "running"
                ):
                    events["pulls"].append(
                        {"time": ts, "target": target, "result_chars": len(result_text), "result": result_text}
                    )
                else:
                    # Asking for a result does not deliver one when the host
                    # explicitly returns a running placeholder.
                    events["polls"] += 1
                    events["poll_result_chars"] += len(result_text)
                m = WAITING_INPUT_RE.search(result_text)
                if m:
                    events["escalations"].append(
                        {"time": ts, "kind": "waiting_input", "wait_kind": m.group(1), "request_id": m.group(2), "target": target}
                    )
            elif tname == "task_reply":
                events["escalations"].append(
                    {
                        "time": ts,
                        "kind": "task_reply",
                        "request_id": params.get("request_id"),
                        "reply": params.get("reply") or ("answers" if params.get("answers") else None),
                        "target": target,
                    }
                )
            elif tname == "task_cancel":
                events["cancels"].append(
                    {"time": ts, "target": target, "reason": params.get("reason")}
                )
            elif tname == "task_message":
                events["steers"] += 1
            elif tname == "task_revive":
                events["revives"] += 1
            elif tname == "wait_for_user":
                events["waits"] += 1
        if any(
            events[key]
            for key in ("polls", "diagnostic_reads", "pulls", "escalations", "cancels", "steers", "revives", "waits")
        ):
            task_events[sid] = events

    # ---- dispatch boundaries -------------------------------------------------
    dispatches: list[DispatchBoundary] = []
    for sid, items in by_session.items():
        sub_spans = [
            (span, attrs)
            for span, attrs in items
            if ((span.get("spanKind") or "").lower() == "tool" or span.get("name", "").startswith("execute_tool"))
            and tool_name_of(span, attrs) == "subagent"
        ]
        if not sub_spans:
            continue
        sub_spans.sort(key=lambda p: p[0].get("startTime", ""))
        parent_llm = sorted(
            [(s, a) for s, a in items if (s.get("spanKind") or "").lower() == "llm"],
            key=lambda p: p[0].get("startTime", ""),
        )
        # Blind Spot E timeline helpers: parent TOOL spans (inline-escape streaks)
        # and `compaction` op timestamps (episode boundaries).
        parent_tools = sorted(
            [
                (s, a)
                for s, a in items
                if (s.get("spanKind") or "").lower() == "tool"
                or s.get("name", "").startswith("execute_tool")
            ],
            key=lambda p: p[0].get("startTime", ""),
        )
        compaction_ts = sorted(
            tso(s.get("startTime", ""))
            for s, a in items
            if tso(s.get("startTime", "")) and is_compaction_span(s, a)
        )
        wire_records = wire_records_of(sid)
        wire_frames = {str(wr["seq"]): wr["frame"] for wr in wire_records}
        # Bug J (2026-11-03): one parsed, deduped wrapper-occurrence list per
        # wire frame — the ONLY claim currency for async delivery resolution.
        # Raw open-tag str.count also counts truncated remnants (an unclosed
        # echo of an earlier wrapper), which offset baselines and made the
        # old code skip frames that already contained the target child's own
        # wrapper (ses_e7edada63 k=4: raw=4 vs parsed=3 — the 4th occurrence
        # WAS k4's, but the frame was skipped because 3 < 4).
        frame_wrappers = frame_wrapper_scan(wire_records)
        frame_child_counts = [Counter(h["sid"] for h in hits) for hits in frame_wrappers]
        # Bug I (2026-11-03): compaction drops wrappers from the parent
        # context, so a child's visible per-frame occurrence count can go
        # DOWN between adjacent frames. Record every drop (ts, new visible
        # count) per child: claims made before a drop must be reset to the
        # count still visible, otherwise required = claimed + 1 chases an
        # occurrence index that no longer exists (ses_e7988dd44 k=21: count
        # 4 → compaction 2 → claim stuck at 4 ⇒ required 5 unreachable ⇒
        # delivery=none for a boundary whose cancelled wrapper WAS there).
        child_drops: dict[str, list[tuple[datetime | None, int]]] = defaultdict(list)
        prev_child_counts: dict[str, int] = {}
        for wr, counts in zip(wire_records, frame_child_counts):
            for child in set(prev_child_counts) | set(counts):
                cnt = counts.get(child, 0)
                prev_cnt = prev_child_counts.get(child, 0)
                if cnt < prev_cnt:
                    child_drops[child].append((wr["ts_dt"], cnt))
                prev_child_counts[child] = cnt
        # task_status(result=true) pulls indexed by target session for this parent
        pulls_by_child: dict[str, list[dict[str, Any]]] = {}
        for evt in task_events.get(sid, {}).get("pulls", []):
            if evt["target"]:
                pulls_by_child.setdefault(evt["target"], []).append(evt)
        # Bug 1 (2026-11-01): highest wrapper occurrence index already claimed
        # per child by an earlier dispatch of this parent — a warm re-dispatch
        # of the same child must resolve to ITS OWN (later) wrapper.
        # Bug I (2026-11-03): claims made before a compaction drop are reset
        # to the still-visible count (see child_drops above), so the ratchet
        # stays meaningful after the parent context was compacted.
        wire_claimed: dict[str, int] = {}
        # §4.2.4 [M2]: every child session of this parent — used to detect
        # cold-spawn cross-child re-reads against prior dispatch windows.
        child_siblings = {c for c, p in child_parent.items() if p == sid}
        for k, (span, attrs) in enumerate(sub_spans, 1):
            rec = DispatchBoundary(k=k, parent_session=sid)
            rec.start = span.get("startTime", "")
            rec.end = span.get("endTime", "")
            rec.latency_s = 0.0
            t0, t1 = tso(rec.start), tso(rec.end)
            if t0 and t1:
                rec.latency_s = (t1 - t0).total_seconds()
            rec.launch_latency_s = rec.latency_s
            params = dig(attrs, "tool", "parameters") or {}
            rec.target_agent = params.get("agent") or params.get("subagent_type")
            rec.description = params.get("description")
            rec.background = params.get("background")
            rec.prompt_chars = len(params.get("prompt") or "")
            rec.t_dispatch_est = int(rec.prompt_chars / chars_per_token)

            # --- sync/async classification from the RESULT, not the param -----
            raw = tool_result_text(attrs)
            result_text, wrapper_state = decode_tool_result(raw)
            rec.mode, out_child, rec.protocol_state = classify_launch(result_text)

            resume = params.get("sessionID") or params.get("task_id")
            rec.warm = bool(resume)
            rec.child_session = out_child or resume

            # cold start: find the child spawned by this parent inside the window
            if not rec.child_session:
                candidates = [
                    c for c, p in child_parent.items() if p == sid and c in by_session
                ]
                for cand in candidates:
                    first_span = by_session[cand][0][0]
                    ft = tso(first_span.get("startTime", ""))
                    if ft and t0 and t1 and t0 <= ft <= t1:
                        rec.child_session = cand
                        break

            # --- async completion resolution ----------------------------------
            terminal_t: datetime | None = None

            def link_consume(rdt: datetime) -> None:
                """Attach the parent LLM span that sent the wrapper-bearing
                request (logged at ``rdt``) and measure exact T_report =
                Δin(R_consume) − in(R_prev) − out_vis(R_prev).

                The frame ``ts`` marks request-send time, so the sending span
                is the last parent LLM span that STARTED at/before it. When
                Phoenix dropped that span, exact measurement is skipped and
                the char-based estimate remains authoritative."""
                nonlocal terminal_t
                for idx in range(len(parent_llm) - 1, -1, -1):
                    llm_span, llm_attrs = parent_llm[idx]
                    s0 = tso(llm_span.get("startTime", ""))
                    if s0 is None or s0 > rdt + timedelta(seconds=1):
                        continue
                    if idx > 0:
                        prev_u = usage_of(parent_llm[idx - 1][1])
                        cur_u = usage_of(llm_attrs)
                        prev_out_vis = max(
                            prev_u["output_tokens"]
                            - prev_u["reasoning_output_tokens"],
                            0,
                        )
                        exact = (
                            cur_u["input_tokens"]
                            - prev_u["input_tokens"]
                            - prev_out_vis
                        )
                        rec.t_report_exact = exact if exact >= 0 else None
                        rec.r_consume_in = cur_u["input_tokens"]
                        rec.r_consume_start = llm_span.get("startTime", "")
                    break

            if rec.mode == "async":
                rec.report_delivery = "none"
                # (a) native completion notification in a later parent request.
                # Phoenix's input.value truncates at 60 KiB and the wrapper
                # quotes are JSON-escaped inside serialized request bodies, so
                # the plain-regex scan of span bodies almost never hits. Scan
                # the FULL wire record list instead, in file/``ts`` order (the
                # JSONL seq counter resets across host restarts and is not the
                # spans' ``opencode.wire.seq`` namespace). The wrapper persists
                # in every later request, so delivery = the first post-launch
                # frame whose occurrence count for this child EXCEEDS the
                # count at launch (a warm re-dispatch adds a 2nd wrapper).
                if rec.child_session:
                    child = rec.child_session

                    def wr_count(idx: int) -> int:
                        # parsed, deduped occurrence count of this child in
                        # wire frame idx — Bug J: never the raw open-tag count
                        return frame_child_counts[idx].get(child, 0)

                    # baseline: parsed count in the last frame at/before launch
                    baseline_idx = -1
                    for idx, wr in enumerate(wire_records):
                        rdt = wr["ts_dt"]
                        if rdt and t0 and rdt <= t0:
                            baseline_idx = idx
                    baseline = wr_count(baseline_idx) if baseline_idx >= 0 else 0
                    # Bug I: apply compaction drops that happened at/before
                    # this launch — reset the stale claim to the still-visible
                    # count (a claim above what any later frame shows refers to
                    # an occurrence index that compaction removed). A claim
                    # above the pre-launch baseline WITHOUT a drop is the
                    # legitimate warm-resume case (the wrapper appeared in a
                    # frame between two dispatches) and must stand, so the
                    # later boundary cannot re-claim the same occurrence.
                    claimed = wire_claimed.get(child, 0)
                    for drop_ts, new_cnt in child_drops.get(child, ()):
                        if drop_ts is None or (t0 and drop_ts > t0):
                            continue
                        if claimed > new_cnt:
                            wire_claimed[child] = new_cnt
                            claimed = new_cnt
                    required = max(baseline + 1, claimed + 1)
                    prev_count = baseline
                    for idx, wr in enumerate(wire_records):
                        rdt = wr["ts_dt"]
                        if not rdt or not t0 or rdt <= t0:
                            continue
                        cnt = wr_count(idx)
                        if cnt < prev_count:
                            # compaction drop mid-window (Bug I): reset the
                            # ratchet to the visible count
                            required = min(required, cnt + 1)
                            if wire_claimed.get(child, 0) > cnt:
                                wire_claimed[child] = cnt
                        prev_count = cnt
                        if cnt < required:
                            continue
                        hits = [h for h in frame_wrappers[idx] if h["sid"] == child]
                        # trust the parse: cnt == len(hits) by construction —
                        # a frame whose parsed hits reach the required
                        # occurrence ALWAYS delivers (Bug J: the old code
                        # required the RAW count too and skipped frames where
                        # a truncated remnant had inflated it)
                        hit = hits[required - 1]
                        wire_claimed[child] = required
                        rec.report_delivery = "native_notification"
                        rec.protocol_state = hit["state"]
                        report_text = hit["report"].strip()
                        if report_text.startswith("[") or report_text.startswith('"'):
                            report_text, _ = decode_tool_result(report_text)
                        result_text = report_text
                        terminal_t = rdt
                        rec.r_consume_start = wr["ts"]
                        link_consume(rdt)
                        break
                    if rec.report_delivery == "none":
                        # Bug K (2026-11-03): warm-resume shared wrapper. A
                        # single terminal wrapper of the child can cover
                        # several boundaries of the same parent (merged
                        # report — ses_e6422a2e7 k1+k2 "Both checks are
                        # complete", k3+k4): the occurrence index was claimed
                        # by the first boundary, so later boundaries can never
                        # reach required = claimed + 1 and were booked
                        # delivery=none/running/UNKNOWN even though the child
                        # finished and its report WAS delivered into the
                        # parent context. Classify such boundaries
                        # shared_wrapper / SHARED_DELIVERY (informative; the
                        # report was absorbed by the earlier boundary, so
                        # T_report/NCR stay unknown and the ledger does not
                        # change).
                        post_max = 0
                        post_terminal: dict[tuple[str, str, str, str], dict[str, str]] = {}
                        for idx, wr in enumerate(wire_records):
                            rdt = wr["ts_dt"]
                            if not rdt or not t0 or rdt <= t0:
                                continue
                            cnt = wr_count(idx)
                            post_max = max(post_max, cnt)
                            for h in frame_wrappers[idx]:
                                if h["sid"] == child and h["state"] in (
                                    "completed",
                                    "cancelled",
                                ):
                                    post_terminal.setdefault(wrapper_identity(h), h)
                        terminal_hits = list(post_terminal.values())
                        desc_matched = any(
                            rec.description
                            and h["description"]
                            and normalized_desc(h["description"]) == normalized_desc(rec.description)
                            for h in terminal_hits
                        )
                        # every visible occurrence already claimed by an
                        # earlier boundary AND a terminal wrapper exists ⇒
                        # the report was shared, never missing
                        if terminal_hits and claimed >= 1 and post_max <= claimed:
                            rec.report_delivery = "shared_wrapper"
                            rec.shared_desc_match = desc_matched
                            rec.protocol_state = terminal_hits[-1]["state"]
                            terminal_t = None  # window bounded by (c) as before
                else:
                    # child id only resolvable at delivery time: take the
                    # first post-launch wrapper that no earlier dispatch of
                    # this parent claimed, preferring a description match
                    claimed = {
                        d.child_session
                        for d in dispatches
                        if d.parent_session == sid and d.child_session
                    } | set(wire_claimed)
                    for wr in wire_records:
                        rdt = wr["ts_dt"]
                        if not rdt or not t0 or rdt <= t0:
                            continue
                        for hit in subagent_wrappers(wr["frame"], None):
                            if hit["sid"] in claimed:
                                continue
                            if (
                                rec.description
                                and hit["description"]
                                and hit["description"] != rec.description
                            ):
                                continue
                            rec.child_session = hit["sid"]
                            wire_claimed[hit["sid"]] = 1
                            rec.report_delivery = "native_notification"
                            rec.protocol_state = hit["state"]
                            report_text = hit["report"].strip()
                            if report_text.startswith("[") or report_text.startswith('"'):
                                report_text, _ = decode_tool_result(report_text)
                            result_text = report_text
                            terminal_t = rdt
                            rec.r_consume_start = wr["ts"]
                            link_consume(rdt)
                            break
                        if terminal_t is not None:
                            break
                # (b) task_status(result=true) pull for this child
                if (
                    rec.report_delivery == "none"
                    and rec.child_session in pulls_by_child
                ):
                    for pull in sorted(pulls_by_child[rec.child_session], key=lambda e: e["time"]):
                        pt = tso(pull["time"])
                        if pt and t0 and pt > t0:
                            rec.report_delivery = "task_status_pull"
                            result_text = pull.get("result") or result_text
                            st = task_status_state(result_text)
                            # omos contract: result=true retrieves a completed
                            # task's final text and refuses other terminals
                            rec.protocol_state = st or (
                                "completed" if result_text.strip() else "unknown"
                            )
                            for llm_span, llm_attrs in parent_llm:
                                s0, s1 = (
                                    tso(llm_span.get("startTime", "")),
                                    tso(llm_span.get("endTime", "")),
                                )
                                if s0 and s1 and s0 <= pt <= s1:
                                    u = usage_of(llm_attrs)
                                    rec.r_k_in, rec.r_k_out = (
                                        u["input_tokens"],
                                        u["output_tokens"],
                                    )
                                    rec.r_k_reasoning = u["reasoning_output_tokens"]
                                    break
                            for llm_span, llm_attrs in parent_llm:
                                s0 = tso(llm_span.get("startTime", ""))
                                if s0 and pt and s0 > pt:
                                    rec.r_next_in = usage_of(llm_attrs)["input_tokens"]
                                    rec.r_consume_in = rec.r_next_in
                                    rec.r_consume_start = llm_span.get("startTime", "")
                                    if rec.r_k_in is not None:
                                        r_k_out_vis = max(
                                            (rec.r_k_out or 0) - (rec.r_k_reasoning or 0),
                                            0,
                                        )
                                        rec.t_report_exact = max(
                                            rec.r_next_in - rec.r_k_in - r_k_out_vis, 0
                                        )
                                    break
                            terminal_t = pt
                            break
                # (c) no delivery evidence: bound the window by the next
                #     re-dispatch to the same child, else the child's last span
                if terminal_t is None and rec.child_session in by_session:
                    child_spans_sorted = sorted(
                        by_session[rec.child_session], key=lambda p: p[0].get("startTime", "")
                    )
                    for later, _ in sub_spans[k:]:
                        lt = tso(later.get("startTime", ""))
                        lp = dig(_, "tool", "parameters") or {}
                        # same child relaunched later → window ends there
                        later_child = lp.get("sessionID") or lp.get("task_id")
                        if lt and t1 and lt > t1 and later_child == rec.child_session:
                            terminal_t = lt
                            break
                    if terminal_t is None and child_spans_sorted:
                        last_end = tso(child_spans_sorted[-1][0].get("endTime", ""))
                        if last_end:
                            terminal_t = last_end

            # R_k: parent LLM span whose window contains the dispatch start
            if rec.r_k_in is None:
                for llm_span, llm_attrs in parent_llm:
                    s0, s1 = tso(llm_span.get("startTime", "")), tso(llm_span.get("endTime", ""))
                    if s0 and s1 and t0 and s0 <= t0 <= s1:
                        u = usage_of(llm_attrs)
                        rec.r_k_in, rec.r_k_out = u["input_tokens"], u["output_tokens"]
                        rec.r_k_reasoning = u["reasoning_output_tokens"]
                        rec.r_k_seq = str(dig(llm_attrs, "opencode", "wire", "seq") or "") or None
                        rec.pre_snippet = snippet(reasoning_text_of(llm_attrs), snippet_len)
                        body, trunc, src = wire_body(llm_attrs, wire_frames)
                        rec.body_source_k, rec.body_truncated_k = src, trunc
                        break

            # R_k+1: first parent LLM span starting after the dispatch end
            if rec.r_next_in is None:
                bound_span = bound_attrs = None
                for llm_span, llm_attrs in parent_llm:
                    s0 = tso(llm_span.get("startTime", ""))
                    if s0 and t1 and s0 > t1:
                        bound_span, bound_attrs = llm_span, llm_attrs
                        rec.r_next_in = usage_of(llm_attrs)["input_tokens"]
                        break
                if bound_span is not None:
                    # Fix A (2026-11-03): wrapper-frame binding guard. The
                    # first parent LLM span surviving after the dispatch end
                    # is the true consume turn ONLY if the parent wire frame
                    # that actually carries the child wrapper maps onto it.
                    # When a host restart drops the consume span (ses_e68dff2d…
                    # k=1: wrapper frame 12:41:39, next surviving span
                    # 23:44:17), binding silently measures Δin across the
                    # restart gap and absorbs recovery prompts,
                    # <system-update> catalogs and abort notices into "exact"
                    # T_report. Locate the earliest post-dispatch wire frame
                    # with a NEW wrapper occurrence for this child (record ts
                    # order — never seq; quotes are JSON-escaped inside
                    # bodies); its consuming span is the last parent LLM span
                    # starting at/before it (+1s slack, as in link_consume).
                    # Mismatch ⇒ exact untrustworthy: null it, flag
                    # restart_interleaved (the estimate stays effective), and
                    # do not harvest post_snippet from the post-restart span.
                    consume_wr = None
                    if rec.child_session:
                        # Bug J consistency (2026-11-03): the consume-frame
                        # binding guard counts PARSED wrapper occurrences
                        # (frame_child_counts), not raw open-tag str.count —
                        # truncated remnants inflated the raw count and made
                        # the "new occurrence" detection fire on frames that
                        # carried no new closed wrapper.
                        child = rec.child_session

                        def _wr_count(idx: int) -> int:
                            return frame_child_counts[idx].get(child, 0)

                        baseline_idx = -1
                        for idx, wr in enumerate(wire_records):
                            rdt = wr["ts_dt"]
                            if rdt and t1 and rdt <= t1:
                                baseline_idx = idx
                        baseline = _wr_count(baseline_idx) if baseline_idx >= 0 else 0
                        for idx, wr in enumerate(wire_records):
                            rdt = wr["ts_dt"]
                            if not rdt or not t1 or rdt <= t1:
                                continue
                            if _wr_count(idx) > baseline:
                                consume_wr = wr
                                break
                    covering = None
                    if consume_wr is not None:
                        rdt = consume_wr["ts_dt"]
                        for llm_span, llm_attrs in parent_llm:
                            s0 = tso(llm_span.get("startTime", ""))
                            if s0 and rdt and s0 <= rdt + timedelta(seconds=1):
                                covering = llm_span
                    # Async dispatches resolve their consume turn separately
                    # (native-notification scan / task_status pull +
                    # link_consume) and never rely on this binding, so the
                    # guard applies to sync dispatches only.
                    if (
                        rec.mode == "sync"
                        and consume_wr is not None
                        and covering is not bound_span
                    ):
                        rec.r_next_in = None
                        rec.r_consume_in = None
                        rec.r_consume_start = ""
                        rec.restart_interleaved = True
                        rec.snippet_source = "post-restart span (consume span dropped)"
                    else:
                        rec.post_snippet = snippet(
                            reasoning_text_of(bound_attrs), snippet_len
                        )
                        rec.snippet_source = "consume span"
            # N_rem: parent LLM calls after the report actually entered context
            rem_after = terminal_t if (rec.mode == "async" and terminal_t) else t1
            rec.n_rem = sum(
                1
                for llm_span, _ in parent_llm
                if (s0 := tso(llm_span.get("startTime", ""))) and rem_after and s0 > rem_after
            )
            # Blind Spot E: episode-bounded N_rem^ep — stop counting at the next
            # `compaction` op or an inter-turn idle gap > τ_ep (default 30 min).
            # Raw cross-episode N_rem stays on the record for comparison only.
            ep_gap = timedelta(minutes=episode_gap_min)
            horizon = next((c for c in compaction_ts if rem_after and c > rem_after), None)
            rec.n_rem_ep, rec.episode_end_reason = 0, "session_end"
            ep_prev_end: datetime | None = None
            for llm_span, _ in parent_llm:
                s0 = tso(llm_span.get("startTime", ""))
                if not s0:
                    continue
                e0 = tso(llm_span.get("endTime", "")) or s0
                if not rem_after or s0 <= rem_after:
                    ep_prev_end = e0  # carry the pre-report end across the boundary
                    continue
                if horizon and s0 >= horizon:
                    rec.episode_end_reason = "compaction"
                    break
                if ep_prev_end and (s0 - ep_prev_end) > ep_gap:
                    rec.episode_end_reason = "idle_gap"
                    break
                rec.n_rem_ep += 1
                ep_prev_end = e0

            # exact T_report (sync): Δin(R_k+1) − in(R_k) − out_vis(R_k), §4.2.1
            if rec.mode == "sync" and rec.r_next_in is not None and rec.r_k_in is not None:
                r_k_out_vis = max((rec.r_k_out or 0) - (rec.r_k_reasoning or 0), 0)
                exact = rec.r_next_in - rec.r_k_in - r_k_out_vis
                # negative ⇒ compaction/turn-type change between samples: unreliable
                rec.t_report_exact = exact if exact >= 0 else None

            report, state = decode_tool_result(result_text)
            rec.report_state = wrapper_state or state or rec.protocol_state
            if rec.mode == "async" and rec.report_delivery == "none":
                # no delivery evidence: the launch handle is NOT a report —
                # leave T_report unknown rather than fake a huge NCR
                report = ""
                rec.report_state = rec.protocol_state
            if rec.report_delivery == "shared_wrapper":
                # Bug K: the report WAS delivered into the parent context but
                # absorbed by an earlier boundary of this parent — no report
                # of its own to measure, ledger unchanged
                report = ""
                rec.report_state = rec.protocol_state
            rec.report_chars = len(report)
            rec.t_report_est = int(rec.report_chars / chars_per_token)
            # §1.2: the host reports a terminal protocol but the payload is
            # empty ⇒ semantic degraded state "empty"; an async still running
            # simply has no report yet (stays unknown until delivery resolves).
            if report:
                rec.status = parse_status(report)
            elif rec.report_delivery == "shared_wrapper":
                # shared delivery is not a degraded boundary — the report
                # exists, just under an earlier boundary's account
                rec.status = "unknown"
            else:
                rec.status = "empty" if rec.protocol_state != "running" else "unknown"
            rec.has_issues_section = "Issues & Observations" in report or "Issues and Observations" in report

            # actor execution inside the dispatch window
            # sync: [t0, t1] (parent blocked). async: [t0, terminal_t]
            win_hi = terminal_t if (rec.mode == "async" and terminal_t) else t1
            if rec.child_session and rec.child_session in by_session:
                child_items = sorted(
                    by_session[rec.child_session], key=lambda p: p[0].get("startTime", "")
                )
                child_llm = [
                    (s, a)
                    for s, a in child_items
                    if (s.get("spanKind") or "").lower() == "llm" or s.get("name", "").startswith("chat ")
                ]
                in_win = [
                    (s, a)
                    for s, a in child_items
                    if (st := tso(s.get("startTime", ""))) and t0 and win_hi and t0 <= st <= win_hi
                ]
                if in_win:
                    rec.child_start = in_win[0][0].get("startTime", "")
                    rec.child_end = max(
                        (s.get("endTime", "") for s, _ in in_win), default=""
                    )
                    c0, c1 = tso(rec.child_start), tso(rec.child_end)
                    if c0 and c1:
                        rec.child_exec_s = (c1 - c0).total_seconds()
                rec.actor_llm_n = sum(
                    1 for s, _ in in_win if (s.get("spanKind") or "").lower() == "llm"
                )
                tool_win = [
                    (s, a)
                    for s, a in in_win
                    if (s.get("spanKind") or "").lower() == "tool"
                    or s.get("name", "").startswith("execute_tool")
                ]
                rec.actor_tool_n = len(tool_win)
                rec.actor_raw_result_chars = sum(len(tool_result_text(a)) for _, a in tool_win)
                win_llm = [
                    (s, a)
                    for s, a in in_win
                    if (s.get("spanKind") or "").lower() == "llm" or s.get("name", "").startswith("chat ")
                ]
                prev_ctx = 0
                for s, a in child_llm:
                    et = tso(s.get("endTime", ""))
                    if et and t0 and et < t0:
                        u = usage_of(a)
                        prev_ctx = u["input_tokens"] + u["output_tokens"]
                    else:
                        break
                rec.actor_cold_start = rec.warm is False and prev_ctx == 0
                if win_llm:
                    u_last = usage_of(win_llm[-1][1])
                    end_ctx = u_last["input_tokens"] + u_last["output_tokens"]
                    rec.actor_absorbed_tokens = max(end_ctx - prev_ctx, 0)
                    # exact T_dispatch (warm): first child LLM input − prior context
                    u_first = usage_of(win_llm[0][1])
                    if rec.warm and prev_ctx:
                        rec.t_dispatch_exact = max(u_first["input_tokens"] - prev_ctx, 0)

                # §4.2.4 [M2]: outbound self-sufficiency — T_obs decomposition
                # and the re-acquisition tax T_reload. A child `read` counts as
                # reload when its [offset, offset+limit) interval is fully
                # covered by earlier reads of the same path from (a) the parent
                # before launch, (b) prior child dispatches (warm reuse or
                # sibling children), or (c) earlier reads within this turn.
                t_disp_used = (
                    rec.t_dispatch_exact
                    if rec.t_dispatch_exact is not None
                    else rec.t_dispatch_est
                )
                prior_intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
                parent_bill = bills.get(sid)
                if parent_bill is not None:
                    for rc in parent_bill.reads:
                        if rc.ts < rec.start:
                            prior_intervals[rc.path].append(read_interval(rc))
                for cs in child_siblings:
                    if cs in bills:
                        for rc in bills[cs].reads:
                            if rc.ts < rec.start:
                                prior_intervals[rc.path].append(read_interval(rc))
                observe_chars = 0
                reload_chars = 0
                in_turn_intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
                for obs_span, obs_attrs in tool_win:
                    obs_name = tool_name_of(obs_span, obs_attrs)
                    if obs_name not in OBSERVE_TOOLS:
                        continue
                    observe_chars += len(tool_result_text(obs_attrs))
                    if obs_name != "read":
                        continue
                    rc = read_call_of(obs_span, obs_attrs)
                    lo, hi = read_interval(rc)
                    covered_by = prior_intervals.get(rc.path, []) + in_turn_intervals.get(
                        rc.path, []
                    )
                    if covered_by and any(a0 <= lo and hi <= b0 for a0, b0 in covered_by):
                        reload_chars += rc.out_chars
                    in_turn_intervals[rc.path].append((lo, hi))
                rec.t_obs_tokens = round(observe_chars / chars_per_token)
                rec.t_reload_tokens = round(reload_chars / chars_per_token)
                if t_disp_used > 0:
                    rec.rar = round(rec.t_reload_tokens / t_disp_used, 3)
                    rec.dsr = round(1.0 / (1.0 + rec.rar), 3)

            # Bug B fix (2026-10-31): a sync dispatch that returned an empty
            # result AND shows zero child LLM activity in its window is a
            # failed resume/dispatch (e.g. warm session unresumable after
            # context saturation — ses_e799a9c6 k=5, 0s / empty result).
            # §1.2 dual-layer: keep the host's protocol verdict untouched and
            # surface semantic status "empty" (never a silent UNKNOWN) so the
            # parent rotates the session instead of trusting the boundary.
            if rec.mode == "sync" and not report.strip() and rec.actor_llm_n == 0:
                rec.status = "empty"
                rec.verdict = "FAILED_DISPATCH"

            # Bug C fix (2026-10-31): plausibility gate on the exact Δin.
            # Host injections between R_prev and R_consume (restart recovery
            # prompts, <system-update> catalog swaps, dropped-span churn —
            # ses_e7063a72 k=1 gained ~5.4k tok of them) inflate t_report_exact
            # far beyond the report's own char estimate.  When exact exceeds
            # 2× the estimate by more than 1,500 tokens, trust the estimate;
            # keep the raw exact for diagnostic transparency.
            if (
                rec.t_report_exact is not None
                and rec.t_report_exact > 2.0 * rec.t_report_est
                and (rec.t_report_exact - rec.t_report_est) > 1500
            ):
                rec.t_report_polluted = True
                rec.t_report_effective = rec.t_report_est
            else:
                rec.t_report_effective = (
                    rec.t_report_exact if rec.t_report_exact is not None else rec.t_report_est
                )

            # ratios
            if rec.report_chars and rec.actor_raw_result_chars:
                rec.ncr_char = rec.actor_raw_result_chars / rec.report_chars
            report_tok = rec.t_report_effective
            if rec.actor_absorbed_tokens is not None and report_tok:
                rec.ncr_ctx = rec.actor_absorbed_tokens / report_tok
            if report_tok:  # unknown T_report (undelivered async) ⇒ savings unknowable
                raw_tok = int(rec.actor_raw_result_chars / chars_per_token)
                # ΔT_noise is NOT clamped: a report larger than the raw
                # material it filtered is extra content the parent re-carries
                # on every remaining episode turn, so Saved_k must go
                # negative and deepen NetSaved_k (monotone in report size;
                # zero-tool dispatches with raw=0 land at −T_report).
                rec.dt_noise_tokens = raw_tok - report_tok
                # Blind Spot E: N_rem^ep is the authoritative multiplier — raw
                # cross-episode N_rem once counted unrelated turns hours later.
                rec.compounded_saved = rec.n_rem_ep * rec.dt_noise_tokens
                # §4.2.3 [M4]: decode-priced net savings and the minimum
                # amortization horizon N*_rem — dispatching pays off only once
                # the episode's remaining turns cross it (β = c_out / c_in).
                t_disp_used = (
                    rec.t_dispatch_exact
                    if rec.t_dispatch_exact is not None
                    else rec.t_dispatch_est
                )
                boundary_cost = beta * (t_disp_used + report_tok)
                rec.net_saved_tokens = round(rec.compounded_saved - boundary_cost)
                # ΔT_noise ≤ 0 is unamortizable — no N*_rem threshold exists.
                rec.n_rem_star = (
                    round(boundary_cost / rec.dt_noise_tokens, 2)
                    if rec.dt_noise_tokens > 0
                    else None
                )

            score = rec.ncr_ctx if rec.ncr_ctx is not None else rec.ncr_char
            if rec.verdict == "FAILED_DISPATCH":
                pass  # already classified by the Bug B gate above
            elif rec.report_delivery == "shared_wrapper":
                # Bug K: informative verdict — the child finished and its report
                # entered the parent context, but under an earlier boundary's
                # occurrence claim. Not UNKNOWN (nothing is missing), not
                # FAILED (the work happened); T_report/NCR are simply not
                # attributable to this boundary, so it never enters the
                # rework or savings ledgers either.
                rec.verdict = "SHARED_DELIVERY"
            elif score is None:
                rec.verdict = "UNKNOWN"
            elif rec.status in ("yielded", "blocked"):
                rec.verdict = "HANDOFF_RETRY"
            elif score >= VERDICT_HIGH_NCR:
                rec.verdict = "HIGH_GAIN"
            elif score >= VERDICT_MODERATE_NCR:
                rec.verdict = "MODERATE_GAIN"
            else:
                rec.verdict = "MARGINAL_OVERHEAD"

            # Blind Spot E: post-failure parent inline escape (C_escape). After a
            # boundary in the rework set K_rework — protocol error/cancelled,
            # semantic yielded/blocked/empty, or a failed launch — attribute the
            # parent's inline tool streak — up to the next subagent dispatch,
            # the next compaction, or an idle gap > τ_ep — to boundary k.
            if (
                rec.protocol_state in ("error", "cancelled")
                or rec.status in ("yielded", "blocked", "empty")
                or rec.verdict == "FAILED_DISPATCH"
            ):
                esc_candidates: list[datetime] = []
                for later_span, _ in sub_spans[k:]:
                    st = tso(later_span.get("startTime", ""))
                    if st and t1 and st > t1:
                        esc_candidates.append(st)
                esc_candidates.extend(c for c in compaction_ts if t1 and c > t1)
                esc_prev_end: datetime | None = None
                for llm_span, _ in parent_llm:
                    s0 = tso(llm_span.get("startTime", ""))
                    e0 = tso(llm_span.get("endTime", "")) or s0
                    if not s0:
                        continue
                    if t1 and s0 <= t1:
                        esc_prev_end = e0
                        continue
                    if esc_prev_end and (s0 - esc_prev_end) > ep_gap:
                        esc_candidates.append(s0)
                        break
                    esc_prev_end = e0
                esc_end = min(esc_candidates) if esc_candidates else None
                esc_tools = [
                    (s, a)
                    for s, a in parent_tools
                    if (st := tso(s.get("startTime", ""))) and t1 and st > t1
                    and (esc_end is None or st <= esc_end)
                    and tool_name_of(s, a) in INLINE_TOOLS
                ]
                esc_llm = [
                    (s, a)
                    for s, a in parent_llm
                    if (st := tso(s.get("startTime", ""))) and t1 and st > t1
                    and (esc_end is None or st <= esc_end)
                ]
                if esc_tools and esc_llm:
                    rec.inline_escape_tools = len(esc_tools)
                    u_first = usage_of(esc_llm[0][1])
                    u_last = usage_of(esc_llm[-1][1])
                    rec.inline_escape_ctx_delta = max(
                        u_last["input_tokens"] - u_first["input_tokens"], 0
                    )
                    last_act = max(
                        (
                            tso(s.get("endTime", "")) or tso(s.get("startTime", ""))
                            for s, _ in esc_tools + esc_llm
                        ),
                        default=None,
                    )
                    if last_act and t1:
                        rec.inline_escape_duration_s = (last_act - t1).total_seconds()
            dispatches.append(rec)

    dispatches.sort(key=lambda d: (d.parent_session, d.start))

    # ---- Bug L (2026-11-03): cancelled terminal state write-back ------------
    # task_cancel events were collected (polls/pulls/escalations/cancels) but
    # only ever decorated `rework_tokens`; the cancelled child's dispatch kept
    # protocol_state="running" forever when no wrapper ever arrived (user
    # interrupt mid-run — the host drops the child without a terminal wrapper
    # unless the notification path emits a state="cancelled" one). Map each
    # cancel event to the dispatch of that child that was live at cancel time
    # and still shows "running" with no resolved delivery → write "cancelled"
    # (sync-side state, no ledger changes: T_report/NCR stay unknown).
    for sid, ev in task_events.items():
        for cancel_evt in ev.get("cancels", []):
            target = cancel_evt.get("target")
            ct = tso(cancel_evt.get("time") or "")
            if not target or ct is None:
                continue
            candidates = [
                d
                for d in dispatches
                if d.parent_session == sid
                and d.child_session == target
                and d.protocol_state in (None, "running")
                and d.report_delivery in ("none", "sync_tool_result")
                and (st := tso(d.start)) is not None
                and st <= ct
            ]
            if not candidates:
                continue
            live = candidates[-1]  # latest dispatch of this child before the cancel
            live.protocol_state = "cancelled"
            if not live.report_chars:
                # §1.2: terminal protocol + empty payload ⇒ semantic "empty"
                live.status = "empty"

    # ---- Blind Spot D: child saturation waterline (Peak(A) < τ_rotate) -----
    # Peak(A) = the child session's peak input context (same meter as M4's
    # `peak input`). Warm reuse is optimal only below τ_rotate; at/above it a
    # forced warm dispatch risks overflow/crash and the child should rotate
    # (ses_e799a9c6 k=5 died at child Peak(A)=274,198 tok).
    for rec in dispatches:
        if rec.child_session and rec.child_session in bills:
            rec.child_peak_input = bills[rec.child_session].peak_input
    dispatched_children = {d.child_session for d in dispatches if d.child_session}
    child_sessions = sorted(
        set(child_parent)
        | dispatched_children
        | {sid for sid, roles in sessions_roles.items() if roles & CHILD_AGENT_ROLES}
    )
    child_saturation: list[dict[str, Any]] = []
    for sid in child_sessions:
        bill = bills.get(sid)
        if not bill or not bill.llm_n:
            continue
        overloaded = bill.peak_input >= child_peak_warn
        child_saturation.append(
            {
                "session_id": sid,
                "parent_id": child_parent.get(sid),
                "peak_input": bill.peak_input,
                "llm_n": bill.llm_n,
                "overloaded": overloaded,
                "verdict": "CHILD_OVERLOADED (Rotate Session)" if overloaded else "ok",
            }
        )
    child_saturation.sort(key=lambda c: (-c["peak_input"], c["session_id"]))

    # ---- Blind Spot F: orphan / in-flight children --------------------------
    # A child session is *covered* when any parent `subagent` TOOL span exists
    # for it: a dispatch record names it, a subagent span's params name it, or
    # its first span falls inside a parent subagent window (an end-less span is
    # still open in memory — sync return pending). Uncovered children split by
    # recency vs the trace horizon: recent + parent silent ⇒ in-flight; stale ⇒
    # orphan (parent span dropped by crash/restart — ses_e7063a72 →
    # ses_e706375e7 after a restart lost the covering span).
    subagent_windows: list[tuple[str, str | None, datetime, datetime | None]] = []
    for span, attrs, sid in parsed:
        kind = (span.get("spanKind") or "").lower()
        if kind != "tool" and not span.get("name", "").startswith("execute_tool"):
            continue
        if tool_name_of(span, attrs) != "subagent":
            continue
        params = dig(attrs, "tool", "parameters") or {}
        named = params.get("sessionID") or params.get("task_id") or params.get("taskID")
        st = tso(span.get("startTime", ""))
        if st is None:
            continue
        et = tso(span.get("endTime", ""))  # None = span still open (sync call)
        subagent_windows.append((sid, str(named) if named else None, st, et))
    span_ends = [
        tso(s.get("endTime", "")) or tso(s.get("startTime", "")) for s, _, _ in parsed
    ]
    trace_max_end = max((e for e in span_ends if e), default=None)

    orphan_children: list[dict[str, Any]] = []
    for sid in child_sessions:
        pid = child_parent.get(sid)
        items = by_session.get(sid) or []
        if not items:
            continue
        first_start = tso(items[0][0].get("startTime", ""))
        child_ends = [
            tso(s.get("endTime", "")) or tso(s.get("startTime", "")) for s, _ in items
        ]
        last_end = max((e for e in child_ends if e), default=None)
        if not first_start or not last_end:
            continue
        # covered paths (a)–(c)
        if sid in dispatched_children:
            continue
        if any(named == sid for _, named, _, _ in subagent_windows if named):
            continue
        covered = False
        for wparent, _, w0, w1 in subagent_windows:
            if pid is None or wparent != pid:
                continue
            if w0 <= first_start and (w1 is None or first_start <= w1):
                covered = True
                break
        if covered:
            continue
        # Bug 2 (2026-11-01): the exporter sometimes drops the parent
        # `execute_tool subagent` TOOL span while the child completed normally
        # and its `<subagent sessionID=… state=completed">…</subagent>` wrapper
        # IS present in the parent's wire log. Such children are delivered
        # dispatches missing only their Phoenix coverage — never orphan waste.
        wire_covered = False
        if pid:
            esc_open = '<subagent sessionID=\\"%s\\"' % sid
            plain_open = '<subagent sessionID="%s"' % sid
            for wr in wire_records_of(pid):
                if esc_open in wr["frame"] or plain_open in wr["frame"]:
                    wire_covered = True
                    break
        if wire_covered:
            bill = bills.get(sid)
            consumed = (bill.tot_input + bill.tot_output) if bill else 0
            orphan_children.append(
                {
                    "session_id": sid,
                    "parent_id": pid,
                    "classification": "delivered_missing_tool_span",
                    "age_s": None,
                    "first_span": items[0][0].get("startTime", ""),
                    "last_span_end": max(
                        (s.get("endTime", "") for s, _ in items), default=""
                    ),
                    "llm_n": bill.llm_n if bill else 0,
                    "consumed_tokens": consumed,
                }
            )
            continue
        # Fix B (2026-11-03): host-restart kill notice. A parent wire record
        # containing `Tool execution interrupted (sessionID: <sid>` proves the
        # child was aborted by a server/host restart AND that the parent
        # received an explicit kill notice — infra overhead, not agent-
        # attributable dropped work. Book as killed_by_host_restart; the
        # C_rework aggregation only counts orphan_aborted_or_dropped, so
        # these tokens are reported separately instead of as rework waste.
        # Bug H revision (2026-11-03): the notice must sit in a STRUCTURED
        # tool_result slot (messages[*].content[*].type == "tool_result" with
        # is_error true) — a bare substring scan of the frame also matched
        # plain-text discussions that merely quoted the notice (one eval
        # session discussed it 125+ times in reasoning) and user interrupts
        # produce a byte-identical tool_result (no reason code distinguishes
        # them). A cancelled terminal wrapper for the child in the same wire
        # log marks a user interrupt, not a host restart, so such children
        # stay out of killed_by_host_restart (residual ambiguity documented
        # in the casebook, awaiting an upstream reason code).
        if pid:
            kill_needle = "Tool execution interrupted (sessionID: %s" % sid
            kill_ts = ""
            for wr in wire_records_of(pid):
                if frame_kill_notice(wr["frame"], kill_needle):
                    kill_ts = wr["ts"]
                    break
            if kill_ts and not child_has_cancelled_wrapper(
                wire_records_of(pid), sid
            ):
                bill = bills.get(sid)
                orphan_children.append(
                    {
                        "session_id": sid,
                        "parent_id": pid,
                        "classification": "killed_by_host_restart",
                        "age_s": None,
                        "first_span": items[0][0].get("startTime", ""),
                        "last_span_end": max(
                            (s.get("endTime", "") for s, _ in items), default=""
                        ),
                        "llm_n": bill.llm_n if bill else 0,
                        "consumed_tokens": (bill.tot_input + bill.tot_output)
                        if bill
                        else 0,
                        "notice_ts": kill_ts,
                    }
                )
                continue
        # uncovered → classify by recency + parent silence after child start
        # Liveness / in-flight guard: an uncovered child whose latest activity
        # lies within CHILD_LIVE_WINDOW_SEC of the trace horizon is still
        # executing — or its covering parent `subagent` span has not flushed
        # to Phoenix yet (async launches return immediately, so the parent
        # legitimately keeps writing newer spans while the child runs).
        # Classify it `in_flight` and never book it as dropped waste.
        horizon = trace_max_end or datetime.now(timezone.utc)
        age_s = (horizon - last_end).total_seconds()
        live_recent = age_s <= child_live_window_sec
        parent_items = by_session.get(pid) or []
        parent_newer = any(
            (st := tso(s.get("startTime", ""))) and st > first_start for s, _ in parent_items
        )
        in_flight = live_recent or (age_s <= in_flight_window_sec and not parent_newer)
        cls = "in_flight" if live_recent else (
            "in_flight_pending_parent" if in_flight else "orphan_aborted_or_dropped"
        )
        bill = bills.get(sid)
        consumed = (bill.tot_input + bill.tot_output) if bill else 0
        orphan_children.append(
            {
                "session_id": sid,
                "parent_id": pid,
                "classification": cls,
                "age_s": round(age_s, 1) if age_s != INF else None,
                "first_span": items[0][0].get("startTime", ""),
                "last_span_end": max(
                    (s.get("endTime", "") for s, _ in items), default=""
                ),
                "llm_n": bill.llm_n if bill else 0,
                "consumed_tokens": consumed,
            }
        )
    orphan_children.sort(key=lambda o: (o["classification"], o["session_id"]))

    # ---- pair-level rollups --------------------------------------------------
    pairs: dict[str, dict[str, Any]] = {}
    for rec in dispatches:
        entry = pairs.setdefault(
            rec.parent_session,
            {
                "dispatches": 0,
                "warm": 0,
                "cold": 0,
                "t_dispatch_exact": 0,
                "t_dispatch_est": 0,
                "t_dispatch_total": 0,  # exact where measurable, est otherwise
                "t_report_exact": 0,
                "t_report_est": 0,
                "t_report_total": 0,
                "report_chars": 0,
                "raw_chars": 0,
                "absorbed": 0,
                "saved": 0,
                "children": set(),
                "statuses": Counter(),
                "verdicts": Counter(),
                "rework": 0,
                "async": 0,
                "child_windows": [],
                # Blind Spot E: post-failure parent inline escape (C_escape) rollup
                "escape_ctx_delta": 0,
                "escape_tools": 0,
                "escape_duration_s": 0.0,
                "escape_dispatches": 0,
                # Blind Spot F: uncovered children attributed to this parent
                "orphan_children": 0,
                "orphan_waste_tokens": 0,
            },
        )
        entry["dispatches"] += 1
        entry["warm" if rec.warm else "cold"] += 1
        if rec.t_dispatch_exact is not None:
            entry["t_dispatch_exact"] += rec.t_dispatch_exact
            entry["t_dispatch_total"] += rec.t_dispatch_exact
        else:
            entry["t_dispatch_est"] += rec.t_dispatch_est
            entry["t_dispatch_total"] += rec.t_dispatch_est
        if rec.t_report_exact is not None:
            entry["t_report_exact"] += rec.t_report_exact
        if rec.t_report_effective == rec.t_report_est:
            # no exact measurement, or exact was polluted → estimate is effective
            entry["t_report_est"] += rec.t_report_est
        entry["t_report_total"] += rec.t_report_effective
        entry["report_chars"] += rec.report_chars
        entry["raw_chars"] += rec.actor_raw_result_chars
        entry["absorbed"] += rec.actor_absorbed_tokens or 0
        entry["saved"] += rec.compounded_saved
        if rec.child_session:
            entry["children"].add(rec.child_session)
        entry["statuses"][rec.status] += 1
        entry["verdicts"][rec.verdict] += 1
        if rec.status in ("yielded", "blocked", "empty") or rec.verdict in (
            "HANDOFF_RETRY",
            "MARGINAL_OVERHEAD",
            "FAILED_DISPATCH",
        ):
            entry["rework"] += 1
        if rec.mode == "async":
            entry["async"] += 1
            c0, c1 = tso(rec.child_start), tso(rec.child_end)
            if c0 and c1:
                entry["child_windows"].append((c0, c1))
        if rec.inline_escape_ctx_delta is not None:
            entry["escape_ctx_delta"] += rec.inline_escape_ctx_delta
            entry["escape_tools"] += rec.inline_escape_tools
            entry["escape_dispatches"] += 1
        if rec.inline_escape_duration_s is not None:
            entry["escape_duration_s"] += rec.inline_escape_duration_s
    for orphan in orphan_children:
        if orphan["classification"] != "orphan_aborted_or_dropped":
            continue  # in-flight children are pending, not yet waste
        pentry = pairs.get(orphan["parent_id"] or "")
        if pentry is not None:
            pentry["orphan_children"] += 1
            pentry["orphan_waste_tokens"] += orphan["consumed_tokens"]

    # Δt_parallel per parent: serial sum vs merged union of child execution windows
    for entry in pairs.values():
        windows = sorted(entry.get("child_windows") or [])
        serial = sum((c1 - c0).total_seconds() for c0, c1 in windows)
        union_s = 0.0
        if windows:
            cur0, cur1 = windows[0]
            for c0, c1 in windows[1:]:
                if c0 <= cur1:  # overlapping or touching → merge
                    cur1 = max(cur1, c1)
                else:
                    union_s += (cur1 - cur0).total_seconds()
                    cur0, cur1 = c0, c1
            union_s += (cur1 - cur0).total_seconds()
        entry["serial_child_s"] = serial
        entry["union_child_s"] = union_s
        entry["dt_parallel_s"] = max(serial - union_s, 0.0)
        entry["speedup_pct"] = (entry["dt_parallel_s"] / serial * 100) if serial else 0.0

    # explicit rework cost: attribute cancelled children's absorbed tokens
    child_absorbed = {d.child_session: (d.actor_absorbed_tokens or 0) for d in dispatches}
    for sid, events in task_events.items():
        for cancel in events.get("cancels", []):
            cancel["rework_tokens"] = child_absorbed.get(cancel.get("target"))
    for entry in pairs.values():
        entry["children"] = sorted(entry["children"])
        entry["statuses"] = dict(entry["statuses"])
        entry["verdicts"] = dict(entry["verdicts"])
        entry.pop("child_windows", None)  # datetime tuples: render-only, not JSON-safe

    return {
        "bills": bills,
        "dispatches": dispatches,
        "pairs": pairs,
        "task_events": task_events,
        "child_saturation": child_saturation,
        "orphan_children": orphan_children,
        "params": {
            "child_peak_warn": child_peak_warn,
            "episode_gap_min": episode_gap_min,
            "in_flight_window_sec": in_flight_window_sec,
            "child_live_window_sec": child_live_window_sec,
            "beta": beta,
        },
    }


def render_economics(econ: dict[str, Any], chars_per_token: float) -> str:
    """Render the economics scorecard + boundary inspector as markdown."""
    bills: dict[str, SessionBill] = econ["bills"]
    dispatches: list[DispatchBoundary] = econ["dispatches"]
    pairs: dict[str, dict[str, Any]] = econ["pairs"]
    task_events: dict[str, dict[str, Any]] = econ.get("task_events", {})
    params = econ.get("params") or {}
    child_peak_warn = params.get("child_peak_warn", DEFAULT_CHILD_PEAK_WARN)
    beta = params.get("beta", DEFAULT_BETA)
    saturation: list[dict[str, Any]] = econ.get("child_saturation") or []
    overloaded_ids = {c["session_id"] for c in saturation if c.get("overloaded")}
    lines: list[str] = []
    lines.append("## Context-bandwidth economics scorecard")
    lines.append("")
    lines.append(
        f"- token estimates use {chars_per_token:g} chars/token "
        "(`--chars-per-token`; measured ≈2.35 for zh/en-mixed prompts — "
        "exact figures below come from `gen_ai.usage.input_tokens` deltas)"
    )
    lines.append("")

    # Metric 4 bill table
    lines.append("### Metric 4 — token & latency bill (per session)")
    lines.append("")
    lines.append(
        "| session | role | llm | peak input | in(R₁) | Δpeak_self "
        "| tot input | cache read | cache% | out | reasoning | visible |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for bill in sorted(bills.values(), key=lambda b: b.first):
        if not bill.llm_n:
            continue
        lines.append(
            f"| `{bill.session_id[:20]}…` | {','.join(sorted(bill.roles)) or '-'} "
            f"| {bill.llm_n} | {bill.peak_input:,}{' ⚠rotate' if bill.session_id in overloaded_ids else ''} "
            f"| {bill.first_turn_input:,} | +{bill.delta_peak_self:,} "
            f"| {bill.tot_input:,} "
            f"| {bill.tot_cache_read:,} | {bill.cache_hit_pct:.0f}% "
            f"| {bill.tot_output:,} | {bill.tot_reasoning:,} | {bill.visible_output:,} |"
        )
    lines.append("")
    lines.append(
        "> Δpeak_self = Peak − in(R₁): context growth the session acquired itself. "
        "A near-zero Δ on a huge peak means the session was born with a large first "
        "prompt (spec reload) — not bloated by its own inline tool noise."
    )
    lines.append("")

    # Blind Spot D: child saturation waterline (Peak(A) < τ_rotate)
    if saturation:
        lines.append(
            f"### Child saturation waterline — Peak(A) vs τ_rotate = {child_peak_warn:,} tok"
        )
        lines.append("")
        lines.append("| child session | parent | llm | Peak(A) | verdict |")
        lines.append("|---|---|---|---|---|")
        for c in saturation:
            mark = " ⚠" if c["overloaded"] else ""
            parent = f"`{c['parent_id'][:18]}…`" if c.get("parent_id") else "-"
            lines.append(
                f"| `{c['session_id'][:20]}…` | {parent} | {c['llm_n']} "
                f"| {c['peak_input']:,}{mark} | {c['verdict']} |"
            )
        lines.append("")
        lines.append(
            "> Peak(A) = child session's peak input context (dual of M4's parent Peak(R)). "
            "Warm reuse (γ_warm→1) is optimal only below τ_rotate; at/above it rotate the "
            "child — carry a compact anchor summary into a fresh session — or the next "
            "warm dispatch risks overflow/crash (regression: k=5 at Peak(A)=274,198 tok)."
        )
        lines.append("")

    # Metric 1/2/3 pair summary
    if pairs:
        lines.append("### Metrics 1–3 — dispatch pairs")
        lines.append("")
        lines.append(
            "| parent | child(ren) | dispatches | sync/async | warm/cold | γ_warm "
            "| NCR_char | NCR_ctx | statuses | rework/overhead | saved tok-calls |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for pid, entry in sorted(pairs.items()):
            n = entry["dispatches"]
            gw = entry["warm"] / n if n else 0.0
            tot_report = entry["t_report_total"]
            ncr_char = (
                f"{entry['raw_chars'] / entry['report_chars']:.2f}"
                if entry["report_chars"]
                else "-"
            )
            ncr_ctx = f"{entry['absorbed'] / tot_report:.2f}" if tot_report else "-"
            statuses = ", ".join(f"{k}×{v}" for k, v in sorted(entry["statuses"].items()))
            children = ", ".join(f"`{c[:14]}…`" for c in entry["children"]) or "-"
            lines.append(
                f"| `{pid[:18]}…` | {children} | {n} | {n - entry['async']}/{entry['async']} "
                f"| {entry['warm']}/{entry['cold']} "
                f"| {gw * 100:.0f}% | {ncr_char} | {ncr_ctx} "
                f"| {statuses} | {entry['rework']} | {entry['saved']:,} |"
            )
        lines.append("")

        # Handoff Essay Friction Share
        lines.append("### Handoff essay friction share")
        lines.append("")
        for pid, entry in pairs.items():
            parent = bills.get(pid)
            child_visible = sum(
                bills[c].visible_output for c in entry["children"] if c in bills
            )
            if not parent or not parent.visible_output:
                continue
            t_disp = entry["t_dispatch_total"]
            t_rep = entry["t_report_total"]
            p_share = t_disp / parent.visible_output * 100
            c_share = (t_rep / child_visible * 100) if child_visible else 0.0
            combined = (
                (t_disp + t_rep) / (parent.visible_output + child_visible) * 100
                if child_visible
                else p_share
            )
            lines.append(
                f"- `{pid[:18]}…`: T_dispatch≈{t_disp:,} tok = **{p_share:.0f}%** of parent visible; "
                f"T_report≈{t_rep:,} tok = **{c_share:.0f}%** of child visible; "
                f"combined boundary prose = **{combined:.0f}%** of pair visible output"
            )
        lines.append("")

    # Metric 2 redundant reads
    lines.append("### Metric 2 — overlap-aware file-read redundancy")
    lines.append("")
    lines.append("| session | read calls | paths | disjoint | partial | redundant | redundant% |")
    lines.append("|---|---|---|---|---|---|---|")
    for bill in sorted(bills.values(), key=lambda b: b.first):
        st = classify_reads(bill.reads)
        if not st["total"]:
            continue
        pct = st["redundant"] / st["total"] * 100
        lines.append(
            f"| `{bill.session_id[:20]}…` | {st['total']} | {st['paths']} "
            f"| {st['disjoint']} | {st['partial']} | {st['redundant']} | {pct:.0f}% |"
        )
    lines.append("")
    lines.append(
        "> redundant = range fully covered by earlier reads of the same path; "
        "disjoint = pagination touching new lines (not waste)."
    )
    lines.append("")

    # §4.2.4 [M2]: outbound self-sufficiency per dispatch (T_reload / RAR / DSR)
    if any(rec.t_obs_tokens or rec.t_reload_tokens for rec in dispatches):
        lines.append("### Metric 2 — outbound self-sufficiency (T_reload / RAR / DSR)")
        lines.append("")
        lines.append("| k | parent | child | T_dispatch | T_obs | T_reload | RAR | DSR |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for rec in dispatches:
            t_disp_used = (
                rec.t_dispatch_exact
                if rec.t_dispatch_exact is not None
                else rec.t_dispatch_est
            )
            if rec.child_session:
                child = f"`{rec.child_session[:16]}…`"
            else:
                child = "-"
            lines.append(
                f"| {rec.k} | `{rec.parent_session[:16]}…` | {child} "
                f"| {t_disp_used:,} | {rec.t_obs_tokens:,} | {rec.t_reload_tokens:,} "
                f"| {rec.rar if rec.rar is not None else '-'} "
                f"| {rec.dsr if rec.dsr is not None else '-'} |"
            )
        lines.append("")
        lines.append(
            "> T_reload = child `read` results fully covered by earlier reads of the same "
            "path (parent before launch, prior child dispatches, or earlier in-turn — "
            "§4.2.4 sources 1–3); RAR = T_reload/T_dispatch, DSR = 1/(1+RAR). "
            "RAR = 0 ⇒ fully self-sufficient dispatch; RAR ≥ 1 (DSR ≤ 50%) ⇒ inverted "
            "delegation: the child re-acquired known information at ≥ the prompt's cost."
        )
        lines.append("")

    # Boundary inspector
    if dispatches:
        lines.append("## Per-dispatch boundary inspector (Reasoner ↔ Actor)")
        lines.append("")
        lines.append(
            "| k | window | mode | launch | child exec | warm | T_disp (exact/est) "
            "| actor llm/tool | raw chars | Δctx tok | delivery | T_report (exact/est) "
            "| NCR_char | NCR_ctx | N_rem ep/raw | saved | net saved | N*_rem "
            "| protocol | status | verdict |"
        )
        lines.append("|" + "---|" * 21)
        for rec in dispatches:
            t_disp = (
                f"{rec.t_dispatch_exact}/{rec.t_dispatch_est}"
                if rec.t_dispatch_exact is not None
                else f"~{rec.t_dispatch_est}"
            )
            t_rep = (
                f"{rec.t_report_exact}/{rec.t_report_est}"
                if rec.t_report_exact is not None
                else f"~{rec.t_report_est}"
            )
            if rec.t_report_polluted:
                t_rep += "⚠"  # exact Δin polluted by host injections → est effective
            if rec.restart_interleaved:
                t_rep += "⟳"  # consume span dropped across host restart → est effective
            ncr_char = f"{rec.ncr_char:.2f}" if rec.ncr_char is not None else "-"
            ncr_ctx = f"{rec.ncr_ctx:.2f}" if rec.ncr_ctx is not None else "-"
            absorbed = (
                f"{rec.actor_absorbed_tokens:,}" if rec.actor_absorbed_tokens is not None else "-"
            )
            child_exec = f"{rec.child_exec_s:.0f}s" if rec.child_exec_s else "-"
            launch = f"{rec.launch_latency_s:.0f}s"
            delivery = (
                rec.report_delivery.replace("sync_tool_result", "sync")
                .replace("native_notification", "nat-notify")
                .replace("task_status_pull", "pull")
                .replace("shared_wrapper", "shared-wrap")
                .replace("none", "⚠ none")
            )
            net_saved = (
                f"{rec.net_saved_tokens:,}" if rec.net_saved_tokens is not None else "-"
            )
            n_star = str(rec.n_rem_star) if rec.n_rem_star is not None else "-"
            lines.append(
                f"| {rec.k} | {rec.start[11:19]}→{rec.end[11:19]} | {rec.mode} | {launch} "
                f"| {child_exec} | {'warm' if rec.warm else 'COLD'} | {t_disp} "
                f"| {rec.actor_llm_n}/{rec.actor_tool_n} | {rec.actor_raw_result_chars:,} "
                f"| {absorbed} | {delivery} | {t_rep} | {ncr_char} | {ncr_ctx} "
                f"| {rec.n_rem_ep}/{rec.n_rem} | {rec.compounded_saved:,} "
                f"| {net_saved} | {n_star} "
                f"| {rec.protocol_state or '-'} | {rec.status} | {rec.verdict} |"
            )
        lines.append("")

        for rec in dispatches:
            lines.append(
                f"### k={rec.k} — {rec.description or '(no description)'} "
                f"[{rec.verdict}]"
            )
            lines.append("")
            lines.append(
                f"- dispatch: `{rec.start[11:19]}→{rec.end[11:19]}` "
                f"({rec.latency_s:.0f}s, {'warm ' + str(rec.child_session or '')[:22] if rec.warm else 'cold start ' + str(rec.child_session or '?')[:22]})"
            )
            if rec.mode == "async":
                exec_bit = (
                    f"child exec `{rec.child_start[11:19]}→{rec.child_end[11:19]}` "
                    f"({rec.child_exec_s:.0f}s)"
                    if rec.child_exec_s
                    else "child exec unknown"
                )
                lines.append(
                    f"- mode **async**: launch handle returned in {rec.launch_latency_s:.0f}s, "
                    f"{exec_bit}; report delivery: **{rec.report_delivery}**"
                    + (
                        f" (entered parent context at {rec.r_consume_start[11:19]})"
                        if rec.r_consume_start
                        else ""
                    )
                    + (
                        (
                            " — the child's single terminal wrapper ("
                            + (
                                "description-matched, "
                                if rec.shared_desc_match
                                else "merged report, "
                            )
                            + "Bug K) was already claimed by an earlier boundary of "
                            "this parent; the report was absorbed there, so "
                            "T_report/NCR are not attributable to this boundary"
                        )
                        if rec.report_delivery == "shared_wrapper"
                        else ""
                    )
                )
            lines.append(
                f"- prompt: {rec.prompt_chars:,} chars ≈{rec.t_dispatch_est:,} tok "
                + (
                    f" (exact {rec.t_dispatch_exact:,} tok via child-side Δinput)"
                    if rec.t_dispatch_exact is not None
                    else " (exact unavailable: cold start folds system prompt)"
                )
            )
            if rec.r_k_in is not None:
                lines.append(
                    f"- pre-dispatch R_k: in={rec.r_k_in:,} out={rec.r_k_out:,} "
                    f"reasoning={rec.r_k_reasoning or 0:,} wire.seq={rec.r_k_seq or '-'}"
                )
            lines.append(
                f"- actor: {rec.actor_llm_n} LLM / {rec.actor_tool_n} TOOL calls, "
                f"raw results {rec.actor_raw_result_chars:,} chars, "
                f"Δctx {rec.actor_absorbed_tokens if rec.actor_absorbed_tokens is not None else '-'} tok"
            )
            if rec.child_peak_input is not None:
                over = rec.child_peak_input >= child_peak_warn
                lines.append(
                    f"- child Peak(A): {rec.child_peak_input:,} tok "
                    f"(τ_rotate={child_peak_warn:,})"
                    + (" **⚠CHILD_OVERLOADED (Rotate Session)**" if over else "")
                )
            lines.append(
                f"- report: {rec.report_chars:,} chars, "
                f"protocol `{rec.protocol_state or '?'}` / semantic **{rec.status}**"
                + (" (Issues & Observations present)" if rec.has_issues_section else "")
                + (
                    f", exact injected {rec.t_report_exact:,} tok"
                    + (" ⚠polluted → est effective" if rec.t_report_polluted else "")
                    if rec.t_report_exact is not None
                    else (
                        ", exact injected unavailable — restart_interleaved "
                        "(consume span dropped across host restart) → est effective"
                        if rec.restart_interleaved
                        else ""
                    )
                )
            )
            if rec.inline_escape_ctx_delta is not None:
                lines.append(
                    f"- ↪ **inline escape (C_escape)** attributed to this boundary: parent "
                    f"absorbed the work inline for {rec.inline_escape_duration_s / 60:.0f} min — "
                    f"{rec.inline_escape_tools} tool call(s), "
                    f"+{rec.inline_escape_ctx_delta:,} tok parent context growth"
                )
            econ_bits = [
                f"NCR_char={rec.ncr_char:.2f}" if rec.ncr_char is not None else "NCR_char=-",
                f"NCR_ctx={rec.ncr_ctx:.2f}" if rec.ncr_ctx is not None else "NCR_ctx=-",
                f"N_rem^ep={rec.n_rem_ep} (raw {rec.n_rem}, end={rec.episode_end_reason})",
                f"saved≈{rec.compounded_saved:,} tok-calls",
                f"net_saved={rec.net_saved_tokens:,} tok (β={beta:g})"
                if rec.net_saved_tokens is not None
                else "net_saved=-",
                f"N*_rem={rec.n_rem_star}" if rec.n_rem_star is not None else "N*_rem=-",
                f"RAR={rec.rar}/DSR={rec.dsr}" if rec.rar is not None else "RAR=-/DSR=-",
            ]
            lines.append("- economics: " + ", ".join(econ_bits))
            lines.append(f"- pre-reasoning: “{rec.pre_snippet}”")
            if rec.restart_interleaved and not rec.post_snippet:
                lines.append(
                    "- post-reasoning: — (not harvested: the consume span was dropped "
                    "across a host restart; the first surviving post-dispatch span is "
                    "post-restart)"
                )
            else:
                lines.append(f"- post-reasoning: “{rec.post_snippet}”")
            lines.append("")

    # Δt_parallel: async child windows vs serial sum
    async_pairs = {pid: e for pid, e in pairs.items() if e.get("async")}
    if async_pairs:
        lines.append("### Parallel time savings (Δt_parallel, async child windows)")
        lines.append("")
        lines.append("| parent | async children | Σ child exec (serial) | union wall-clock | Δt_parallel | speedup |")
        lines.append("|---|---|---|---|---|---|")
        for pid, entry in sorted(async_pairs.items()):
            lines.append(
                f"| `{pid[:18]}…` | {entry['async']} | {entry['serial_child_s']:.0f}s "
                f"| {entry['union_child_s']:.0f}s | **{entry['dt_parallel_s']:.0f}s** "
                f"| **{entry['speedup_pct']:.0f}%** |"
            )
        lines.append("")
        lines.append(
            "> Δt_parallel = Σ child_exec − merged-union(child windows); sync dispatches "
            "(parent blocked) contribute non-overlapping windows and are excluded."
        )
        lines.append("")

    # task_* control-plane boundary events
    if task_events:
        lines.append("### task_* control-plane boundary events")
        lines.append("")
        for sid, ev in sorted(task_events.items()):
            bits = []
            if ev["polls"]:
                bits.append(
                    f"**{ev['polls']} polling probes** (task_status without result; "
                    f"{ev['poll_result_chars']:,} result chars ≈{int(ev['poll_result_chars'] / chars_per_token):,} tok overhead)"
                )
            if ev["diagnostic_reads"]:
                bits.append(f"{ev['diagnostic_reads']} diagnostic read(s)")
            if ev["pulls"]:
                bits.append(
                    f"{len(ev['pulls'])} result pull(s) (task_status result=true)"
                )
            if ev["steers"]:
                bits.append(f"{ev['steers']} task_message steer(s)")
            if ev["revives"]:
                bits.append(f"{ev['revives']} task_revive(s)")
            if ev["waits"]:
                bits.append(f"{ev['waits']} wait_for_user")
            if not bits and not ev["pulls"] and not ev["escalations"] and not ev["cancels"]:
                continue
            lines.append(f"- `{sid[:20]}…`: " + "; ".join(bits) if bits else f"- `{sid[:20]}…`:")
            for esc in ev["escalations"]:
                lines.append(
                    f"  - ⚡ escalation ({esc['time'][11:19]}): {esc['kind']}"
                    + (f" {esc.get('wait_kind')}" if esc.get("wait_kind") else "")
                    + f" request `{esc.get('request_id')}`"
                    + (f" → reply {esc['reply']}" if esc.get("reply") else "")
                    + f" target `{str(esc.get('target'))[:24]}`"
                )
            for cancel in ev["cancels"]:
                rework = cancel.get("rework_tokens")
                lines.append(
                    f"  - ✖ cancel ({cancel['time'][11:19]}): target `{str(cancel.get('target'))[:24]}`"
                    + (f" reason=“{cancel['reason']}”" if cancel.get("reason") else "")
                    + (f" → explicit rework cost ≈{rework:,} child tok" if rework else "")
                )
        lines.append("")

    # Blind Spot F: orphan / in-flight children
    orphans: list[dict[str, Any]] = econ.get("orphan_children") or []
    if orphans:
        lines.append("### Orphan / in-flight children (uncovered by parent `subagent` spans)")
        lines.append("")
        lines.append("| child session | parent | classification | age | llm | consumed tok |")
        lines.append("|---|---|---|---|---|---|")
        for o in orphans:
            age = f"{o['age_s'] / 60:.0f} min" if o.get("age_s") is not None else "n/a"
            parent = f"`{o['parent_id'][:18]}…`" if o.get("parent_id") else "-"
            lines.append(
                f"| `{o['session_id'][:20]}…` | {parent} | **{o['classification']}** "
                f"| {age} | {o['llm_n']} | {o['consumed_tokens']:,} |"
            )
        lines.append("")
        lines.append(
            "> `in_flight` = child active within `--child-live-window-sec` of the trace horizon "
            "— still executing, or its covering parent `subagent` span has not flushed to "
            "Phoenix yet (async launches return immediately, so the parent keeps writing newer "
            "spans); never counted as waste. `in_flight_pending_parent` = child recently active "
            "and parent wrote no newer spans (parent blocked inside a sync `subagent` call whose "
            "TOOL span is still open in memory — window `--in-flight-window-sec`). "
            "`orphan_aborted_or_dropped` = child ended/stale with the covering parent span lost "
            "(crash/restart); its consumed tokens count as dropped waste in C_rework (M3). "
            "`delivered_missing_tool_span` = child completed and its "
            "`<subagent sessionID=… state=\"completed\">` wrapper was found in the parent's wire "
            "log — only the Phoenix `execute_tool subagent` TOOL span was dropped; real delivered "
            "work, excluded from orphan waste. `killed_by_host_restart` = the parent's wire log "
            "records an explicit `Tool execution interrupted (sessionID: …)` kill notice in a "
            "structured `tool_result` slot (`is_error` true) and no cancelled wrapper exists for "
            "the child — the child was aborted by a host/server restart; its consumed tokens are "
            "infrastructure overhead, never agent-attributable C_rework. A user interrupt of a "
            "running child produces a byte-identical error tool_result (no reason code); the "
            "cancelled-wrapper check keeps those out of this class, but orphaned user-interrupted "
            "children without a wrapper remain an unavoidable ambiguity until the host emits a "
            "reason code."
        )
        lines.append("")

    total_saved = sum(rec.compounded_saved for rec in dispatches)
    total_escape_tok = sum(r.inline_escape_ctx_delta or 0 for r in dispatches)
    total_escape_tools = sum(r.inline_escape_tools or 0 for r in dispatches)
    total_escape_s = sum(r.inline_escape_duration_s or 0.0 for r in dispatches)
    orphan_waste = sum(
        o["consumed_tokens"] for o in orphans if o["classification"] == "orphan_aborted_or_dropped"
    )
    if dispatches:
        lines.append(
            f"- **compounded prefill noise saved** Σ N_rem^ep·ΔT_noise = **{total_saved:,} token-calls** "
            "(episode-bounded multiplier; estimate — most would have been cache reads — "
            "the harder saving is context-window headroom)"
        )
        if total_escape_tok:
            lines.append(
                f"- **post-failure inline escape (Σ C_escape)** ≈ **+{total_escape_tok:,} tok** parent "
                f"context across {total_escape_tools} inline tool call(s), {total_escape_s / 60:.0f} min — "
                "work the parent absorbed itself after failed/blocked dispatches"
            )
        if orphan_waste:
            lines.append(
                f"- **orphan dropped waste (C_rework)** ≈ **{orphan_waste:,} tok** consumed by child "
                "sessions whose covering parent `subagent` span was lost — re-executed work"
            )
        killed = [o for o in orphans if o["classification"] == "killed_by_host_restart"]
        if killed:
            killed_tok = sum(o["consumed_tokens"] for o in killed)
            lines.append(
                f"- **killed by host restart (infra overhead)** ≈ {killed_tok:,} tok across "
                f"{len(killed)} child session(s) whose parent wire records an explicit "
                "`Tool execution interrupted (sessionID: …)` notice — aborted by "
                "infrastructure, excluded from C_rework"
            )
        wire_recovered = [o for o in orphans if o["classification"] == "delivered_missing_tool_span"]
        if wire_recovered:
            recovered_tok = sum(o["consumed_tokens"] for o in wire_recovered)
            lines.append(
                f"- **recovered via parent wire log** ≈ {recovered_tok:,} tok across "
                f"{len(wire_recovered)} completed child dispatch(es) whose parent TOOL span "
                "was dropped — delivered work confirmed in the wire log, excluded from C_rework"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Phoenix base URL")
    parser.add_argument("--project", default="ocg-obs", help="project name or id")
    parser.add_argument(
        "--db",
        "--sqlite",
        dest="db",
        type=Path,
        default=None,
        help="read directly from the Phoenix SQLite file (e.g. "
        "obsystem/phoenix-runtime/data/phoenix.db); works while the server is down",
    )
    parser.add_argument("--list-projects", action="store_true", help="only list projects")
    parser.add_argument(
        "--json-out",
        "--json",
        dest="json_out",
        type=Path,
        help="also dump raw analysis as JSON",
    )
    parser.add_argument(
        "--chars-per-token",
        type=float,
        default=4.0,
        help="chars/token for estimates from lengths (default 4; measured ≈2.35 "
        "for zh/en-mixed prompts on glm-5.3)",
    )
    parser.add_argument(
        "--beta",
        type=float,
        default=DEFAULT_BETA,
        help="output/input price ratio β = c_out/c_in for NetSaved_k and the "
        f"amortization horizon N*_rem, k (§4.2.3; default {DEFAULT_BETA})",
    )
    parser.add_argument(
        "--boundary-snippet",
        type=int,
        default=200,
        help="max chars of pre/post reasoning snippets in the boundary inspector",
    )
    parser.add_argument(
        "--child-peak-warn",
        type=int,
        default=DEFAULT_CHILD_PEAK_WARN,
        help=f"child saturation waterline τ_rotate in tokens: flag child sessions with "
        f"Peak(A) at/above it as CHILD_OVERLOADED (default {DEFAULT_CHILD_PEAK_WARN:,})",
    )
    parser.add_argument(
        "--episode-gap-min",
        type=int,
        default=DEFAULT_EPISODE_GAP_MIN,
        help=f"episode boundary τ_ep in minutes: N_rem^ep stops at the next compaction or "
        f"an inter-turn idle gap longer than this (default {DEFAULT_EPISODE_GAP_MIN})",
    )
    parser.add_argument(
        "--in-flight-window-sec",
        type=int,
        default=DEFAULT_IN_FLIGHT_WINDOW_SEC,
        help=f"recency window in seconds classifying an uncovered child as "
        f"in_flight_pending_parent vs orphan_aborted_or_dropped "
        f"(default {DEFAULT_IN_FLIGHT_WINDOW_SEC})",
    )
    parser.add_argument(
        "--child-live-window-sec",
        type=int,
        default=DEFAULT_CHILD_LIVE_WINDOW_SEC,
        help=f"liveness window in seconds: an uncovered child whose last activity lies within "
        f"this window of the trace horizon (or wall clock) is classified in_flight and "
        f"excluded from orphan waste (default {DEFAULT_CHILD_LIVE_WINDOW_SEC})",
    )
    parser.add_argument(
        "--wire-dir",
        type=Path,
        default=DEFAULT_WIRE_DIR,
        help="opencode-genai-obs raw-requests JSONL dir for untruncated wire bodies "
        f"(default {DEFAULT_WIRE_DIR}; pass an empty path to disable)",
    )
    args = parser.parse_args(argv)

    try:
        if args.db:
            project, spans = fetch_spans_sqlite(args.db, args.project)
        else:
            if args.list_projects:
                for proj in list_projects(args.endpoint):
                    print(f"{proj['id']}\t{proj['name']}\ttraces={proj['traceCount']}")
                return 0
            project = resolve_project(args.endpoint, args.project)
            spans = fetch_spans(args.endpoint, project["id"])
        sessions = analyze(spans)

        wire_dir = args.wire_dir if (args.wire_dir and args.wire_dir.is_dir()) else None
        econ = build_economics(
            spans,
            wire_dir,
            args.chars_per_token,
            args.boundary_snippet,
            child_peak_warn=args.child_peak_warn,
            episode_gap_min=args.episode_gap_min,
            in_flight_window_sec=args.in_flight_window_sec,
            child_live_window_sec=args.child_live_window_sec,
            beta=args.beta,
        )

        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "project": project,
                        "sessions": [s.as_dict() for s in sessions.values()],
                        "economics": {
                            "bills": [b.as_dict() for b in econ["bills"].values()],
                            "dispatches": [d.as_dict() for d in econ["dispatches"]],
                            "pairs": {
                                pid: {k: v for k, v in entry.items()}
                                for pid, entry in econ["pairs"].items()
                            },
                            "task_events": econ.get("task_events", {}),
                            "child_saturation": econ.get("child_saturation", []),
                            "orphan_children": econ.get("orphan_children", []),
                            "params": econ.get("params", {}),
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"[json] wrote {args.json_out}", file=sys.stderr)

        print(render_report(project, sessions))
        econ_md = render_economics(econ, args.chars_per_token)
        if econ_md.strip():
            print(econ_md)
        return 0
    except urllib.error.URLError as exc:
        print(f"error: cannot reach Phoenix at {args.endpoint}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
