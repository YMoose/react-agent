---
name: react-eval
description: >
  Evaluate and tune the Reasoner/Actor react-agent harness after a work
  session. Use when asked to check react-mode effectiveness, review recent
  ocg-obs sessions, score delegation boundaries (M1-M10 metrics, Three-Cut
  model), diagnose why Reasoner stopped delegating / Actor re-read everything /
  parent context bloated, audit grader integrity in
  scripts/phoenix_subagent_usage.py (UNKNOWN verdicts, delivery=none, false
  orphans, polluted T_report), or iterate prompts/orchestrator.md and
  prompts/fixer.md with changelog discipline and live deployment. Covers the
  full closed loop: telemetry run -> measurement sanity gate -> Three-Cut
  economic diagnosis -> surgical prompt patch -> sanitized changelog + commit
  + sync to ~/.config/opencode/oh-my-opencode-slim/react/.
---

# React-Agent Evaluation & Tuning

Use the 5-phase workflow developed across the `v2.0 -> v3.5` iterations.
Never skip Phase 2 (grader integrity) before blaming a prompt, and never edit
a prompt without the Phase 5 governance trail.

## Core References & Paths

- Repo root: `~/yzh/obsystem/react-agent`
- Live runtime prompts (deploy state): `~/.config/opencode/oh-my-opencode-slim/react/{orchestrator,fixer}.md`
- Wire logs (check capture settings, truncation and missing records): `~/.local/share/opencode/raw-requests/ses_*.jsonl`
- Theory & methodology:
  - `../../reports/react-mode-eval.md` — local model: 3-layer Context Economics
    Inequality, Three-Cut boundary model, M1–M10 metric registry
  - `../../reports/react-mode-eval.md` §6 — measurement and comparison guardrails
- Empirical failure & grader-bug catalog: `./references/tuning-casebook.md`

## Phase 1 — Prompt-Version Forensics & Telemetry Run (摸清运行版本与拉取遥测)

Prompt refresh depends on the host/plugin version, runtime profile and reload
path. The current plugin reads prompt files during agent construction and
profile refresh; compaction alone is not proof of a prompt reload. Before
interpreting any scorecard, establish which prompt each request actually used:

1. Extract marker strings (distinctive sentences) from
   `../../prompts/{orchestrator,fixer}.md` per git revision
   (`git -C ../../ log -p -- prompts/`), then grep them in
   `~/.local/share/opencode/raw-requests/ses_<session>.jsonl` wire frames
   (inspect system/developer messages or provider-specific `system` /
   `instructions` fields and content blocks; do not assume `messages[0]`).
2. Record per session: prompt version, start/end time, compaction events.
   Attribute behavior only to the version that was actually live.

Then pull the scorecard:

```bash
python3 ~/yzh/obsystem/react-agent/scripts/phoenix_subagent_usage.py \
  --project ocg-obs --chars-per-token 2.35 \
  --json-out /tmp/opencode/react-eval.json
```

(Offline fallback: `--db <snapshot>/phoenix.db` using a consistent SQLite
backup, or copy db + wal + shm after stopping writes. `2.35` is a historical
calibration for sampled zh/en-mixed glm traffic; the CLI default is `4.0`.
Calibrate for other tokenizers and workloads.)

## Phase 2 — Grader & Measurement Integrity Gate (测量仪自检——先验表、再判人)

Follow `react-mode-eval.md` §6: validate the meter before tuning. Before attributing
any `UNKNOWN` verdict, `delivery: none`, `FAILED_DISPATCH`,
`orphan_aborted_or_dropped`, or `⚠polluted` figure to a prompt failure,
cross-check the raw wire log `~/.local/share/opencode/raw-requests/ses_<parent>.jsonl`:

1. **Async notification hidden in wire**: did a
   `<subagent sessionID=\"...\" state=\"completed\">...</subagent>` wrapper
   land in a later parent request frame even though the exporter truncated
   `input.value` (default 60 KiB)? Quotes can be JSON-escaped inside bodies.
   The recorder supplies both JSONL `seq` and Span `opencode.wire.seq`, but
   resets on recreation. The notification scan uses `ts` and wrapper counts;
   `wire_body()` still looks up by seq alone and can misbind across restarts.
   HTTP `ts` records body-read completion, so time matching is approximate.
   Check `truncated` / `frameBytes` and capture gaps before trusting recovery.
2. **False orphan vs delivered vs in-flight**: does the uncovered child's
   wrapper appear in the parent wire JSONL (exporter dropped the parent
   `execute_tool subagent` TOOL span → `delivered_missing_tool_span`, not
   waste)? Recent activity within `300s` is classified `in_flight`; the
   `600s` silent-parent window yields `in_flight_pending_parent`. These are
   configurable recency heuristics, not proof of liveness. A stale child with
   no wire evidence can be `orphan_aborted_or_dropped`, but missing telemetry
   does not prove wasted work. `killed_by_host_restart` is also a fallback
   label: interruption notices do not uniquely identify restart causes.
3. **T_report pollution**: did the usage-based Δin candidate between consecutive parent
   requests absorb host injections (`<system-update>` catalog swaps,
   `<conversation-checkpoint>`, server-restart recovery prompts) instead of
   the actor report alone? (Gate: exact > 2× estimate by > 1,500 tok →
   use the estimate; neither value proves exact report attribution.)
