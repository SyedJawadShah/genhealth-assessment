# Design

A small FastAPI service. It turns a faxed DME order PDF into a checked, per-user order record.

## Architecture

```
client ──HTTPS──▶ Lightsail load balancer ──▶ container (uvicorn + FastAPI, one process)
                                                 │            │
                                                 ▼            ▼
                                       Neon Postgres     Anthropic API
                                    (orders, users,      (claude-opus-5-5,
                                     token digests)       PDF in, JSON out)
```

| Module | Job |
| --- | --- |
| `main.py` | App factory, startup (migrations, demo users), error handlers |
| `auth.py` | Registration, OAuth2 password login, current-user dependency |
| `orders.py` | Order CRUD and upload; every query is filtered by owner |
| `extraction.py` | `Extractor` protocol; the Claude implementation |
| `validation.py` | Pure field checks, no I/O |
| `db.py`, `migrations/` | SQLAlchemy models, engine, Alembic migrations |
| `ratelimit.py` | In-process fixed-window limiter |
| `settings.py` | All configuration from environment variables |

I/O stays at the edges (routes, extractor, database). Validation is plain functions, and tests swap in a fake extractor.

## Data model

- `users`: id, unique lowercase username, scrypt password hash.
- `auth_tokens`: user id, HMAC-SHA256 digest of the bearer token (never the token), expiry.
- `orders`: UUID id, owner id, `status` (`ready` / `needs_review`), `source` (`upload` / `manual`), five field columns, `field_report` (JSON, one entry per field), `document_sha256`, timestamps, and a `version` for optimistic locking (PATCH with a stale `If-Match` answers 409).

The five columns are patient first and last name, date of birth, provider name and NPI. They hold a value only when it passed its check, with one exception: an NPI that fails its checksum (see below). `field_report` records each field's status, reason, source, extracted text and page. The PDF itself is not stored.

## Upload and extraction flow

1. Authenticate. Read at most `MAX_UPLOAD_BYTES + 1` bytes: too big is 413, empty is 422, and no `%PDF-` in the first 1 KiB is 415. The filename and client content type are ignored.
2. Rate limits: per user, plus a global hourly cap that bounds API spend.
3. Send the PDF to Claude as a base64 `document` block, so scans work without OCR. Structured output returns each field as `{value, page}`, copied as written, or `null` when absent, illegible or conflicting. The system prompt defines the ordering provider (the prescriber who signs the order) and says document text is data, not instructions.
4. API errors, refusals or truncated output answer 422/502/503 and save nothing.
5. Validate every field in code, then save the order.

## Validation rules and statuses

| Field | Rule | Normalization |
| --- | --- | --- |
| First / last name | Letters joined by single spaces, hyphens, apostrophes or periods; at most 100 characters | Whitespace collapsed |
| Provider name | Same as names | Leading "Dr." and trailing credentials (MD, D.P.M, ...) removed |
| NPI | 10 digits, Luhn check digit with the `80840` prefix | Spaces and hyphens removed |
| Date of birth | Parses as written (US month first; ISO; month names), a real date, not in the future, not before 1900 | Two-digit years are not expanded |

Statuses:
- `valid`: stored.
- `missing`: `null`, because the document lacks the field.
- `invalid`: `null`, with the extracted text kept in `field_report`.
- `invalid_checksum`: **stored as extracted**, with a warning in the response.
- `needs_review`: passed its check, but the model cited a page the PDF does not have; stored, with a warning.

An order is `ready` only when all five fields are `valid`.

**NPI checksum decision.** The official sample's NPI fails the Luhn check, so graders' documents probably carry fake NPIs too. A well-formed NPI that fails only its checksum is kept, flagged and surfaced as a warning, never corrected or dropped. Everything else that fails a check stays `null`: an impossible date can't be stored as a date, and storing it would be a guess. Manual create and `PATCH` use the same checks, except that a hard-invalid input is rejected with a 422 that names the field without echoing the value.

## Auth and per-user isolation

- `POST /auth/register` and `POST /auth/token` use the OAuth2 password flow, so Swagger's **Authorize** button works.
- Passwords are hashed with scrypt from the standard library. Login does a dummy hash for unknown users, so timing doesn't reveal which usernames exist.
- Tokens are 256-bit random strings, stored as HMAC digests keyed by `SECRET_KEY`, and expire after 12 hours. Logout revokes the token.
- Every order query includes `owner_id = current user`. Another user's order answers **404**, the same as a missing one.
- Rate limits cover registration (per IP), login (per IP and per username) and uploads (per user and global).

## Secrets and deployment

- **Settings:** all come from environment variables via `settings.py`. `.env` is git-ignored.
- **Production secrets:** `SECRET_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL` and `DEMO_USERS` live in one GitHub secret, `APP_ENV_JSON`, which the deploy passes to Lightsail without logging values.
- **Deploy path:** a push to `main` runs CI (lint, strict mypy, tests with coverage, pip-audit, image build and smoke test), then pushes the image to Lightsail and waits for `/healthz` to report the new commit and `/readyz` (database, extraction key, signing key) to answer 200. A failed health check leaves the previous version serving.
- **Startup:** runs migrations and seeds demo users. Production without a database or secret key still serves `/healthz` and answers 503 on data routes, rather than writing to the ephemeral disk. An unreachable database fails startup with a `database_unreachable` log line.

## Key decisions and trade-offs

- **Claude reads the PDF natively, no local OCR.** One call handles text and image-only pages, with about 3 seconds latency and about $0.03 per sample-sized fax. The trade-off is a dependency on an external API, and patient data leaves the service (see the README).
- **The model proposes, code decides.** Model output is untrusted, and every value passes deterministic checks before it is stored.
- **Synchronous upload.** It is simple and fits within the load balancer timeout for typical faxes. Very long documents would need a background job.
- **Opaque tokens over JWTs.** They are revocable and need no new dependency. The cost is one database lookup per request.
- **In-process rate limits.** Correct for a single container, and they reset on restart.
- **Swagger UI on in production**, so reviewers can try the upload. It can be turned off with `DOCS_ENABLED=false`.

## Known risks

- **Extraction accuracy on real-world faxes** is measured only on synthetic fixtures (50/50 fields on 10, including a degraded copy of the official sample and a 5-page fax) and the official sample. Handwriting, very poor scans or unusual layouts may yield `null` or wrong values. Statuses and `needs_review` exist so a person checks.
- **The model could still pick the wrong provider** when a form is ambiguous. Checksums catch typos, not wrong-person errors. An NPPES registry lookup would help.
- **Synthetic data only.** No business associate agreement (BAA) or HIPAA review has been done. Sending real patient data to Anthropic needs one.
- **IP-keyed limits can be bypassed.** Uvicorn trusts `X-Forwarded-For`, so IP-keyed limits can be bypassed by a client that spoofs it. Per-user, per-username and global limits do not depend on it.
- **Open registration** lets anyone create accounts. Spend is bounded by the global upload cap and the provider's spend limit.
