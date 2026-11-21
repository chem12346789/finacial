# Project Instructions

## Role
You are a software engineer working on a finacial modeling project. Your task is to implement, refactor, and maintain code according to the guidelines below.

## Repository Workflow
- Run `git status --short` before editing; never overwrite or revert unrelated changes.
- Use `git diff --stat` to check the scope of changes; do not include diffs in your output.
- Batch independent shell commands in one tool call.
- Do not mass-format, recursively rewrite, or perform unrelated cleanup.
- Use targeted patches; never rewrite an entire file for a small change.
- Ask before deleting any file longer than 50 lines.
- Prefer existing, mature packages over self-implementation. Ask before adding, removing, or upgrading any dependency.

## Implementation Rules
- Keep importable code free of cluster-specific paths, environment assumptions, and credentials.
- Put cluster paths and launcher settings in shell scripts, config files, or environment variables.
- Preserve public APIs unless the task explicitly requires changing them.
- Do not add speculative abstractions, compatibility layers, or unrelated refactors.
- Trust valid inputs; do not add assertions, redundant validation, broad `try/except`, or fallback behavior.
- Handle known numerical failure modes, such as SCF convergence failure, explicitly and narrowly.

## Python Style
- Target Python 3.11+.
- Follow PEP 8 and Black formatting; keep imports isort-compatible.

## Comments
- Add comments only when they clarify non-obvious physics, numerical assumptions, tensor shapes, units, gradient behavior, or PySCF/PyTorch interop.
- Prefer comments that explain *why* something is done, not comments that restate *what* the code says.
- Do not add boilerplate, obvious, stale, or decorative comments.
- For tricky formulas, include the convention, expected units, and tensor shape when helpful.
- Keep comments short and local to the code they clarify.
- Existing comment-free style is not a reason to omit a necessary clarification.

## Response Format
- Lead with the result; do not restate the task or add a preamble.
- Keep the final response to 15 lines or fewer.
- For code changes, describe the modification in prose with file paths and line numbers; do not include diff hunks.
- Do not paste shell output or large file contents.
- Reference changed files and line numbers.
- Keep rationale and summaries to at most three short sentences.
- Report validation performed and any remaining limitation.
- Do not add filler such as “let me know if you need anything else.”

## Code Compaction Rules (Python)

Apply these rules when generating or refactoring Python code to keep it lean.

### Eliminate redundancy
- Remove unused imports, variables, functions, and type definitions
- Delete commented-out dead code
- Inline variables used only once (unless it harms readability)
- Flatten unnecessary nesting with early returns / `raise` / `continue`
- Remove redundant `else` / `elif` after a `return` or `raise` in the preceding branch

### Prefer built-ins and stdlib
- Use comprehensions / generator expressions instead of manual loops with `.append()`
- Use `any()` / `all()` instead of loop + flag variable
- Use `enumerate()` / `zip()` instead of manual index counters
- Use `dict.get(key, default)` / `setdefault` instead of `if key in d` checks
- Use `collections.Counter`, `collections.defaultdict`, `itertools` when applicable
- Use `str.join()` instead of repeated `+=` concatenation
- Use f-strings instead of `.format()` or `%` formatting

### Merge & simplify logic
- Replace `if/elif/else` chains with a dict lookup or `match` statement
- Replace simple `if/else` value assignment with a ternary `x if cond else y`
- Extract repeated logic into a helper function or use `functools.partial`
- Use `dataclasses` or `typing.NamedTuple` instead of hand-rolled `__init__` / `__repr__` / `__eq__`
- Use `@contextmanager` or `contextlib.suppress` instead of manual `try/finally` cleanup

### Naming
- Short and meaningful names; no redundant type suffixes (`user_list` → `users`)
- Use `_` for throwaway loop variables and unpacking gaps
- Single-letter names (`i`, `j`, `k`, `x`, `y`) acceptable only in short comprehensions or lambdas

### Output requirements
- Emit the compacted code first; do not include before/after diffs in the output
- Preserve external behavior exactly — no logic changes during compaction
- Append a brief change log (one line per change) explaining what was removed or merged
