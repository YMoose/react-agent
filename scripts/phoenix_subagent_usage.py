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
  plus compounded prefill savings sum(N_rem,k * dT_noise,k)
- Metric 2: warm session hit rate (sessionID reuse) and an overlap-aware
  redundant file-read rate (disjoint pagination vs true re-reads)
- Metric 3: boundary status counts (Completed / Yielded / Blocked) and
  handoff rework ratio, including task_* control-plane events
  (task_cancel = explicit plan invalidation, task_reply / waiting_input =
  mid-flight escalations, task_status polling probes)
- Metric 4: per-session token & latency bill (context peak, cache_read %,
  visible vs reasoning output) and the Handoff Essay Friction Share
- Per-dispatch Reasoner<->Actor boundary inspector with economic verdicts
  (HIGH_GAIN / MODERATE_GAIN / MARGINAL_OVERHEAD / HANDOFF_RETRY)

Sync and async delegation are both supported. Async launches
(``subagent(background=true)`` or any result matching omos'
``The subagent is working in the background (sessionID: …)`` /
``<task id="…" state="running">`` patterns) are detected from the tool
result — the ``background`` parameter alone is unreliable. For async the
child execution window runs from launch to the report delivery point
(native ``<subagent state=…>`` notification in a later parent request, a
``task_status(result=true)`` pull, the next re-dispatch, or the child's
last span), and Δt_parallel = Σ child_exec − merged window union.

