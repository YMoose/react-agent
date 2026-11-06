# React-Agent Tuning Casebook (`v2.0 -> v3.5`)

Empirical catalog of prompt-iteration outcomes and analyzer (grader) bugs
observed in the `ocg-obs` project. Use it to recognize recurring failure
signatures quickly and to avoid re-introducing fixed anti-patterns.
All session identifiers are sanitized prefixes; no private paths or
transcripts.

Source-audit note (2026-11-03): historical numbers below are retained, not
recomputed in this documentation pass. Cross-session input differences are
descriptive, not controlled causal savings; usage differences and read-overlap
metrics are proxies. The 120k warning is policy, and an empty resume does not
prove context overflow.

## Part A — Prompt Iteration Casebook

### v2.0 (Baseline, commit `2d7bbd3`)

- `31`-line `orchestrator.md` + `23`-line `fixer.md`: Reasoner/Actor split,
  warm `sessionID` reuse across Scout -> Implement -> Verify, fixed 4-part
  Actor report contract.
- Benchmark (`ses_e730e619…` / `ses_e730e0ee…`): `5` sync dispatches,
  γ_warm `80%`, `NCR_char = 9.48:1`, `NCR_ctx = 12.62:1`, Reasoner peak
  `30,166` tok vs `195,749` inline baseline (`-58.5%` total input).

### v3.1 — High-Input Empty Resume -> Rotation Rule (Cut C)

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
- Failure (`ses_e828c9c15…`, v3.3 observed after compaction; reload causality unconfirmed):
  `0` subagent calls across `56` requests after `35` earlier dispatches —
  Reasoner self-exempted via three vectors: (1) temporal inversion (used the
  read allowance to *manufacture* the exemption precondition), (2)
  economic-formula self-override ("doing it inline is more reliable than
  paying dispatch + re-prefill"), (3) shell allowance creep (`npm test`,
  `npm run build`, `git push`). Peak hit `149,816` tok + mid-task
  compaction.
- Local design lesson: runtime prompts must state checkable
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

Fourteen real measurement bugs fixed in `scripts/phoenix_subagent_usage.py`.
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
   `delivery: none / running / UNKNOWN` because (a) the exporter truncates
   `input.value` at its configured budget (default `60` KiB), (b) wrapper quotes are JSON-escaped inside
   serialized request bodies so plain-quote regexes never match, (c) the
   wire JSONL `seq` counter resets across recorder restarts; the current
   exporter uses that same counter for spans' `opencode.wire.seq`, but
   neither is unique across restarts, and (d) wrappers persist in
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
   excluded from C_rework by that heuristic. This label alone does not prove
   infrastructure causality.
   *Revision (2026-11-03, Bug I–L commit)*: the needle match is now
   STRUCTURE-gated — the notice must sit in a
   `messages[*].content[*].type == "tool_result"` part with `is_error` true;
   a bare substring scan of the whole frame also matched plain-text
   discussions that merely quoted the notice (one eval session carried
   `125+` occurrences in its own reasoning) and user interrupts produce a
   byte-identical tool_result (no reason code). Children with a
   `state="cancelled"` wrapper in the parent wire are excluded (user
   interrupt, not restart). Residual ambiguity: an orphaned
   user-interrupted child without a cancelled wrapper is indistinguishable
   from a restart kill until the host emits a reason code — host semantics
   gap, tracked upstream, not a grader fix.
   *Follow-up (2026-11-03, string-content kill notices)*: the structure
   gate above only recognized OpenAI-style content-part lists, but the
   host emits Anthropic-style tool results as
   `{"role": "tool", "content": "<json-string>"}` — and the kill notice
   arrives exactly that way (`content` =
   `{"error":{"type":"aborted","message":"Tool execution interrupted
   (sessionID: …)"},"content":[]}`). Both children this fix targeted
   (`ses_e7063a72…` → `ses_e706375e7…`, 57,217 tok, and
   `ses_e68dff2d…` → `ses_e63550e6…`, 57,579 tok) therefore regressed to
   `orphan_aborted_or_dropped` and back into C_rework. Fix:
   `frame_kill_notice()` additionally accepts a string tool content only
   when it parses to a JSON object with a non-empty `error` object
   carrying the needle (the string twin of `is_error` + needle); bare
   text that merely quotes the notice still does not match, and the
   cancelled-wrapper exclusion is unchanged. Metric impact (ocg-obs
   snapshot): both children reclassified `killed_by_host_restart`;
   C_rework 114,796 → 0 tok, re-reported as "killed by host restart
   (infra overhead) ≈ 114,796 tok across 2 children"; all other triage
   stable (k=5 inline escape 12 tools / 59.4 min / +148,729 tok;
   Peak(A) 274,198 / 219,348 / 207,989; ⚠polluted 18 of 90 boundaries).

9. **Compaction ratchet never resets (Bug I, 2026-11-03)** — the async
   delivery scan claimed wrapper occurrences through a per-parent
   `wire_claimed` ratchet that only ever grew. Compaction removes older
   wrappers from the parent context, so the visible per-frame occurrence
   count DROPS (ses_e7988dd44…, child ses_e644872f8…: count 4 → compaction
   → 2 → new wrapper → 3), while the claim stayed at 4 ⇒
   `required = claimed + 1 = 5` was unreachable forever and the boundary was
   booked `delivery=none / running / UNKNOWN` even though its wrapper was
   sitting in the next frames. Fix: per-child compaction drops are detected
   between adjacent wire frames (count decrease); claims made before a drop
   are reset to the still-visible count, and a mid-window drop resets the
   in-flight `required`. Verified: ses_e7988dd44… k=21 now delivers its
   `state="cancelled"` wrapper (11:41:59) instead of `running`.