4. **Peak attribution**: compare `Peak(R)` with first-observed input
   `in(R_1)` and `Δpeak_self = Peak(R) − in(R_1)`. First input includes
   system/history/tool schemas as well as user content; a small delta
   (e.g. < 40k) is a diagnostic heuristic, not proof of who caused the size.
   Confirm that the selected telemetry actually includes the first request.

Rule: if a measurement bug is found, fix
`../../scripts/phoenix_subagent_usage.py`, validate with
`python3 -m py_compile`, re-run the scorecard, and commit in a dedicated
`scripts:` commit **before** proposing any prompt edit. Details and prior
fixes: `./references/tuning-casebook.md` Part B.

## Phase 3 — Three-Cut Boundary & M1–M10 Economic Diagnosis (三刀切与指标诊断)

Inspect the provider-visible reasoning text from `gen_ai.output.messages` reasoning
parts (legacy fallback `opencode.reasoning`) in the boundary inspector. This
is not a complete internal reasoning trace. M6–M10 are proposed/manual
checks, not all implemented analyzer outputs. Diagnose per cut:

### Cut A — Pre-Dispatch Decision & Outbound Packing

- Delegation trigger rate & post-compaction compliance: did Reasoner delegate
  immediately after compaction, or quote an exemption clause verbatim to
  self-exempt (the v3.3 loophole pattern)?
- Sync/async intent: `background: false` on blocking follow-ups vs
  `background: true` fan-out; flag accidental default-background dispatches.
- Outbound self-sufficiency (`DSR >= 0.80`, `RAR < 0.25`): did Reasoner pack
  `path:line` anchors and spec excerpts already in its context, or dispatch
  "hands-off" and force the child to re-read at `RAR > 1`?

### Cut B — Child Execution & Dual-Layer Return

- `Protocol State × Semantic Status` matrix: `Completed | Yielded (Needs
  Reasoning) | Blocked | Empty`; an `Empty` terminal with a non-empty
  protocol claim is a degraded boundary.
- `fixer.md` adaptive compactness (M10): completed turns with ≤ 4 tool calls
  and no blockers/anomalies use ≤ 6 lines; Yielded/Blocked turns use the full
  contract even with few tools.
- Child input warning threshold: default `τ_rotate = 120k` tokens, configurable
  via `--child-peak-warn`. It is a local rotation policy, not a model limit
  or proof that a high-input session will crash.

### Cut C — Absorption, NetSaved & Rotation

- `NCR_ctx >= 8:1` (`HIGH_GAIN` zone); `NetSaved` with `β = 4.0` and the
  breakeven horizon `N*_rem` vs episode-bounded `N_rem^ep`. These use
  estimates and assumed prices, not measured total cost savings.
- Warm reuse vs cold rotation: rotate on `Empty`/error/saturated lanes
  (the historical `k=5` high-input empty resume; cause unconfirmed) instead of trusting a silent final
  turn (`k=12 -> k=13` pattern); carry anchors into the fresh child.
- `C_escape = 0` (no post-failure inline tool streaks) and `C_poll = 0`
  (no `task_status` polling probes; async notifications consumed passively).

## Phase 4 — Surgical Prompt Iteration (local design guidance)

1. **Verifiable preconditions over cost formulas**: never put economic
   trade-off formulas (`ΔT_raw -> 0`, "re-prefill drag") or
   self-bootstrapping `read`/`shell` loopholes into runtime prompts — they
   hand the model a self-exemption argument. Express checkable states, e.g.
   "verbatim target lines (not checkpoint summaries) are already present in
   your context".
2. **Ground-truth loop over speculative patches**: only patch failure
   modes backed by concrete telemetry from the current evaluation window;
   defer theoretical edge cases with `0` observed occurrences.
3. **One atomic root-cause change & lean budget**:
   one prompt change per iteration, validated before the next; keep
   `orchestrator.md` + `fixer.md` under ~80 lines total — delete or tighten
   before adding.

## Phase 5 — Changelog, Privacy Sanitization, Git Commit & Live Deployment

1. Prepend a detailed entry to `../../prompts/changelog.md` with
   **Trigger / Evidence** (sanitized session prefixes, boundary `k`, numbers),
   **Target Economics / Boundary** (inequality terms + cut), and
   **Expected Metric Impact** (M-ids + direction). Strip private paths,
   credentials, personal identifiers, and raw user transcripts.
2. Stage only `prompts/*.md` + `prompts/changelog.md` in a dedicated
   `prompts: <concise imperative summary>` commit; keep the message brief
   (1–2 line body pointing at `prompts/changelog.md`); never mix prompt
   commits with script or report edits.
3. Deploy and verify byte-identity before any evaluation session:

```bash
cp ~/yzh/obsystem/react-agent/prompts/orchestrator.md \
   ~/yzh/obsystem/react-agent/prompts/fixer.md \
   ~/.config/opencode/oh-my-opencode-slim/react/ \
  && diff -u ~/yzh/obsystem/react-agent/prompts/orchestrator.md \
             ~/.config/opencode/oh-my-opencode-slim/react/orchestrator.md \
  && diff -u ~/yzh/obsystem/react-agent/prompts/fixer.md \
             ~/.config/opencode/oh-my-opencode-slim/react/fixer.md
```

Empty `diff` output verifies the deployed files, not the prompt in a running
request. Sync repo -> live; then verify fresh wire frames after the applicable
host/plugin refresh. Project prompt overrides may take precedence. Do not
assume restart or compaction alone proves activation (Phase 1).
