# Prompt Iteration Changelog

This document records the empirical rationale, Context Economics Inequality
mappings, and expected metric shifts for every change to
`prompts/orchestrator.md` (Reasoner) and `prompts/fixer.md` (Actor). All
entries are sanitized of private paths, credentials, and raw user transcripts.

---

## v3.3 — `orchestrator.md`: Inline Known-Content Edits & Avoid Heavy Warm-Lane Drag

- **Date**: 2026-10-31
- **Files Changed**: `prompts/orchestrator.md` (`<Role>`, `<ReAct Loop>` Step 3)
- **Trigger / Evidence**:
  - In `ses_e6f8614a…` (`k=4, 5, 6`), Reasoner delegated three small,
    content-known documentation/prompt/git tasks to a warm Actor session
    (`ses_e6f7eb86…`) that had already accumulated `~185,000` input tokens of
    prior database/telemetry scouting history:
    1. `k=4` (copy baseline v2 prompts & commit, `50s`): $T_{\text{dispatch}} = 527$,
       $T_{\text{report}} = 976$ $\implies$ `1,503` tokens injected into Reasoner
       vs `~800–2,100` inline counterfactual ($\mathrm{NCR} \approx 0.7\text{–}1.4:1$);
       child re-prefilled `907,989` input tokens across `5` turns.
    2. `k=5` (author 100-line `AGENTS.md` & commit, `52s`): $T_{\text{dispatch}} = 1,046$,
       $T_{\text{report}} = 1,141$ $\implies$ `2,187` tokens injected into Reasoner
       vs `~1,500` inline counterfactual ($\Delta T_{\text{noise}} = -687$ tokens,
       **$\mathrm{NCR} = 0.69:1 < 1$**); child re-prefilled `741,988` input tokens
       across `4` turns.
    3. `k=6` (edit v3 prompts & commit, `71s`): $T_{\text{dispatch}} = 1,719$,
       $T_{\text{report}} = 1,369$ $\implies$ `3,088` tokens injected into Reasoner
       vs `~2,200` inline counterfactual ($\Delta T_{\text{noise}} = -888$ tokens,
       **$\mathrm{NCR} = 0.71:1 < 1$**); child re-prefilled `1,349,227` input tokens
       across `7` turns.
  - Across `k=4..6`, delegating content-known edits produced **negative net
    parent-window savings (`-2,278` tokens overall, $\mathrm{NCR} = 0.66:1$)**
    while burning **`2,999,204` (`3.0M`) child input tokens** in heavy warm-lane
    re-prefills and `173s` of serial wall-clock latency.
  - **Root Cause**: `orchestrator.md` v2/v3.1 only exempted *"single-block
    tweaks where the exact target lines are already in view"*, forcing Reasoner
    to delegate any file creation (`write`), multi-block doc edit, or accompanying
    `git commit` even when Reasoner had already authored the full text in mind.
- **Target Economics / Boundary**:
  - **1. Payload Inversion on $G_{\text{ctx}}$ ($\Delta T_{\text{raw\_return}} \to 0$)**:
    Unlike probe tools (`read`/`grep`/`shell`) whose inputs are tiny (`~20` tokens)
    and outputs are massive (`10k–100k` tokens), authoring tools (`write`/`edit`)
    have inputs equal to the deliverable text (`1,000+` tokens) and near-zero
    tool-result outputs (`"Edited ... (1 replacement)"` $\approx 15$ tokens).
    Delegating a content-known `write`/`edit` forces Reasoner to emit the full
    content in $T_{\text{dispatch}}$ AND absorb Actor's summary in $T_{\text{report}}$,
    inverting $\sigma_{\text{leak}} > 1$ ($\mathrm{NCR} < 1$) so $G_{\text{ctx}} < 0$.
  - **2. 3× Decode Duplication on $C_{\text{tx}}$**: Eliminates generating the
    same text three times via slow autoregressive Decode (Reasoner $T_{\text{dispatch}}$
    $\to$ Actor `write`/`edit` + $T_{\text{report}}$ $\to$ Reasoner user summary).
  - **3. Heavy Warm-Lane Drag on $C_{\text{state}}$**: Prevents routing small,
    self-contained chores through a warm child carrying $>100\text{k}$ tokens of
    unrelated scouting history ($\gamma_{\text{warm}} \cdot N_{\text{child\_turns}} \cdot \mathrm{Ctx}_{\text{warm}}$).
  - **Three-Cut Boundary**: Targets **Cut A** (Pre-Dispatch Decision: inline
    execution when $\Delta T_{\text{raw}} \to 0$, delegation when
    $\Delta T_{\text{raw}} \gg T_{\text{dispatch}}$).
