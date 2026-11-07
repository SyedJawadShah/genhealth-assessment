# Order intake service

A REST API for durable medical equipment (DME) order intake. A user uploads a faxed or scanned
order PDF; the service extracts the patient's name and date of birth and the ordering
provider's name and NPI, checks every value in code, and saves an order that only that user can
see. Values are checked in code and never guessed: a missing field is `null` with a status, and
an NPI that fails its check digit is kept but flagged.

Design, data model, validation rules and trade-offs: [docs/DESIGN.md](docs/DESIGN.md).

## Plan

### Acceptance checklist

1. REST API in Python.
2. Create, read, update, list and delete for an `Order`, persisted in a database (Postgres in
   production, SQLite locally and in tests).
3. PDF upload endpoint.
4. Extracts patient first name, last name and date of birth.
5. Extracts ordering provider name and NPI.
6. Works on unseen PDFs, including image-only scanned faxes and messy multi-page documents.
7. Extracted values are checked in code before they are saved.
8. Missing fields stay `null` with a status; nothing is guessed.
9. Every order route requires authentication (registration and token login, rate limited).
10. Users see only their own orders; other users' orders return 404.
11. Deployed at a public URL with a working upload endpoint and Swagger UI at `/docs`.
12. No secrets in git; commits made as the work progresses.
13. Two demo users seeded from configuration so reviewers can check isolation.
14. `make check` passes; tests cover main paths, edge cases and failures without network calls.
15. README states what data is sent to Anthropic, assumptions and known limitations.

### Core journey

