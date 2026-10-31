# Repository Guidelines — react-agent

## Project Structure & Module Organization

This repository is the evaluation harness for the Reasoner↔Actor (react-mode)
collaboration research. It pairs the live prompts with the OCP theoretical
specification, the empirical benchmarks, and the telemetry analyzer that
scores every dispatch boundary.

- `prompts/orchestrator.md` & `prompts/fixer.md` — **source-of-truth Reasoner
  and Actor prompts**. These are the version-controlled baselines synced to
  `~/.config/opencode/oh-my-opencode-slim/react/`; the deployed copies there
  are runtime state, this repo is the history.
- `prompts/changelog.md` — **detailed rationale log for all prompt changes**,
  recording empirical triggers, Context Economics Inequality mappings, and
  expected metric shifts for every prompt iteration.
- `reports/react-mode-eval.md` — OCP theoretical specification: OTel GenAI
  SemConv telemetry contract + the 4-family/9-primitive model, the 3-layer
  Context Economics Inequality, the Three-Cut Boundary Model (Cut A/B/C), and
  the M1–M10 Metric Registry.
- `reports/react-mode-eval-benchmarks.md` — empirical benchmark results and
  per-session scorecards produced by the commands below.
- `scripts/phoenix_subagent_usage.py` — stdlib-only telemetry and wire-body
  boundary analyzer (Phoenix GraphQL/SQLite + `~/.local/share/opencode/raw-requests/`
  fallback joined via `opencode.wire.seq`).

## Prompt Change, Changelog & Git Commit Discipline (Mandatory)

1. **Detailed rationale in `prompts/changelog.md`**: Every change to
   `prompts/orchestrator.md` or `prompts/fixer.md` MUST be accompanied by a
   detailed entry in `prompts/changelog.md` covering:
   1. **Trigger / Evidence**: The observed failure mode, dispatch boundary
      (`k`), and quantitative telemetry findings (or `design hypothesis` if
      pre-empirical).
   2. **Target Economics / Boundary**: Which term of the Context Economics
      Inequality (`G_ctx`, `G_time` vs `C_tx`, `C_state`, `C_gov`, `C_risk`)
      and which boundary cut (`Cut A` / `Cut B` / `Cut C`) the change targets,
      including the underlying physical mechanism.
   3. **Expected Metric Impact**: Which metrics (`M1`–`M10`) are expected to
      move and in what direction.
2. **Privacy & Data Sanitization (去除隐私数据)**: Always strip sensitive or
   private data before writing to `prompts/changelog.md`, `AGENTS.md`,
   `reports/*.md`, or Git commit messages. Never record API keys, credentials,
   personal names/emails, machine-specific absolute home paths (use repo-relative
   paths or `~/...`), or raw private user prompts; keep only sanitized session
   prefixes (e.g., `ses_e799a9c6…`), boundary indices (`k=5`), and aggregate
   token/timing metrics.
3. **Concise Git commit messages**: Stage the updated `prompts/*.md` together
   with `prompts/changelog.md` in a dedicated commit (never mix with unrelated
   script or report edits). Because the full rationale lives in
   `prompts/changelog.md`, keep the Git commit message brief:

   ```text
   prompts: <concise imperative summary>

   <1–2 line summary of the change and target metric; see prompts/changelog.md>.
   ```

   Reference: `2d7bbd3` records the unmodified v2 baseline.
4. **Live sync & verification**: After committing, deploy the prompts and
   verify byte-identity before running any evaluation session:

   ```bash
   cp prompts/orchestrator.md prompts/fixer.md ~/.config/opencode/oh-my-opencode-slim/react/ \
     && diff -u prompts/orchestrator.md ~/.config/opencode/oh-my-opencode-slim/react/orchestrator.md \
     && diff -u prompts/fixer.md ~/.config/opencode/oh-my-opencode-slim/react/fixer.md
   ```

   (Empty `diff` output = deployed. Only sync in this repo→live direction;
   never overwrite the repo copy from the live directory without a documented
   `prompts/changelog.md` entry.)

## Verification & Analysis Commands

Run focused checks for whatever changed:

```bash
# script syntax gate (after any edit to scripts/phoenix_subagent_usage.py)
python3 -m py_compile scripts/phoenix_subagent_usage.py

# standard evaluation run (Phoenix GraphQL at localhost:6006; add
# --json-out /tmp/opencode/ocg-obs.json for machine-readable scorecards)
python3 scripts/phoenix_subagent_usage.py --project ocg-obs --chars-per-token 2.35

# offline variant when the Phoenix server is down (snapshot db + wal + shm first)
python3 scripts/phoenix_subagent_usage.py --db /tmp/opencode/phx/phoenix.db \
  --project ocg-obs --chars-per-token 2.35
```

Expect boundaries to resolve as `sync`/`async` from anchored result wrappers,
failed empty dispatches as `FAILED_DISPATCH`, and inflated `T_report` exacts
to carry the `⚠polluted` marker.

## Conventions

- Keep Markdown imperative and concise; lowercase kebab-case filenames.
- Commit subjects follow `<scope>: <imperative summary>` (e.g.
  `scripts: …`, `prompts: …`, `docs: …`); one logical change per commit.
- Reports may be edited by parallel sessions: stage explicitly by path and
  never `git add -A` in this repository.
