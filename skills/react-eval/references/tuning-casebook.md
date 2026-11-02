# React-Agent Tuning Casebook (`v2.0 -> v3.5`)

Empirical catalog of prompt-iteration outcomes and analyzer (grader) bugs
observed in the `ocg-obs` project. Use it to recognize recurring failure
signatures quickly and to avoid re-introducing fixed anti-patterns.
All session identifiers are sanitized prefixes; no private paths or
transcripts.

## Part A — Prompt Iteration Casebook

### v2.0 (Baseline, commit `2d7bbd3`)

- `31`-line `orchestrator.md` + `23`-line `fixer.md`: Reasoner/Actor split,
  warm `sessionID` reuse across Scout -> Implement -> Verify, fixed 4-part
  Actor report contract.
- Benchmark (`ses_e730e619…` / `ses_e730e0ee…`): `5` sync dispatches,
  γ_warm `80%`, `NCR_char = 9.48:1`, `NCR_ctx = 12.62:1`, Reasoner peak
  `30,166` tok vs `195,749` inline baseline (`-58.5%` total input).

### v3.1 — Warm-Saturation Crash -> Rotation Rule (Cut C)

- Failure (`ses_e799a9c6…` `k=5`): warm child (`ses_e70bb348…`) reached
  `129` LLM turns / `274,198` input tokens; resume returned `0 chars` in
  `0s` (`FAILED_DISPATCH`). With no fallback branch, Reasoner escaped to
  `80` min of inline work (`40` read / `15` edit / `6` write), parent
  context `97,519 -> 175,941` tok, mid-task compaction
  `170,124 -> 39,409`, spec re-read, until a fresh cold Actor at `k=6`
  restored `NCR_ctx = 22.84:1`.
- Also `ses_e7063a72…` `k=0`: server restart severed an in-flight sync
  dispatch (`45.6k` orphaned child tokens).
- Fix: Step 3 Warm Reuse / Cold Fallback split — on empty/error/saturated
  warm lane, spawn a fresh cold Actor with `path:line` anchors, never
  inline. τ_rotate waterline `120k` added to the analyzer (Blind Spot D).

### v3.2 — Adaptive Compact Reporting (Cut B, `fixer.md`)

- Failure (`ses_e730e619…` `k=4`): a `4`-tool light check produced a
  `1,101`-token 4-section report (`NCR_ctx` collapsed to `4.54:1`); across
  `ses_e730e0ee…` `k=1..5`, `42%` of child visible output was report prose.
- Fix: light completed turns (≤ `4` tool calls) return ≤ `6` lines; full
  contract reserved for heavy or `Yielded`/`Blocked` turns. Targets M10
  (`T_report < 300` tok on light turns), M1 (`NCR_ctx > 8:1`), M4
  (friction share `< 25%`).

### v3.3 — Anti-Pattern: Cost Formulas & Read Allowances (cautionary)

- The prompt added an inline exemption justified by cost economics
  ("act inline when `ΔT_raw -> 0`") plus a `<150`-line focused-read and a
  single `git`/`diff` shell allowance.
- Failure (`ses_e828c9c15…`, post-compaction reload of v3.3):
  `0` subagent calls across `56` requests after `35` earlier dispatches —
  Reasoner self-exempted via three vectors: (1) temporal inversion (used the
  read allowance to *manufacture* the exemption precondition), (2)
  economic-formula self-override ("doing it inline is more reliable than
  paying dispatch + re-prefill"), (3) shell allowance creep (`npm test`,
  `npm run build`, `git push`). Peak hit `149,816` tok + mid-task
  compaction.
- Lesson (`prompt-principes.md` §2.1): runtime prompts must state checkable
  preconditions, never trade-off formulas the model can argue with.

### v3.4 — Strict Inline Prohibition + Single-Edit Exception

- Fix: `<Role>` rewritten — first tool call and every discovery/multi-edit/
  verification step MUST be `subagent(agent="actor")`; at most ONE
  single-block `edit` per turn when verbatim target lines
  **(not checkpoint summaries)** are already in context.
- Result (afternoon `2026-11-01` telemetry): `100%` delegation trigger rate
  (`4/4` sessions) including immediately after the `12:30` UTC compaction,
  `0` post-compaction inline escapes.

### v3.5 — Sync/Async Background Flag + Outbound Anchor Packing

- Failures (`ses_e875f3f6d…`, afternoon): `k=3` held a `204k` spec in
  context but dispatched with only "权威设计依据=spec" — child re-read at
  `RAR = 6.292`, `DSR = 0.137` (vs `DSR = 1.00` on well-anchored dispatches
  in `ses_e828c9c15…`); `k=2` omitted `background: false` on a blocking
  follow-up and was surprised by the harness defaulting to background.
- Fix: Step 1 now mandates explicit `background: false` (blocking) vs
  `background: true` (orthogonal fan-out, consume notifications without
  polling) and packing premises + write boundaries + `path:line` anchors /
  spec excerpts already in context into every dispatch prompt.
- Targets: M2 `DSR > 0.80` (`RAR < 0.25`), M5 `C_poll = 0`, preserve v3.4's
  `100%` trigger rate and `0` `C_escape`.

## Part B — Grader & Telemetry Bug Archetypes

Six real measurement bugs fixed in `scripts/phoenix_subagent_usage.py`.
Re-check these whenever a scorecard looks damning — the meter breaks more
often than the prompt.