All formulas are grounded in OTel GenAI semconv attributes
(``gen_ai.usage.*``, ``gen_ai.tool.call.*``) plus the OpenInference/OpenCode
bridge attributes (``input.value``, ``input.value_original_length``,
``output.value``, ``opencode.reasoning``, ``opencode.wire.seq``,
``opencode.session.parent_id``).  When a span body was truncated by the
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
from datetime import datetime
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
    """Parse ISO-ish timestamps from either transport ("…T…Z" or "YYYY-MM-DD HH:MM:SS.ffffff")."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in value else value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
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


def child_wrapper_re(child_session: str) -> re.Pattern[str]:
    """Host-authored terminal wrapper for one specific child session."""
    return re.compile(
        r'<subagent sessionID="'
        + re.escape(child_session)
        + r'" state="(\w+)"(?:\s+description="([^"]*)")?\s*>([\s\S]*?)</subagent>'
    )


def classify_launch(result_text: str) -> tuple[str, str | None, str | None]:
    """(mode, child_sessionID_from_output, protocol_state) from the tool result.

    Async launches are detected from omos' actual output patterns — NOT from the
    ``background`` parameter, which is empirically absent on async launches too.
    Patterns mirror ``oh-my-opencode-slim/src/utils/task.ts``: the v2 native
    background sentence and the v1 byte-stable running placeholder.
    """
    m = NATIVE_BACKGROUND_RE.search(result_text)
    if m:
        return "async", m.group(1), "running"
    m = RUNNING_TASK_TAG_RE.search(result_text)
    if m:
        return "async", m.group(1), "running"
    m = SUBAGENT_WRAPPER_RE.search(result_text)
    if m:
        return "sync", m.group(1), m.group(2)
    return "sync", None, None


def task_status_state(result_text: str) -> str | None:
    """Protocol state line from a task_status render (``state: <word>``)."""
    m = re.search(r"^state:\s*(\w+)", result_text, re.MULTILINE)
    return m.group(1) if m else None


def snippet(text: str, limit: int) -> str:
    """Single-line reasoning snippet, markdown-table safe."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def load_wire_frames(wire_dir: Path | None, session_id: str) -> dict[str, str]:
    """seq → raw request frame for one session, from the plugin's untruncated JSONL sink."""
    if not wire_dir:
        return {}
    path = wire_dir / f"{session_id}.jsonl"
    frames: dict[str, str] = {}
    if not path.exists():
        return frames
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
                seq = rec.get("seq")
                frame = rec.get("frame")
                if seq is not None and isinstance(frame, str):
                    frames[str(seq)] = frame
    except OSError:
        return {}
    return frames


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
        start = float((rc.offset or 1) - 1)
        end = start + (rc.limit if rc.limit is not None else 10**9)
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
    def cache_hit_pct(self) -> float:
        return self.tot_cache_read / self.tot_input * 100 if self.tot_input else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "roles": sorted(self.roles),
            "llm_calls": self.llm_n,
            "peak_input_tokens": self.peak_input,
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
    t_report_exact: int | None = None  # Δin(R_k+1) − in(R_k) − out(R_k)
    t_report_est: int = 0
    # economics
    ncr_char: float | None = None
    ncr_ctx: float | None = None
    n_rem: int = 0
    dt_noise_tokens: int = 0
    compounded_saved: int = 0
    verdict: str = "UNKNOWN"

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
            "ncr_char": round(self.ncr_char, 2) if self.ncr_char else None,
            "ncr_ctx": round(self.ncr_ctx, 2) if self.ncr_ctx else None,
            "n_rem": self.n_rem,
            "dt_noise_tokens": self.dt_noise_tokens,
            "compounded_saved": self.compounded_saved,
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
                    params = dig(attrs, "tool", "parameters") or {}
                    off = params.get("offset")
                    lim = params.get("limit")
                    bill.reads.append(
                        ReadCall(
                            ts=span.get("startTime", ""),
                            path=str(params.get("path") or "?"),
                            offset=int(off) if isinstance(off, (int, float, str)) and str(off).lstrip("-").isdigit() else None,
                            limit=int(lim) if isinstance(lim, (int, float, str)) and str(lim).lstrip("-").isdigit() else None,
                            out_chars=len(tool_result_text(attrs)),
                        )
                    )
        bills[sid] = bill

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
                if params.get("result") is True or str(params.get("result")).lower() == "true":
                    events["pulls"].append(
                        {"time": ts, "target": target, "result_chars": len(result_text), "result": result_text}
                    )
                elif params.get("diagnostic") is True or str(params.get("diagnostic")).lower() == "true":
                    events["diagnostic_reads"] += 1
                else:
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
        wire_frames = load_wire_frames(wire_dir, sid)
        # task_status(result=true) pulls indexed by target session for this parent
        pulls_by_child: dict[str, list[dict[str, Any]]] = {}
        for evt in task_events.get(sid, {}).get("pulls", []):
            if evt["target"]:
                pulls_by_child.setdefault(evt["target"], []).append(evt)
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
            consume_span: tuple[dict[str, Any], dict[str, Any]] | None = None
            if rec.mode == "async":
                rec.report_delivery = "none"
                # (a) native completion notification in a later parent request
                if rec.child_session:
                    wpat = child_wrapper_re(rec.child_session)
                    for idx, (llm_span, llm_attrs) in enumerate(parent_llm):
                        s0 = tso(llm_span.get("startTime", ""))
                        if not s0 or not t0 or s0 < t0:
                            continue
                        body, _, _ = wire_body(llm_attrs, wire_frames)
                        wm = wpat.search(body or "")
                        if wm:
                            rec.report_delivery = "native_notification"
                            rec.protocol_state = wm.group(1)
                            report_text = wm.group(3).strip()
                            if report_text.startswith("[") or report_text.startswith('"'):
                                report_text, _ = decode_tool_result(report_text)
                            result_text = report_text
                            terminal_t = s0
                            consume_span = (llm_span, llm_attrs)
                            # exact T_report: Δin(R_consume) − in(prev) − out(prev)
                            if idx > 0:
                                prev_u = usage_of(parent_llm[idx - 1][1])
                                cur_u = usage_of(llm_attrs)
                                exact = (
                                    cur_u["input_tokens"]
                                    - prev_u["input_tokens"]
                                    - prev_u["output_tokens"]
                                )
                                rec.t_report_exact = exact if exact >= 0 else None
                                rec.r_consume_in = cur_u["input_tokens"]
                                rec.r_consume_start = llm_span.get("startTime", "")
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
                                    break
                            for llm_span, llm_attrs in parent_llm:
                                s0 = tso(llm_span.get("startTime", ""))
                                if s0 and pt and s0 > pt:
                                    rec.r_next_in = usage_of(llm_attrs)["input_tokens"]
                                    rec.r_consume_in = rec.r_next_in
                                    rec.r_consume_start = llm_span.get("startTime", "")
                                    if rec.r_k_in is not None:
                                        rec.t_report_exact = max(
                                            rec.r_next_in - rec.r_k_in - rec.r_k_out, 0
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
                        rec.pre_snippet = snippet(dig(llm_attrs, "opencode", "reasoning") or "", snippet_len)
                        body, trunc, src = wire_body(llm_attrs, wire_frames)
                        rec.body_source_k, rec.body_truncated_k = src, trunc
                        break

            # R_k+1: first parent LLM span starting after the dispatch end
            if rec.r_next_in is None:
                for llm_span, llm_attrs in parent_llm:
                    s0 = tso(llm_span.get("startTime", ""))
                    if s0 and t1 and s0 > t1:
                        rec.r_next_in = usage_of(llm_attrs)["input_tokens"]
                        rec.post_snippet = snippet(dig(llm_attrs, "opencode", "reasoning") or "", snippet_len)
                        break
            # N_rem: parent LLM calls after the report actually entered context
            rem_after = terminal_t if (rec.mode == "async" and terminal_t) else t1
            rec.n_rem = sum(
                1
                for llm_span, _ in parent_llm
                if (s0 := tso(llm_span.get("startTime", ""))) and rem_after and s0 > rem_after
            )

            # exact T_report (sync): Δin(R_k+1) − in(R_k) − out(R_k)
            if rec.mode == "sync" and rec.r_next_in is not None and rec.r_k_in is not None:
                exact = rec.r_next_in - rec.r_k_in - rec.r_k_out
                # negative ⇒ compaction/turn-type change between samples: unreliable
                rec.t_report_exact = exact if exact >= 0 else None

            report, state = decode_tool_result(result_text)
            rec.report_state = wrapper_state or state or rec.protocol_state
            if rec.mode == "async" and rec.report_delivery == "none":
                # no delivery evidence: the launch handle is NOT a report —
                # leave T_report unknown rather than fake a huge NCR
                report = ""
                rec.report_state = rec.protocol_state
            rec.report_chars = len(report)
            rec.t_report_est = int(rec.report_chars / chars_per_token)
            rec.status = parse_status(report) if report else "unknown"
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

            # ratios
            if rec.report_chars and rec.actor_raw_result_chars:
                rec.ncr_char = rec.actor_raw_result_chars / rec.report_chars
            report_tok = rec.t_report_exact if rec.t_report_exact is not None else rec.t_report_est
            if rec.actor_absorbed_tokens is not None and report_tok:
                rec.ncr_ctx = rec.actor_absorbed_tokens / report_tok
            if report_tok:  # unknown T_report (undelivered async) ⇒ savings unknowable
                raw_tok = int(rec.actor_raw_result_chars / chars_per_token)
                rec.dt_noise_tokens = max(raw_tok - report_tok, 0)
                rec.compounded_saved = rec.n_rem * rec.dt_noise_tokens

            score = rec.ncr_ctx if rec.ncr_ctx is not None else rec.ncr_char
            if score is None:
                rec.verdict = "UNKNOWN"
            elif rec.status in ("yielded", "blocked"):
                rec.verdict = "HANDOFF_RETRY"
            elif score >= VERDICT_HIGH_NCR:
                rec.verdict = "HIGH_GAIN"
            elif score >= VERDICT_MODERATE_NCR:
                rec.verdict = "MODERATE_GAIN"
            else:
                rec.verdict = "MARGINAL_OVERHEAD"
            dispatches.append(rec)

    dispatches.sort(key=lambda d: (d.parent_session, d.start))

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
            entry["t_report_total"] += rec.t_report_exact
        else:
            entry["t_report_est"] += rec.t_report_est
            entry["t_report_total"] += rec.t_report_est
        entry["report_chars"] += rec.report_chars
        entry["raw_chars"] += rec.actor_raw_result_chars
        entry["absorbed"] += rec.actor_absorbed_tokens or 0
        entry["saved"] += rec.compounded_saved
        if rec.child_session:
            entry["children"].add(rec.child_session)
        entry["statuses"][rec.status] += 1
        entry["verdicts"][rec.verdict] += 1
        if rec.status in ("yielded", "blocked") or rec.verdict in ("HANDOFF_RETRY", "MARGINAL_OVERHEAD"):
            entry["rework"] += 1
        if rec.mode == "async":
            entry["async"] += 1
            c0, c1 = tso(rec.child_start), tso(rec.child_end)
            if c0 and c1:
                entry["child_windows"].append((c0, c1))

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
    }