- **Expected Metric Impact**:
  - **M1 (NCR & Compound Savings)**: Eliminate sub-unity ($\mathrm{NCR} < 1:1$,
    `MARGINAL_OVERHEAD`) dispatches on content-known doc/prompt/config edits,
    keeping all delegated dispatches in the $\mathrm{NCR}_{\text{ctx}} > 6:1$
    zone.
  - **M4 (Token Bill & Essay Friction)**: Cut child prefill waste on light
    follow-up turns by `> 80%` (avoiding multi-million token warm-lane drag)
    and reduce parent $T_{\text{dispatch}}$ friction share.
  - **Wall-Clock Latency**: Reduce small doc/prompt edit + commit turns from
    `50–71s` (delegated 3× decode) to `15–25s` (inline 1× decode).

---

## v3.2 — `fixer.md`: Adaptive Compact Reporting for Light Turns

- **Date**: 2026-10-31
- **Files Changed**: `prompts/fixer.md` (`<Report Contract>`)
- **Trigger / Evidence**:
  - In `ses_e730e619…` at dispatch boundary `k=4`, Reasoner reused a warm
    Actor for a lightweight verification check (`4` tool calls, `8,493` raw
    tool output chars, $\Delta\mathrm{Ctx} = 4,995$ tokens). Because `fixer.md`
    v2 mandated a fixed 4-section report regardless of turn weight, Actor
    emitted a `1,101`-token report, compressing $\mathrm{NCR}_{\text{char}}$
    down to `2.90:1` and $\mathrm{NCR}_{\text{ctx}}$ to `4.54:1` (`MODERATE_GAIN`).
  - Across `ses_e730e0ee…` (`k=1..5`), `6,608` of the child's `15,597` visible
    output tokens (`42%`) were spent generating report prose. Similarly, on
    short `3–4` tool turns in `ses_e6f8614a…` (`k=4, 5`), fixed 4-part reports
    cost `976` and `1,141` tokens respectively.