1. Register or use a demo account; get a bearer token (Swagger's **Authorize** button works).
2. `POST /orders/upload` with a PDF: the file is checked, Claude extracts the fields, code
   validates them, and an order is created.
3. Read the per-field result: value, `valid` / `missing` / `invalid`, and a reason. The order
   is `ready` when all five fields are valid, otherwise `needs_review`.
4. `PATCH /orders/{id}` to fill in or correct fields; input is validated the same way.
5. List, read and delete your own orders.

### Assumptions

- The **ordering provider** is the prescriber who signs the order, not a referring physician
  or the author of a clinical note.
- Ambiguous numeric dates are read as US `MM/DD/YYYY`. Two-digit birth years are treated as
  ambiguous and flagged rather than expanded.
- An **NPI** is valid when it is exactly 10 digits and passes the Luhn check with the `80840`
  prefix. An NPI that fails only the check digit (the official sample's does) is stored exactly
  as extracted with status `invalid_checksum` and a warning in the response, never corrected or
  dropped. A value that is not 10 digits is `invalid`.
- `null` means the document does not contain the field. Values that fail other checks (an
  impossible date, a name with digits) are also `null` in the order, with status `invalid` and
  the extracted text kept in `fields` for review.
- A **date of birth** is valid when it is a real calendar date, not in the future and not
  before 1900.
- Provider names are stored without trailing credentials (`MD`, `D.P.M`, ...).
- The original PDF is not stored; only its SHA-256 and the extraction result are kept.

## Known limitations

- **Deploy trust is broad.** The AWS deploy role trusts every repository owned by the GitHub
  account. Mitigation: after grading, re-run `scripts/aws_bootstrap.sh` with
  `GITHUB_REPO=genhealth-assessment`.
- **Spoofed `X-Forwarded-For` can dodge per-IP limits.** Mitigation: per-user, per-username
  and global upload limits do not depend on client headers and still apply.
- **A database connection is held during the AI call.** Heavy concurrent uploads could exhaust
  the connection pool. Mitigation: open the session only after extraction, or move extraction
  to a background job.

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/), make, git
- Docker, for building the image locally
- For deploys: a GitHub repository and an AWS account

## Quick start

```bash
make setup           # install locked dependencies and git hooks
cp .env.example .env
make run             # http://localhost:8000
make check           # everything CI runs except the Docker build
```

## Commands

| Command | What it does |
| --- | --- |
| `make setup` | `uv sync --locked` and install the pre-commit and commit-msg hooks |
| `make fmt` | Format and apply safe lint fixes |
| `make lint` | Ruff lint and format check |
| `make typecheck` | `mypy --strict` over `src` and `tests` |
| `make test` | Pytest with branch coverage; fails below 85% |
| `make audit` | `pip-audit` of the locked runtime dependencies |
| `make hooks` | Every pre-commit hook against all files |
| `make check` | lint, typecheck, test and hooks |
| `make docker-build` / `make docker-run` | Build and run the production image (read-only, no capabilities) |

## Layout

```
src/app/main.py           app factory, generic error handler, index page
src/app/settings.py       configuration from environment variables
src/app/middleware.py     request IDs, request log, security headers
src/app/logging_setup.py  JSON logs to stdout
src/app/health.py         GET /healthz
tests/                    pytest suite
.github/workflows/        ci.yml (checks + image), deploy.yml (CI, then Lightsail)
scripts/                  aws_bootstrap.sh (one-time), deploy_lightsail.sh (every deploy)
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_NAME` | `service` | Display name |
| `APP_ENV` | `development` | `development`, `test` or `production`; production disables API docs and enables HSTS |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `GIT_SHA` | `unknown` | Build version, reported by `/healthz`; set at image build time |
| `MAX_REQUEST_BYTES` | `26214400` (25 MiB) | Largest request body accepted; larger ones get 413 before the app reads them |

## Quality gates

On every commit (pre-commit): whitespace and file hygiene, private-key detection, gitleaks
secret scan, ruff lint and format, strict mypy, `uv.lock` consistency, actionlint for
workflows, shellcheck for scripts, and Conventional Commit messages.

In CI on every push and pull request: all of the above, tests with the coverage floor, a
dependency vulnerability audit, and an x86_64 image build that is started read-only and
checked for a non-root user and a healthy `/healthz`.

Third-party GitHub Actions and hooks are pinned to commit SHAs. Dependabot proposes updates
weekly for Python packages, Actions and the base image.

## Deployment (AWS Lightsail)

A push to `main` runs CI, builds an x86_64 image, pushes it to the Lightsail container
service, creates a new deployment and waits until it is active. It then checks that the
public `/healthz` reports the commit just pushed. A deployment that fails health checks
leaves the previous version serving.

One-time setup:

1. With admin AWS credentials (or in AWS CloudShell), run
   `GITHUB_OWNER=<github-user> AWS_REGION=<region> scripts/aws_bootstrap.sh`. It creates the
   GitHub OIDC provider, a deploy role limited to Lightsail container actions, and the
   container service. **The container service is billed monthly until deleted.**
2. Set the three repository variables the script prints (`gh variable set ...`).
3. Push to `main`, or run the Deploy workflow manually. Until the variables exist, the deploy
   job is skipped rather than failed.

App settings and secrets: put any settings the app reads (session keys, demo accounts, API
keys) in one GitHub secret, `APP_ENV_JSON`, as a JSON object of strings. Each deploy merges it
into the container's environment; `APP_ENV` and `GIT_SHA` are always set by the deploy. The
deploy log lists only the setting names. For example, with a freshly generated session key:

```bash
gh secret set APP_ENV_JSON --body "{\"SECRET_KEY\": \"$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\"}"
```

Run `gh secret set APP_ENV_JSON` with no `--body` to paste the JSON without it appearing in
shell history. Changing the secret takes effect on the next deploy.

Rollback: Lightsail keeps the last 50 deployment versions; redeploy one from the console, or
revert the commit and push.

Lightsail notes: build images for x86_64 (CI does this, so ARM laptops are fine); container
disk is ephemeral, so keep no state there; the public endpoint is HTTPS only; deployment
environment variables are visible to anyone with Lightsail access to the account, so they
are not a secret store.

## Starting a project from this template

- **New repository:**
  `gh repo create <name> --private --template <owner>/<this-repo> --clone`, then
  `make setup`. Rename `name` in `pyproject.toml`.
- **Existing repository:** copy `.github/`, `scripts/`, `.pre-commit-config.yaml`,
  `Dockerfile`, `.dockerignore`, `Makefile`, `AGENTS.md`, `CLAUDE.md` and the `[tool.*]`
  sections of `pyproject.toml`, then adjust paths.
- **Disclose it.** Add a "Pre-existing work" section to the project README naming this
  template, its link and date, and stating that all product functionality was written
  afterwards.

## Security

See [SECURITY.md](SECURITY.md) for the controls in place and their limitations.
