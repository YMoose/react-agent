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

Drive the 5-phase closed loop proven across the `v2.0 -> v3.5` iterations.
Never skip Phase 2 (grader integrity) before blaming a prompt, and never edit
a prompt without the Phase 5 governance trail.

## Core References & Paths

- Repo root: `~/yzh/engineering/obsystem/react-agent`
- Live runtime prompts (deploy state): `~/.config/opencode/oh-my-opencode-slim/react/{orchestrator,fixer}.md`
- Raw untruncated wire logs: `~/.local/share/opencode/raw-requests/ses_*.jsonl`
- Theory & methodology:
  - `../../reports/react-mode-eval.md` — OCP spec: 3-layer Context Economics
    Inequality, Three-Cut boundary model, M1–M10 metric registry
  - `../../reports/eval-method.md` — grader integrity, confound control,
    anti-overfit hillclimbing laws (train/held-out, no failure pasting)
  - `~/yzh/engineering/context-engineering/prompt-principes.md`
    (§2.1–§2.6) — prompt design principles for high-reasoning models
- Empirical failure & grader-bug catalog: `./references/tuning-casebook.md`

## Phase 1 — Prompt-Version Forensics & Telemetry Run (摸清运行版本与拉取遥测)

OpenCode caches a session's system prompt in memory until restart or
compaction, so a session that started before a prompt edit keeps running the
OLD version. Before interpreting any scorecard, establish which prompt
version each evaluated session actually ran:

1. Extract marker strings (distinctive sentences) from
   `../../prompts/{orchestrator,fixer}.md` per git revision
   (`git -C ../../ log -p -- prompts/`), then grep them in
   `~/.local/share/opencode/raw-requests/ses_<session>.jsonl` wire frames
   (the `messages[0]` system content holds the live prompt verbatim).
2. Record per session: prompt version, start/end time, compaction events.
   Attribute behavior only to the version that was actually live.

Then pull the scorecard:

```bash
python3 ~/yzh/engineering/obsystem/react-agent/scripts/phoenix_subagent_usage.py \
  --project ocg-obs --chars-per-token 2.35 \
  --json-out /tmp/opencode/react-eval.json
```

(Offline fallback when Phoenix is down: `--db <snapshot>/phoenix.db` after
snapshotting db + wal + shm. Expect `--chars-per-token 2.35` for zh/en-mixed
glm traffic.)

## Phase 2 — Grader & Measurement Integrity Gate (测量仪自检——先验表、再判人)

`eval-method.md` §3–§4: never hillclimb on a broken meter. Before attributing
any `UNKNOWN` verdict, `delivery: none`, `FAILED_DISPATCH`,
`orphan_aborted_or_dropped`, or `⚠polluted` figure to a prompt failure,
cross-check the raw wire log `~/.local/share/opencode/raw-requests/ses_<parent>.jsonl`:

1. **Async notification hidden in wire**: did a
   `<subagent sessionID=\"...\" state=\"completed\">...</subagent>` wrapper
   land in a later parent request frame even though Phoenix truncated
   `input.value` at 60 KiB? Remember the quotes are JSON-escaped inside
   serialized bodies, and the JSONL `seq` counter resets across host
   restarts (do not join frames by seq; use record `ts` order).
2. **False orphan vs delivered vs in-flight**: does the uncovered child's
   wrapper appear in the parent wire JSONL (exporter dropped the parent
   `execute_tool subagent` TOOL span → `delivered_missing_tool_span`, not
   waste)? Or is the child still active within `300s` of the trace horizon
   (`in_flight`, never waste)? True `orphan_aborted_or_dropped` requires a
   stale child with no wire evidence.
3. **T_report pollution**: did the exact Δin between consecutive parent
   requests absorb host injections (`<system-update>` catalog swaps,
   `<conversation-checkpoint>`, server-restart recovery prompts) instead of
   the actor report alone? (Gate: exact > 2× estimate by > 1,500 tok →
   trust the estimate.)
4. **Peak attribution**: is a high `Peak(R)` inherited from the Turn-1 user
   payload (`in(R_1)` large, `Δpeak_self = Peak(R) − in(R_1) < 40k` → born
   large) rather than self-acquired inline bloat (large `Δpeak_self`)?

Rule: if a measurement bug is found, fix
`../../scripts/phoenix_subagent_usage.py`, validate with
`python3 -m py_compile`, re-run the scorecard, and commit in a dedicated
`scripts:` commit **before** proposing any prompt edit. Details and prior
fixes: `./references/tuning-casebook.md` Part B.

## Phase 3 — Three-Cut Boundary & M1–M10 Economic Diagnosis (三刀切与指标诊断)

Extract Reasoner chain-of-thought from `gen_ai.output.messages` reasoning
parts (legacy fallback `opencode.reasoning`) in the boundary inspector, then
diagnose per cut:

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
- `fixer.md` adaptive compactness (M10): light turns (≤ 4 tool calls) must
  return ≤ 6 lines instead of the full 4-section report.
- Child saturation waterline: `Peak(A) < τ_rotate = 120k` tokens; at/above
  it, warm reuse must rotate to a fresh cold child.

### Cut C — Absorption, NetSaved & Rotation
- `NCR_ctx >= 8:1` (`HIGH_GAIN` zone); `NetSaved` with `β = 4.0` and the
  breakeven horizon `N*_rem` vs actual episode-bounded `N_rem^ep`.
- Warm reuse vs cold rotation: rotate on `Empty`/error/saturated lanes
  (the `k=5` saturated dead-lane crash) instead of trusting a silent final
  turn (`k=12 -> k=13` pattern); carry anchors into the fresh child.
- `C_escape = 0` (no post-failure inline tool streaks) and `C_poll = 0`
  (no `task_status` polling probes; async notifications consumed passively).

## Phase 4 — Surgical Prompt Iteration (prompt-principes.md + eval-method.md guardrails)

1. **Verifiable preconditions over cost formulas (§2.1)**: never put economic
   trade-off formulas (`ΔT_raw -> 0`, "re-prefill drag") or
   self-bootstrapping `read`/`shell` loopholes into runtime prompts — they
   hand the model a self-exemption argument. Express checkable states, e.g.
   "verbatim target lines (not checkpoint summaries) are already present in
   your context".
2. **Ground-truth loop over speculative patches (§2.2)**: only patch failure
   modes backed by concrete telemetry from the current evaluation window;
   defer theoretical edge cases with `0` observed occurrences.
3. **One atomic root-cause change & lean budget (§2.6, eval-method.md §5.1)**:
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
cp ~/yzh/engineering/obsystem/react-agent/prompts/orchestrator.md \
   ~/yzh/engineering/obsystem/react-agent/prompts/fixer.md \
   ~/.config/opencode/oh-my-opencode-slim/react/ \
  && diff -u ~/yzh/engineering/obsystem/react-agent/prompts/orchestrator.md \
             ~/.config/opencode/oh-my-opencode-slim/react/orchestrator.md \
  && diff -u ~/yzh/engineering/obsystem/react-agent/prompts/fixer.md \
             ~/.config/opencode/oh-my-opencode-slim/react/fixer.md
```

Empty `diff` output = deployed. Sync only repo -> live, never the reverse
without a documented changelog entry. Remember: running sessions keep their
cached prompt until restart/compaction — schedule the next eval window
accordingly (Phase 1).
