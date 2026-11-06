# service

A starting point for a Python web service: FastAPI, strict typing, tests, pre-commit hooks,
CI, a hardened container image and push-to-deploy on AWS Lightsail. It contains no product
logic, only a health check and an index page.

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
