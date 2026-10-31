# Prompt Iteration Changelog

This document records the empirical rationale, Context Economics Inequality
mappings, and expected metric shifts for every change to
`prompts/orchestrator.md` (Reasoner) and `prompts/fixer.md` (Actor). All
entries are sanitized of private paths, credentials, and raw user transcripts.

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
