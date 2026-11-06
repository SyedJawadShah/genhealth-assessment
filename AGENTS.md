# Engineering standards

These rules apply to every change in this repository, whether made by a person or an AI agent.
They are generic: nothing here describes a specific product or task.

## Definition of done

`make check` passes: formatting, lint, strict type check, tests with the coverage floor, and
every pre-commit hook. CI runs the same checks plus a container build and smoke test. Never
bypass hooks (`--no-verify`), and never weaken a check, threshold or test to make it pass.

## Workflow

- Start from the task statement. Before writing code, write a short acceptance checklist and
  record assumptions in the README rather than guessing silently.
- Work in small, reviewable steps: one logical change per commit, with Conventional Commit
  messages (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `ci:`, `chore:`).
- Build a thin end-to-end slice first, then deepen it. Keep the main branch deployable.

## Code

- Python 3.12, fully typed. `mypy --strict` must pass. Avoid `Any` and `# type: ignore`;
  when one is unavoidable, explain why in a comment on the same line.
- Ruff-clean (lint and format). Match the style of surrounding code.
- Prefer the standard library and existing dependencies. Add a dependency only when it removes
  real complexity, with `uv add`, so it is locked in `uv.lock`.
- Configuration comes from environment variables through `app/settings.py`. No hardcoded
  secrets, hostnames, credentials or environment-specific paths.
- Keep modules small and single-purpose. Keep I/O at the edges and core logic pure, so it can
  be tested without a server, network or filesystem.
- Validate at boundaries and fail clearly. Never swallow exceptions silently.

## Testing

- Every bug fix starts with a test that fails without the fix.
- Every feature ships with tests for the main path, edge cases and failure cases.
- Unit tests make no network calls. Use synthetic test data only; never commit real personal
  data.
- Test behavior, not implementation details. Keep coverage above the configured floor.

## Security

- Treat all external input as untrusted: request parameters, headers, cookies, uploaded files,
  third-party API responses and model output.
- Authenticate every non-public route. Authorize every resource access against the current
  user; respond 404 for resources the user does not own, so their existence is not revealed.
- Uploaded files: enforce size limits, detect type from content rather than the filename or
  client MIME type, and never use client-supplied names as storage paths.
- Escape all output. Never mark user-controlled content as safe HTML.
- Use parameterized SQL. Never build shell commands from input; `shell=True` is not allowed.
- Set a timeout on every outbound call and bound every retry loop.
- Never log secrets, tokens, request bodies or personal data. Log event names, IDs, status and
  timing. Keep personal data out of URLs, filenames and exception messages.
- Error responses are generic and carry the request ID; details go to server logs only.
- Secrets live in environment variables or a secret manager, never in git. `.env` is
  git-ignored; `.env.example` holds placeholders only.
- Delete temporary files in `finally` blocks or with context managers.

## Documentation

- Keep README commands accurate: setup, run, test, deploy.
- Only claim behavior that tests cover. Record known limitations, what was not done, and why.