10. **Raw open-tag count ≠ parsed hits skips deliverable frames (Bug J,
    2026-11-03)** — the baseline/occurrence currency was
    `frame.count(open_tag)` (raw `str.count`), which also counts truncated
    remnants: an unclosed echo of an earlier wrapper inflates the count by
    one. Two failure shapes: (a) a frame whose raw count reaches
    `required` but whose parsed hits are one short was skipped entirely even
    though the hits list already contained the target child's own wrapper
    (ses_e7edada63… k=4: raw=4 vs parsed=3 — the parsed 3rd hit WAS k4's);
    (b) the inflated baseline shifted every later warm occurrence index by
    one (ses_e7edada63… k=2 bound k3's wrapper at 03:16:53 and k3 bound
    k4's at 03:47:40 — off-by-one against their true frames 03:00:07 /
    03:16:53). Fix: the ONLY claim currency is the parsed hit count —
    `subagent_wrappers` requires a closed `</subagent>` (both regex forms),
    occurrences are deduplicated per frame by wrapper identity
    (sid, state, description, whitespace-normalized report — a wrapper can
    appear twice in one frame, escaped tool_result copy + plain
    re-serialized copy), the parser now also digs into
    `tool_result.content` (sync dispatch results live there; the Fix A
    binding guard was blind to every sync delivery frame), and the escaped
    raw-body fallback no longer double-counts parsed message bodies. Raw
    counts are kept out of every skip decision.

11. **Warm-resume shared wrapper booked as undelivered (Bug K, 2026-11-03)**
    — one terminal wrapper of a child can cover several boundaries of the
    same parent (merged report: ses_e6422a2e7… k1+k2 — the wrapper opens
    "Both checks are complete" — and k3+k4). The occurrence index is claimed
    by the first boundary, so the later boundary's `required = claimed + 1`
    can never be met: `delivery=none / running / UNKNOWN` for a boundary
    whose child actually finished and whose report DID enter the parent
    context. Fix: when the child has terminal wrappers post-launch, every
    visible occurrence is already claimed, and no new one ever appears, the
    boundary is classified `delivery="shared_wrapper"` with an informative
    `SHARED_DELIVERY` verdict (not UNKNOWN, not FAILED, not in the rework or
    savings ledgers — the report was absorbed by the earlier boundary, so
    T_report/NCR stay unknown for it; protocol_state takes the shared
    wrapper's terminal state). The description match (first 40 chars,
    normalized) is recorded as `shared_desc_match` evidence: exact match
    means the wrapper carried this boundary's description; otherwise the
    merged-report reading (the ses_e6422a2e7… k2/k4 case: wrapper
    descriptions belong to k1/k3 while the report answers both).

12. **Cancelled terminal state never written back (Bug L, 2026-11-03)** —
    (a) the `hit["state"] → protocol_state` write-back existed but was
    unreachable behind the Bug I ratchet (fixed by 9); (b) collected
    `task_cancel` events only ever decorated `rework_tokens` and never
    touched `protocol_state`, so a user-interrupted child with no terminal
    wrapper stayed `running` forever (the host drops interrupted children
    without a wrapper unless the notification path emits a
    `state="cancelled"` one). Fix: each `task_cancel` event maps to the
    latest dispatch of its target child that was live at cancel time and
    still shows `running` with no resolved delivery → `protocol_state =
    "cancelled"` (semantic `empty` when no report; ledger fields untouched).
    Verified: ses_e7988dd44… k=21 (child ses_e644872f8…, user-cancelled)
    renders `cancelled`, via (a) once Bug I/Bug J let the wrapper through.

Related measurement refinement (same date): `Δpeak_self = Peak(R) − in(R_1)`
now separates sessions born with a huge first prompt (e.g. peak `204,458`
with `in(R_1) = 168,545` ⇒ `Δ = 35,913`, born large) from self-bloated ones
(`Δ = 266,860` on a `7,338`-token first turn) — check it before blaming a
prompt for `Peak(R)`.

13. **SQLite/wire timezone mismatch aborts offline evaluation (2026-11-06)** —
    the November 4 onward review failed before producing its scorecard:
    SQLite span timestamps contain UTC wall-clock values without offsets,
    while wire `ts` values include `Z`. `tso()` preserved this difference,
    so delivery matching raised `TypeError` when comparing naive and aware
    datetimes. Phoenix's timestamp adapter restores UTC on database reads;
    the analyzer's raw SQLite transport must do the same. Fix: attach UTC
    only to parsed timestamps lacking an offset, retaining explicit offsets.
    Stdlib regression tests first reproduced both cross-transport equality
    failures, then verified delivery ordering, elapsed time, offset handling,
    and unknown timestamps. This is a transport defect, not a prompt failure.

14. **Result-request placeholders hide polling (2026-11-06)** — five
    `task_status(result=true)` calls in `ses_e5a5fdee…` returned only
    `state: running`, but the analyzer counted them as report deliveries.
    Classification followed the request flag without checking the response.
    Fix: explicit running placeholders count as polls; real result pulls and
    diagnostic reads remain separate. A synthetic end-to-end regression
    through `build_economics()` failed before the fix and verifies all three
    categories afterwards. The November 4 onward window has six polls,
    one actual result pull, and one diagnostic read, versus one/six/one
    previously. This corrects measurement of already-observed behavior;
    it does not infer a new host lifecycle state.
