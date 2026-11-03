# Repository Guidelines — react-agent

## Project Structure & Module Organization

This repository is the evaluation and tuning harness for `Reasoner ↔ Actor` (`react`-mode) collaboration, closing the loop between live prompts, the context-economics specification, empirical benchmarks, and the telemetry analyzer:

- `prompts/orchestrator.md` & `prompts/fixer.md` — **Source-of-truth Reasoner and Actor prompts**, synced one-way from this repository to runtime state at `~/.config/opencode/oh-my-opencode-slim/react/`.
- `prompts/changelog.md` — **Falsifiable rationale log** mapping every prompt change to empirical telemetry triggers, Context Economics Inequality terms, and expected `M1–M10` shifts.
- `reports/react-mode-eval.md` — **Core evaluation specification (§1–§6)**: 4-family/9-primitive interaction model & Three-Cut Boundary (`§1`), Context Bandwidth Economics Inequality (`§2`), OTel GenAI SemConv telemetry contract (`§3`), observable parameter expansion with per-term `M1–M10` formulas and verdicts (`§4.1–§4.3`), and adapter extension slots (`§5`).
- `reports/react-mode-eval-benchmarks.md` — **Empirical benchmark scorecards** and regression analyses across real `ocg-obs` sessions.
- `scripts/phoenix_subagent_usage.py` — **Stdlib-only boundary analyzer** joining Phoenix GraphQL/SQLite spans with `~/.local/share/opencode/raw-requests/` wire JSONL via `opencode.wire.seq`.
- `skills/react-eval/` — **Closed-loop evaluation skill** (5-phase workflow, `references/tuning-casebook.md`, and `evals/evals.json`), symlinked at `~/.agents/skills/react-eval`.

## Prompt Iteration, Privacy & Deployment Workflow

To keep prompt changes falsifiable and prevent drift between version-controlled prompts and live OpenCode runtime state:

1. **Record the rationale (`prompts/changelog.md`)**: Pair every edit to `prompts/orchestrator.md` or `prompts/fixer.md` with a changelog entry stating:
   - **Trigger / Evidence**: observed failure mode, boundary index (`k`), and quantitative telemetry (or `design hypothesis` when pre-empirical).
   - **Target Economics / Boundary**: the targeted inequality term (`G_ctx`, `G_time` vs `C_tx`, `C_state`, `C_gov`, `C_risk`), boundary cut (`Cut A` / `Cut B` / `Cut C`), and physical mechanism.
   - **Expected Metric Impact**: which `M1–M10` metrics should move and in what direction.
2. **Keep public-repo telemetry sanitized**: Because this repository is published to a public remote (`github.com:YMoose/react-agent`), write all docs, changelogs, and commits using repo-relative or `~/...` paths, short session prefixes (`ses_e799a9c6…`), boundary indices (`k=5`), aggregate token/timing metrics, and the `YMoose <YMoose@users.noreply.github.com>` commit identity—omitting API keys, personal emails/names, machine-specific `/home/<user>` paths, and raw private user prompts.
3. **Isolate prompt commits**: Stage `prompts/*.md` with `prompts/changelog.md` in a dedicated commit (separate from script or report changes) with a concise summary referencing the changelog (`2d7bbd3` marks the v2.0 baseline):

   ```text
   prompts: <concise imperative summary>

   <1–2 line summary of the change and target metric; see prompts/changelog.md>.
   ```

4. **Deploy repo → live runtime and verify byte-identity** before running evaluation sessions:

   ```bash
   cp prompts/orchestrator.md prompts/fixer.md ~/.config/opencode/oh-my-opencode-slim/react/ \
     && diff -u prompts/orchestrator.md ~/.config/opencode/oh-my-opencode-slim/react/orchestrator.md \
     && diff -u prompts/fixer.md ~/.config/opencode/oh-my-opencode-slim/react/fixer.md
   ```

   Empty `diff` output confirms deployment. Always promote changes from `prompts/` to the live directory so git history remains authoritative.

## Verification & Ground-Truth Gates

Run focused checks for every change and verify against objective pass criteria:

```bash
# 1. Syntax gate after editing scripts/phoenix_subagent_usage.py
python3 -m py_compile scripts/phoenix_subagent_usage.py

# 2. Live evaluation run against Phoenix GraphQL (localhost:6006)
python3 scripts/phoenix_subagent_usage.py --project ocg-obs --chars-per-token 2.35 \
  --json-out /tmp/opencode/ocg-obs.json

# 3. Offline evaluation run when Phoenix server is down (snapshot db + wal + shm first)
python3 scripts/phoenix_subagent_usage.py --db /tmp/opencode/phx/phoenix.db \
  --project ocg-obs --chars-per-token 2.35
```

**Pass criteria**: boundaries resolve as `sync`/`async` from anchored result wrappers, empty non-running dispatches classify as `status="empty"` with `FAILED_DISPATCH`, and inflated exact `T_report` values carry the `⚠polluted` marker.

## Upstream Telemetry Fidelity (Ground-Truth Preservation)

Tuning prompts or grading formulas on corrupted observations invalidates the feedback loop. When an evaluation surfaces missing, truncated, or ambiguous telemetry from the upstream exporter (`~/yzh/obsystem/opencode-genai-obs`), wire JSONL, or host runtime:

1. **Separate analyzer bugs from upstream gaps** during the Phase 2 grader-integrity check: fix analyzer logic defects in `scripts/phoenix_subagent_usage.py` (with a `tuning-casebook.md` entry), and treat dropped spans, truncated attributes, or missing host reason codes as upstream data requirements.
2. **Escalate upstream gaps with a concrete spec**: File an upstream requirement report containing `path:line` root-cause evidence, sanitized `ses_…` prefixes, proposed attribute/event schema changes, and acceptance criteria (reference: `~/yzh/obsystem/react-eval-and-tool-span-loss-report.md`).
3. **Keep downstream fallbacks explicitly labeled**: Maintain stopgaps (such as wire JSONL recovery or structurally gated `killed_by_host_restart` classification) as documented fallbacks in `tuning-casebook.md` rather than synthesizing unverified host semantics.

## Conventions

- Write concise, imperative Markdown with lowercase `kebab-case` filenames.
- Format commit subjects as `<scope>: <imperative summary>` (`scripts: …`, `prompts: …`, `docs: …`), one logical change per commit.
- Stage files explicitly by path (`git add <path>`) so concurrent evaluation sessions editing `reports/` do not collide.