1. **Unanchored `<task>` quote false-async (Bug A, 2026-10-31)** — an actor
   report that merely *documented* `<task id="…" state="running">` deep in
   its body flipped the dispatch to `async`. Fix: async launch patterns are
   matched only at the start of the result; an outer
   `<subagent sessionID=… state=…>` terminal wrapper at position 0 wins.
2. **Silent `0s` empty resume read as UNKNOWN (Bug B, 2026-10-31)** — a
   warm resume on a saturated child (`274,198` tok) returned `0 chars` in
   `0s` and produced neither status nor verdict. Fix: sync dispatch with
   empty result AND zero child LLM activity ⇒ semantic `empty` +
   `FAILED_DISPATCH` so the parent rotates.
3. **Host-injection `T_report` pollution (Bug C, 2026-10-31)** — exact Δin
   between consecutive parent requests absorbed host injections
   (`<system-update>` catalog swaps, server-restart recovery prompts;
   `~5.4k` tok in `ses_e7063a72…` `k=1`), inflating NCR denominators.
   Fix: plausibility gate — exact > `2×` the char-based estimate by more
   than `1,500` tok ⇒ mark `⚠polluted`, fall back to the estimate.
4. **Active child booked as orphan waste (Blind Spot F, 2026-10-31)** —
   uncovered children whose last activity was within `300s` of the trace
   horizon (or `600s` with a silent parent) were classified
   `orphan_aborted_or_dropped`, inflating `C_rework`. Fix: `in_flight` /
   `in_flight_pending_parent` classes excluded from waste.
5. **JSON-escaped wire notifications + occurrence-count deltas (Bug 1,
   2026-11-01, commit `1eca441`)** — async completions misreported as
   `delivery: none / running / UNKNOWN` because (a) Phoenix truncates
   `input.value` at `60` KiB, (b) wrapper quotes are JSON-escaped inside
   serialized request bodies so plain-quote regexes never match, (c) the
   wire JSONL `seq` counter resets across host restarts and is a different
   namespace than spans' `opencode.wire.seq`, and (d) wrappers persist in
   every later frame, so a warm re-dispatch needs its OWN occurrence.
   Fix: scan the full parent wire record list in `ts` order; delivery =
   first post-launch frame whose occurrence count for the child exceeds the
   launch baseline; exact `T_report` via the timestamp-matched consume
   span. Recovered 5 afternoon dispatches to `native_notification /
   completed / HIGH_GAIN` (`NCR_ctx 10.41–77.84`).
6. **Dropped parent TOOL span -> false orphan (Bug 2, 2026-11-01, commit
   `1eca441`)** — the exporter dropped 3 parent `execute_tool subagent`
   spans while the children completed normally with their wrappers present
   in the parent wire log; the classifier booked `2,187,913` tok as
   `C_rework`. Fix: wire cross-check classifies such children
   `delivered_missing_tool_span`, excluded from waste (true orphan level:
   `57,217` tok — later reclassified as infra-kill by Bug H below).

7. **First-surviving-span R_k+1 binding across a host restart (Bug G,
   2026-11-03)** — sync `t_report_exact = Δin(R_k+1) − in(R_k) − out_vis(R_k)`
   bound R_k+1 to the first parent LLM span surviving after the dispatch end
   with no check that that span is the turn which consumed the wrapper. When
   a host restart drops the true consume span (`ses_e68dff2d…` k=1: wrapper
   frame `12:41:39`, next surviving span `23:44:17` — 11 h later), Δin
   silently absorbs restart-recovery prompts, `<system-update>` catalogs and
   abort notices (exact `3,672` tok for a `1,506`-char report; also
   `ses_e799a9c6…` k=12, where an intermediate pre-wrapper turn was bound
   instead). The Bug-C pollution gate usually catches the large case, but
   small cross-boundary deltas pass it while still measuring the wrong
   turn. Fix (wrapper-frame binding guard): locate the earliest post-
   dispatch wire frame with a NEW wrapper occurrence for the child (record
   `ts` order, JSON-escaped quotes); its consuming span is the last parent
   LLM span starting at/before it (+1 s slack). Mismatch ⇒ exact nulled,
   `restart_interleaved` friction flag, estimate stays effective, and
   `post_snippet` is no longer harvested from the post-restart span.

8. **Infra-killed child charged as C_rework (Bug H, 2026-11-03)** — a child
   aborted by a host/server restart leaves no completed wrapper and no
   covering TOOL span (Code-Mode `execute` launches never have one), so it
   fell to `orphan_aborted_or_dropped` once the recency windows expired —
   even though the parent's own wire log records the explicit kill notice
   `{"error":{"type":"aborted","message":"Tool execution interrupted
   (sessionID: …)"}`. Two such children (`57,579` + `57,217` tok, the
   latter previously cited as the "true orphan level" in Bug 2) were booked
   as agent-attributable rework waste. Fix: the orphan branch scans the
   parent wire for `Tool execution interrupted (sessionID: <sid>` and
   classifies such children `killed_by_host_restart` — consumed tokens are
   reported as infrastructure overhead, excluded from C_rework.

Related measurement refinement (same date): `Δpeak_self = Peak(R) − in(R_1)`
now separates sessions born with a huge first prompt (e.g. peak `204,458`
with `in(R_1) = 168,545` ⇒ `Δ = 35,913`, born large) from self-bloated ones
(`Δ = 266,860` on a `7,338`-token first turn) — check it before blaming a
prompt for `Peak(R)`.