def render_economics(econ: dict[str, Any], chars_per_token: float) -> str:
    """Render the economics scorecard + boundary inspector as markdown."""
    bills: dict[str, SessionBill] = econ["bills"]
    dispatches: list[DispatchBoundary] = econ["dispatches"]
    pairs: dict[str, dict[str, Any]] = econ["pairs"]
    task_events: dict[str, dict[str, Any]] = econ.get("task_events", {})
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
        "| session | role | llm | peak input | tot input | cache read | cache% "
        "| out | reasoning | visible |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for bill in sorted(bills.values(), key=lambda b: b.first):
        if not bill.llm_n:
            continue
        lines.append(
            f"| `{bill.session_id[:20]}…` | {','.join(sorted(bill.roles)) or '-'} "
            f"| {bill.llm_n} | {bill.peak_input:,} | {bill.tot_input:,} "
            f"| {bill.tot_cache_read:,} | {bill.cache_hit_pct:.0f}% "
            f"| {bill.tot_output:,} | {bill.tot_reasoning:,} | {bill.visible_output:,} |"
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

    # Boundary inspector
    if dispatches:
        lines.append("## Per-dispatch boundary inspector (Reasoner ↔ Actor)")
        lines.append("")
        lines.append(
            "| k | window | mode | launch | child exec | warm | T_disp (exact/est) "
            "| actor llm/tool | raw chars | Δctx tok | delivery | T_report (exact/est) "
            "| NCR_char | NCR_ctx | N_rem | saved | protocol | status | verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
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
                .replace("none", "⚠ none")
            )
            lines.append(
                f"| {rec.k} | {rec.start[11:19]}→{rec.end[11:19]} | {rec.mode} | {launch} "
                f"| {child_exec} | {'warm' if rec.warm else 'COLD'} | {t_disp} "
                f"| {rec.actor_llm_n}/{rec.actor_tool_n} | {rec.actor_raw_result_chars:,} "
                f"| {absorbed} | {delivery} | {t_rep} | {ncr_char} | {ncr_ctx} "
                f"| {rec.n_rem} | {rec.compounded_saved:,} "
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
            lines.append(
                f"- report: {rec.report_chars:,} chars, "
                f"protocol `{rec.protocol_state or '?'}` / semantic **{rec.status}**"
                + (" (Issues & Observations present)" if rec.has_issues_section else "")
                + (
                    f", exact injected {rec.t_report_exact:,} tok"
                    if rec.t_report_exact is not None
                    else ""
                )
            )
            econ_bits = [
                f"NCR_char={rec.ncr_char:.2f}" if rec.ncr_char is not None else "NCR_char=-",
                f"NCR_ctx={rec.ncr_ctx:.2f}" if rec.ncr_ctx is not None else "NCR_ctx=-",
                f"N_rem={rec.n_rem}",
                f"saved≈{rec.compounded_saved:,} tok-calls",
            ]
            lines.append("- economics: " + ", ".join(econ_bits))
            lines.append(f"- pre-reasoning: “{rec.pre_snippet}”")
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

    total_saved = sum(rec.compounded_saved for rec in dispatches)
    if dispatches:
        lines.append(
            f"- **compounded prefill noise saved** Σ N_rem·ΔT_noise = **{total_saved:,} token-calls** "
            "(estimate; most would have been cache reads — the harder saving is context-window headroom)"
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
        "--boundary-snippet",
        type=int,
        default=200,
        help="max chars of pre/post reasoning snippets in the boundary inspector",
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
        econ = build_economics(spans, wire_dir, args.chars_per_token, args.boundary_snippet)

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
