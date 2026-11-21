# Finacial — Copilot Instructions

Financial research and portfolio codebase. Python modules cover momentum screening, broker communication, and risk-parity research. Make surgical changes.

## DO NOT
- No mass formatting, recursive rewrites, or unrelated cleanup.
- No whole-file rewrite for a small change; use targeted patches.
- No edits to vendor/, third_party/, or generated *_pb2.py files.
- If a file has more than 50 lines, ask before deleting it.
- No dependency add/remove/upgrade without asking.
- No data generation, model training, Slurm, or long-running jobs unless explicitly requested.
- Do not claim a check passed unless it was actually run.
- Read no more than 40 lines per tool call.
- Do not show raw train*.log output; filter it with `rg -iv 'warning|key|Loading|Adjusted' <file> | sed '/^$/d'`.

## DO
- Before editing, run `git status --short`.
- Before making changes, run `git stash create` as a rollback snapshot; report its ref.
- After editing, run `git diff --stat` to check the scope.
- Locate code with `rg` first; read only the smallest relevant range.
- Batch independent shell commands in one call.
- For changed Python files, run `python -c "import <module>"` when import-safe.
- For behavior changes, run targeted tests when available.
- When testing the momentum pipeline end-to-end, use test mode (`momentum.py --max-securities 100`) instead of a full-universe K-line pull.
- In differentiable PyTorch paths, preserve autograd; use explicit `float64` and device for physics tensors where applicable.
- Keep importable code free of cluster-specific paths and import-time side effects.
- Preserve unrelated working-tree changes.

## OUTPUT
- Code only, no explanation, unless asked.
- Prefer bullets over paragraphs.
- Describe changes in prose with file and line references; do not include diff hunks.
- Keep rationale to three short sentences or fewer.
- No filler.

## Investment Context
- Unless I explicitly specify otherwise, interpret investment and trading questions as referring to my play-money allocation, managed within my leverage allocation—not my core risk-parity portfolio.
- Play money still has risk limits; do not assume unlimited loss tolerance or permission to increase leverage.

## SAFETY NET
- To undo this task's changes, apply the recorded snapshot with `git stash apply <ref>`. Do not overwrite unrelated working-tree changes.

## DONE
- [ ] Only task-relevant files changed.
- [ ] Unrelated working-tree changes preserved.
- [ ] Modified Python files pass syntax/import checks.
- [ ] Numerical and differentiable tensor paths preserve explicit dtype/device and gradients.
- [ ] No cluster-specific paths in importable code.
- [ ] No prohibited long-running job launched.
- [ ] Response is 15 lines or fewer.
