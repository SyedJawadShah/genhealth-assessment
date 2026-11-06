# Security

## Controls in this template

Covered by tests (`tests/`):

- Security headers on every response, including 404s and 500s: `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`,
  a restrictive `Content-Security-Policy`, and `Cache-Control: no-store`. HSTS in production.
- Interactive API docs and the OpenAPI schema are disabled in production.
- Unhandled errors return a generic body with a request ID; exception details stay in logs.
- Validation errors (422) name the field and the kind of problem but never echo the
  submitted value, which may be personal data.
- Request bodies over `MAX_REQUEST_BYTES` are refused with 413 before the app reads them:
  a declared size is checked up front, and bodies without one are counted as they arrive.
- Request logs record the route template, status and timing, never query strings or raw
  paths, so identifiers in URLs stay out of logs.
- HTML output is escaped.

Enforced by CI and hooks:

- Secret scanning (gitleaks) and private-key detection on every commit and in CI.
- Dependency vulnerability audit (pip-audit) of the locked runtime dependencies.
- Ruff's bandit-derived security rules (`S`).
- The container runs as a non-root user and passes a smoke test with a read-only root
  filesystem and all capabilities dropped. The build context is an allowlist, so `.env`
  and `.git` never enter the image.
- GitHub Actions use read-only tokens by default, do not persist checkout credentials, and
  are pinned to commit SHAs.
- Deploys use GitHub OIDC for short-lived AWS credentials; no AWS keys are stored in GitHub.
  The deploy role can only call the listed Lightsail container actions.

## Known limitations

- No authentication or authorization is included; each application must add them.
- No rate limiting.
- The deploy role's Lightsail actions use `Resource: "*"`, and by default its trust policy
  accepts the `main` branch or `production` environment of any repository owned by
  `GITHUB_OWNER`. Set `GITHUB_REPO` in `scripts/aws_bootstrap.sh` to narrow it to one
  repository.
- App secrets are stored as the GitHub secret `APP_ENV_JSON` and passed to Lightsail at deploy
  time through a private temporary file, never in logs or command arguments. Once deployed,
  they are visible to anyone with Lightsail access to the AWS account, which is not a
  dedicated secret store.
- `lightsailctl` is downloaded from AWS's "latest" URL without checksum verification.
- Base images are pinned by tag, not digest.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner rather than in a
public issue.
