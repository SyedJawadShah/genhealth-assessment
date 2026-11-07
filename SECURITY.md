# Security

## Controls

Covered by tests (`tests/`):

- **Authentication.** Every order route needs a bearer token. Accounts come from registration
  (username 3–32 characters, password 12–128) or demo users seeded from configuration.
  - Passwords are hashed with scrypt.
  - Tokens are 256-bit random strings, stored only as HMAC-SHA256 digests keyed by `SECRET_KEY`.
    They expire after 12 hours and can be revoked with `POST /auth/logout`.
  - Changing a demo user's password revokes their tokens.
  - Login timing does not reveal which usernames exist.
- **Authorization.** Every order query is filtered by owner. Another user's order answers 404,
  the same as one that does not exist.
- **Rate limits.**
  - Registration: per IP.
  - Login: per IP and per username.
  - Uploads: per user (100 an hour) and globally (300 an hour, which bounds API spend).
  - Over-long login fields are refused before the limiter or hashing runs. Limiter keys are
    hashed and expired windows pruned.
- **Uploads.**
  - At most 20 MiB.
  - The type is decided by content: no `%PDF-` in the first 1 KiB gives
    `415 not_a_pdf`, "Only PDF files are accepted."
  - The client's filename and content type are ignored, and the PDF is not stored (only its
    SHA-256).
- **Untrusted model output.** Extracted values pass deterministic checks before they are saved.
  Malformed output becomes `502 extraction_failed`, never partial data or a 500. A page the
  document does not have marks the field `needs_review`.
- **Errors.**
  - Error bodies carry a code, the request ID and, for fixable upload errors, a fixed message;
    never internal details.
  - Validation errors (422) name the field but never echo the value. Unknown field names become
    `<unknown field>`.
  - Concurrent edits answer 409 `version_conflict`.
- **Logs.**
  - Event names, IDs, statuses and timings only; never query strings, request bodies, tokens or
    patient data.
  - Unhandled errors log the exception type and request ID only.
  - The `anthropic`, `httpx`/`httpx2` and `httpcore` loggers are held at WARNING and `pypdf` at
    ERROR, whatever `LOG_LEVEL` is.
- **Security headers** on every response: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, a restrictive
  `Content-Security-Policy` and `Cache-Control: no-store`. HSTS in production. The frontend at
  `/app` uses no inline scripts or styles.
- **Swagger UI at `/docs` is enabled in production** so reviewers can try the API. It shows
  only the API schema, and every data route still needs a token. Set `DOCS_ENABLED=false` to
  turn it off.
- **Request bodies over `MAX_REQUEST_BYTES`** (25 MiB) are refused with 413 before the app reads
  them.

Enforced by CI, hooks and the deploy:

- Secret scanning (gitleaks) and private-key detection on every commit and in CI.
- Dependency vulnerability audit (pip-audit) of the locked runtime dependencies.
- Ruff's bandit-derived security rules; strict mypy.
- The container runs as a non-root user and passes a smoke test with a read-only root
  filesystem and all capabilities dropped. The build context is an allowlist, so `.env`, `.git`
  and `data/` never enter the image.
- **Deploys are gated on readiness.** A deploy succeeds only when the new container reports the
  pushed commit on `/healthz` and `/readyz` answers 200: database reachable, Anthropic key set,
  token signing key set. Otherwise the previous version keeps serving.
- GitHub Actions use read-only tokens by default and are pinned to commit SHAs. Deploys use
  GitHub OIDC for short-lived AWS credentials; no AWS keys are stored in GitHub.

## Secrets

- **Where they live:** `SECRET_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL` and `DEMO_USERS` are
  stored in one GitHub secret, `APP_ENV_JSON`.
- **How they reach the container:** the deploy passes them to Lightsail through a private temp
  file and logs only the setting names.
- **Locally:** `.env` is git-ignored, and `.env.example` holds placeholders only.
- **What the app does if one is missing in production:** it serves `/healthz` but answers 503 on
  data routes and on `/readyz`, rather than falling back to local disk or a random key.

## Data sent to Anthropic

- **What:** each uploaded PDF, in full (base64), with a fixed system prompt. Nothing else is
  sent: no usernames, account IDs, tokens or other orders.
- **Where:** the Anthropic Messages API (model `claude-opus-5-5`), with a 50-second timeout and at
  most one retry.
- **Why:** reading scanned and faxed pages needs a vision model; there is no local OCR.
- **Terms:** Anthropic's commercial API terms apply. There is no business associate agreement
  (BAA), so the service must be used with synthetic data only until one is in place.

## Known limitations

- No BAA or HIPAA review; synthetic data only.
- Uvicorn trusts `X-Forwarded-For`, so per-IP limits can be dodged by spoofing it. Per-user,
  per-username and global limits still apply.
- Rate limits are in-process: per container, reset on restart.
- Open registration lets anyone create accounts; spend is bounded by the global upload cap and
  the Anthropic account's spend limit.
- The deploy role's Lightsail actions use `Resource: "*"`, and its trust policy accepts every
  repository owned by the GitHub account. Narrow it with `GITHUB_REPO` in
  `scripts/aws_bootstrap.sh`.
- Anyone with Lightsail access to the AWS account can read the deployed environment variables;
  Lightsail is not a dedicated secret store.
- `lightsailctl` is downloaded without checksum verification, and base images are pinned by
  tag, not digest.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner rather than in a
public issue.
