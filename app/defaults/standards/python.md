---
applies_to:
  languages: [python]
---
# Python standards

Applies to `.py` files that are not Spark jobs.

## Correctness
- No bare `except:` and no `except Exception` that swallows the error without logging or re-raising.
- Mutable default arguments (`def f(x=[])`) are a defect.
- Resources (files, connections, sessions) are opened in a `with` block or explicitly closed.
- Comparisons to `None` use `is` / `is not`.

## Structure
- Functions do one thing; flag functions longer than ~50 lines or with more than ~5 parameters.
- No duplicated logic that already exists elsewhere in the diff — say where it exists.
- Module-level side effects (I/O, network calls, credential reads at import time) are flagged.

## Typing and interfaces
- Public functions carry type hints on parameters and the return value.
- Prefer explicit return types over `Any`.
- Dataclasses or Pydantic models over dicts-of-unknown-shape crossing module boundaries.

## Errors and logging
- Log with the `logging` module, never `print`, in library or server code.
- Never log secrets, tokens, connection strings, or full request bodies.
- Error messages state what failed and what the caller should do about it.

## Tests
- New behaviour that is not covered by a test in the same pull request is an `info` finding,
  not a blocker, unless the repo's own standards say otherwise.