- **Target Economics / Boundary**:
  - **Inequality Terms**: Directly lowers right-hand boundary communication
    generation tax $C_{\text{tx}} = (T_{\text{dispatch}} + T_{\text{report}}) \cdot c_{\text{out}}$
    and reduces context leak ratio $\sigma_{\text{leak}} = 1 / \mathrm{NCR}$,
    thereby increasing left-hand noise isolation $\Delta T_{\text{noise}} = \Delta T_{\text{Actor\_raw}} - T_{\text{report}}$.
  - **Three-Cut Boundary**: Targets **Cut B** (Actor return generation) and
    **Cut C** (report token injection into Reasoner's context window).
- **Expected Metric Impact**:
  - **M10 (Adaptive Report Compactness)**: Cut $T_{\text{report}}$ on
    lightweight completed turns ($\le 4$ tool calls) from `~1,000–1,150`
    tokens to `< 300` tokens ($\le 6$ lines).
  - **M1 (NCR)**: Raise light-turn $\mathrm{NCR}_{\text{ctx}}$ above `8:1`
    (`HIGH_GAIN`).
  - **M4 (Handoff Essay Friction Share)**: Reduce $\mathrm{Friction}_{\text{child}}$
    below `25%` while retaining the full 4-part report on $>4$-tool or
    `Yielded`/`Blocked` turns to preserve `0` rework on **M3/M6**.

---

## v3.1 — `orchestrator.md`: Warm-Session Failure & Saturation Rotation Rule

- **Date**: 2026-10-31
- **Files Changed**: `prompts/orchestrator.md` (`<ReAct Loop>` Step 3)
- **Trigger / Evidence**:
  - In session `ses_e799a9c6…` at dispatch boundary `k=5` (03:47:40 UTC), the
    warm Actor session (`ses_e70bb348…`) had accumulated `129` LLM turns and
    `134` tool calls across `k=1..4`, saturating its context window at
    `274,198` input tokens.
  - When Reasoner attempted to resume that warm `sessionID` at `k=5`, the
    `subagent` tool returned an empty string (`0 chars`) in `0s`
    (`FAILED_DISPATCH`).
  - Because `orchestrator.md` v2 only instructed *"Reuse Warm sessionID Until
    Converged"* with no fallback branch for warm-session failure or context
    saturation, Reasoner abandoned delegation at Cut C and fell back to `80`
    minutes of inline file reading and editing (`40` `read` calls, `15` `edit`
    calls, `6` `write` calls, including a single turn generating `13,356`
    output tokens).
  - That inline escape bloated Reasoner's main context window from `97,519` to
    `175,941` tokens, triggered a mid-task context `compaction` at 05:06 UTC
    (`170,124 -> 39,409` tokens), and forced a full re-read of the target
    specification document (`10%` parent read redundancy), until Reasoner
    finally spawned a fresh cold Actor at `k=6` (`ses_e6f84033…`, restoring
    `NCR_ctx = 22.84:1`).
  - A related failure appeared in `ses_e7063a72…` at `k=0`, where a server
    restart severed an in-flight synchronous dispatch (`45.6k` orphaned child
    input tokens).
- **Target Economics / Boundary**:
  - **Inequality Terms**: Preserves left-hand context-isolation gain $G_{\text{ctx}}$
    and bounds right-hand state/rework costs $C_{\text{state}}$ and
    $C_{\text{risk}} = p_{\text{loss}} \cdot C_{\text{rework}}$.
  - **Three-Cut Boundary**: Targets **Cut C** (Post-Return Absorption after a
    failed or empty `subagent` return) and **Cut A** (next Pre-Dispatch
    Decision: forcing rotation to a fresh cold `CALL_SYNC` rather than
    escaping into noisy inline file reads).
- **Expected Metric Impact**:
  - **M3 (Boundary Terminal State & Rework)**: Recover from `FAILED_DISPATCH`
    in a single cold re-dispatch (`0` post-failure inline escape turns).
  - **M4 & M8 (Context Peak & Compaction Proxy)**: Keep Reasoner peak context
    $\mathrm{Peak}(R) < 40\text{k}$ tokens and eliminate parent `compaction`
    events (`0` compactions).
  - **M2 (Warm Hit Rate & Read Redundancy)**: Keep parent `redundant` re-read
    rate $< 5\%$ by preventing compaction-induced amnesia.

---

## v2.0 (Baseline) — Initial Two-Agent ReAct Split

- **Baseline Commit**: `2d7bbd3`
- **Files**: `prompts/orchestrator.md` (31 lines), `prompts/fixer.md` (23 lines)
- **Summary**: Established the core Reasoner (`orchestrator`) and Actor
  (`fixer`) split using OpenCode v2 tool vocabulary (`subagent`, `agent: "actor"`,
  `sessionID`, `task_cancel`), warm `sessionID` reuse across Scout -> Implement
  -> Verify cycles, and the 4-part Actor report contract (`Status`, `What Was Done`,
  `Files Touched`, `Issues & Observations`).
- **Baseline Benchmark (`ses_e730e619…` / `ses_e730e0ee…`)**: `5` synchronous
  dispatches ($\gamma_{\text{warm}} = 80\%$), $\mathrm{NCR}_{\text{char}} = 9.48:1$,
  $\mathrm{NCR}_{\text{ctx}} = 12.62:1$, Reasoner peak context `30,166` tokens
  vs `195,749` inline baseline (`-58.5%` total input tokens).
