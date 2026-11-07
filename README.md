# Order intake service

Durable medical equipment (DME) orders still arrive as faxed PDFs that someone retypes. This
service takes that PDF and does the typing. A signed-in user uploads an order (text or scanned,
one page or many). Claude reads it, and the service extracts five fields:

- the patient's first name, last name and date of birth
- the ordering provider's name and NPI

Every value is checked in code before it is saved, and each user sees only their own orders.
Nothing is guessed: a field the document lacks is `null` with a status, and a value that fails
a check is flagged for review.

- **Live:** https://service.hgn2de2jtj4ca.us-east-2.cs.amazonlightsail.com
- **Design:** [docs/DESIGN.md](docs/DESIGN.md) (architecture, data model, validation rules, trade-offs)
- **Security:** [SECURITY.md](SECURITY.md)

## How to try it

1. Open the browser UI at
   [/app](https://service.hgn2de2jtj4ca.us-east-2.cs.amazonlightsail.com/app/), or the API docs
   (Swagger UI) at [/docs](https://service.hgn2de2jtj4ca.us-east-2.cs.amazonlightsail.com/docs).
2. Create an account. Registration rules (the API enforces the same):
   - **Username:** 3 to 32 characters; letters, digits, `_`, `.` or `-`. Not case sensitive.
   - **Password:** at least 12 characters (at most 128).

   Two demo users (`demo1`, `demo2`) are available on request; credentials are sent
   separately. You can also register your own accounts. Each account has its own upload limit
   (100 an hour), and two accounts of your own make the isolation easy to test: upload as one,
   then request that order as the other and get 404.
3. Upload an order PDF. In `/docs`, click **Authorize**, enter the username and password, then
   use `POST /orders/upload`.
4. Read the result. Each field has a value (or `null`), a status, a reason and the page it came
   from. The order is `ready` only when all five fields are valid; otherwise it is
   `needs_review`, with any `warnings` listed.
5. Fix fields with `PATCH /orders/{id}`, which re-checks them. Send the order's `version` as
   `If-Match` to be protected against overwriting a newer edit. List, read and delete your own
   orders.

With curl:

```bash
URL=https://service.hgn2de2jtj4ca.us-east-2.cs.amazonlightsail.com
curl -s -X POST "$URL/auth/register" -H 'content-type: application/json' \
  -d '{"username": "reviewer-a", "password": "choose-a-long-password"}'
TOKEN=$(curl -s -X POST "$URL/auth/token" -d 'username=reviewer-a&password=choose-a-long-password' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s -X POST "$URL/orders/upload" -H "Authorization: Bearer $TOKEN" -F 'file=@order.pdf'
curl -s "$URL/orders" -H "Authorization: Bearer $TOKEN"
```

### Field statuses

| Status | Meaning | Stored value |
| --- | --- | --- |
| `valid` | Passed its check | The normalized value |
| `missing` | The document does not contain it | `null` |
| `invalid` | Failed a check (impossible date, digits in a name, NPI not 10 digits) | `null`; the extracted text is kept in `fields` |
| `invalid_checksum` | A 10-digit NPI that fails the CMS check digit | Stored as extracted, with a warning |
| `needs_review` | Passed its check, but the model cited a page the PDF does not have | Stored, with a warning |

**The official sample's NPI fails the CMS check digit.** 9182734556 does not pass the Luhn check
with the `80840` prefix. The service does not correct it or drop it. It stores the NPI exactly
as extracted, marks it `invalid_checksum`, adds a warning, and leaves the order in
`needs_review` for a person to confirm.

## Measured results

- **Live model, 10 synthetic fixtures: 50/50 fields correct**
  (`make score`; fixtures in [tests/fixtures/](tests/fixtures/)):
  - The official sample rasterized, rotated about 3° and speckled.
  - The official sample with the date of birth blacked out: `null`, `missing`, even though
    "AGE: 124" is visible.
  - A 5-page image-only fax with a cover sheet, an emergency contact, a referring physician and
    the ordering physician on the last page. The right patient and provider were found, with
    the correct page numbers.
  - A form listing three doctors, unusual labels ("Pt Name: LOVELACE, ADA", "D.O.B."), a scan
    with a fake NPI, a missing date of birth, and a two-patient packet.
- **Two-patient packet:** all fields come back `null` (`needs_review`) and are never mixed
  between the two patients.
- **Non-PDF uploads** (txt, docx, PNG, text renamed to `.pdf`): `415 not_a_pdf`, "Only PDF files
  are accepted." The type is detected from the content, not the filename, so a real PDF named
  `scan.png` is accepted.
- **Latency:** about 2–4 seconds per document.
- **Cost:** about $0.03 for the 4-page official sample (about 7,300 input and 120 output tokens
  at Claude Opus 5.5's $4 / $20 per million). It scales with page count.
- **Tests:** `make check` passes with 211 tests at 99% branch coverage. No test calls the network.

## Requirements

| # | Requirement (paraphrased) | Where | Proved by |
| --- | --- | --- | --- |
| 1 | Create, read, update and delete orders in a database | `src/app/orders.py`, `src/app/db.py`, `src/app/migrations/` (Postgres on Neon in production) | `tests/test_orders.py::test_get_list_update_delete`, `test_create_complete_order_is_ready`, `test_stale_update_is_rejected_with_409` |
| 2 | Upload a PDF and extract patient name and DOB and the provider's name and NPI, including scans and messy multi-page documents | `src/app/orders.py` (`upload_order`), `src/app/extraction.py` | `tests/test_upload.py::test_upload_creates_a_ready_order`, `tests/test_extraction.py::test_sends_the_pdf_natively_with_the_configured_model`, live `tests/test_fixture_scoring.py::test_live_extraction_score` (50/50) |
| 3 | Check values before saving and never guess a missing field | `src/app/validation.py`, `src/app/orders.py` (`_check_extracted`, page bounds) | `tests/test_validation.py`, `tests/test_upload.py::test_upload_keeps_flagged_npi_and_nulls_only_missing_fields`, `test_cited_page_outside_document_needs_review` |
| 4 | Authentication; each user sees only their own orders | `src/app/auth.py`, `src/app/orders.py` (`_owned_order`) | `tests/test_auth.py::test_protected_routes_need_a_valid_token`, `tests/test_orders.py::test_users_cannot_see_or_change_each_others_orders` |
| 5 | Publicly deployed | `Dockerfile`, `.github/workflows/deploy.yml`, `scripts/deploy_lightsail.sh`, `src/app/health.py` | `tests/test_health.py::test_readyz_ok_when_database_and_extraction_work`; each deploy must see the new commit on `/healthz` and a 200 from `/readyz` |

## Architecture

```
browser (/app) or API client
        │ HTTPS
        ▼
Lightsail load balancer ──▶ container: uvicorn + FastAPI (one process)
                                │                     │
                                ▼                     ▼
                        Neon Postgres           Anthropic API
                     (users, token digests,   (claude-opus-5-5 reads
                      orders, field report)    the PDF, returns JSON)
```

Each upload goes through these steps:

1. Size and content checks (`%PDF-` signature), then per-user and global rate limits.
2. Claude reads the PDF natively, so scans need no OCR. It returns each field as written, or
   `null`, with the page it came from, against a strict schema.
3. Deterministic checks: NPI check digit, real past date of birth, name shape, page within the
   document.
4. Save. Only validated values go into the order's columns; the per-field report keeps the
   status, reason, extracted text and page.

Model output is treated as untrusted throughout. Details: [docs/DESIGN.md](docs/DESIGN.md).

## How agents are used

**In development.**
- Claude Code built the service following [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md):
  acceptance checklist first, one small commit per change, and `make check` before every commit.
- Codex did two independent read-only reviews. Every finding was triaged. Fixes landed one per
  commit, each with a test that failed first; the rest are listed under known limitations.
- CI gates every push: lint, strict types, tests with a coverage floor, a dependency audit, and
  an image build plus smoke test. Then the deploy is verified on `/healthz` and `/readyz`.

**In the product.**
1. An AI reader: Claude Opus 5.5 reads the PDF natively and returns schema-validated fields,
   each with its page source.
2. Deterministic validators: NPI checksum, dates, names, page bounds.
3. Anything doubtful goes to `needs_review`. The service never guesses.

## Security notes

- **Accounts:** registration and token login. Passwords are hashed with scrypt; tokens are
  random, stored as HMAC digests and expire after 12 hours.
- **Isolation:** every order route needs a token and filters by owner. Another user's order
  answers 404.
- **Rate limits:** on registration, login and uploads (per user, plus a global cap on API spend).
- **Uploads:** at most 20 MiB, checked by content; the PDF itself is not stored, only its SHA-256.
- **Logs:** event names, IDs, statuses and timings only. No patient data, request bodies, tokens
  or exception messages; HTTP-client and PDF-parser loggers are capped.
- **Secrets:** they live in the `APP_ENV_JSON` GitHub secret, never in git.

**What is sent to Anthropic, and why.** Each uploaded PDF is sent in full (base64), with a fixed
system prompt, to the Anthropic Messages API, because reading scanned faxes needs a vision
model. Nothing else is sent: no usernames, account IDs or other orders. Anthropic's commercial
API terms apply. There is no business associate agreement (BAA), so use synthetic data only.
More in [SECURITY.md](SECURITY.md).

## Run, test and deploy

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), make, git; Docker for the image.

```bash
make setup           # install locked dependencies and git hooks
cp .env.example .env # then set ANTHROPIC_API_KEY to try uploads locally
make run             # http://localhost:8000 (SQLite in ./local.db)
make check           # lint, strict mypy, tests with coverage floor, all pre-commit hooks
make score           # live extraction score over tests/fixtures (calls the Anthropic API)
make docker-build    # production image; make docker-run starts it read-only
```

**Deploy:** push to `main`. GitHub Actions runs CI, builds the image and deploys it to AWS
Lightsail. The deploy fails unless the new container reports the pushed commit on `/healthz`
and answers 200 on `/readyz` (database reachable, extraction and token signing configured). A
failed deploy leaves the previous version serving.

Production settings come from the `APP_ENV_JSON` GitHub secret: `SECRET_KEY`,
`ANTHROPIC_API_KEY`, `DATABASE_URL` (Neon, direct connection) and `DEMO_USERS`.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`, `test` or `production` (production enables HSTS) |
| `DATABASE_URL` | SQLite locally, in-memory in tests, none in production | Postgres URL; without it, data routes answer 503 in production |
| `SECRET_KEY` | random per process outside production | Keys the HMAC that stores tokens; required in production |
| `ANTHROPIC_API_KEY` | unset | Without it, uploads answer 503 `extraction_unavailable` |
| `EXTRACTION_MODEL` / `EXTRACTION_EFFORT` | `claude-opus-5-5` / `medium` | Model and effort for extraction |
| `EXTRACTION_TIMEOUT_SECONDS` / `EXTRACTION_MAX_RETRIES` | `50` / `1` | Bounds on each API call |
| `DEMO_USERS` | `{}` | JSON object of demo username to password, created at startup |
| `DOCS_ENABLED` | `true` | Swagger UI at `/docs` and the OpenAPI schema |
| `MAX_UPLOAD_BYTES` / `MAX_REQUEST_BYTES` | 20 MiB / 25 MiB | Upload and request body limits |
| `UPLOAD_LIMIT_PER_HOUR` / `UPLOAD_GLOBAL_LIMIT_PER_HOUR` | `100` / `300` | Upload rate limits per user and overall |
| `LOGIN_LIMIT_PER_MINUTE` / `REGISTER_LIMIT_PER_HOUR` | `10` / `10` | Auth rate limits |
| `TOKEN_TTL_HOURS` | `12` | Token lifetime |
| `LOG_LEVEL`, `GIT_SHA`, `APP_NAME` | `INFO`, `unknown`, `Order intake service` | Logging, build version, display name |

### Layout

```
src/app/main.py          app factory, startup (migrations, demo users), error handlers, landing page
src/app/auth.py          registration, token login, current user
src/app/orders.py        order CRUD and upload, scoped to the current user
src/app/extraction.py    Claude PDF extraction behind an Extractor protocol; page count
src/app/validation.py    pure field checks
src/app/db.py            models, engine, migrations runner (src/app/migrations/)
src/app/health.py        /healthz (liveness) and /readyz (readiness)
src/app/ratelimit.py     in-process rate limiter
src/app/frontend.py      serves the browser UI in src/app/static/ at /app
tests/                   pytest suite; tests/fixtures/ holds synthetic PDFs and expected results
docs/DESIGN.md           design and trade-offs
```

## Assumptions

- The **ordering provider** is the prescriber who signs the order, not a referring physician or
  the author of a clinical note.
- Numeric dates are read as US `MM/DD/YYYY`. Two-digit birth years are flagged rather than
  expanded.
- A date of birth must be a real date, not in the future and not before 1900.
- Provider names are stored without a leading title or trailing credentials (`MD`, `D.P.M`), but
  never cut down to a single word, so surnames like "Do" are kept.

## Known limitations

- **One order per upload.** A packet with two patients returns all fields `null` (never mixed),
  but the second order is not split out, and the reason shown is `not_found_in_document`.
  Upload orders separately for now.
- **Accuracy is measured on synthetic fixtures.** They have typed text and mild skew. Real
  handwriting, very poor scans or pages rotated 90° may yield `null` or wrong values; statuses
  and `needs_review` exist so a person checks. Checksums catch typos, not a wrong person.
- **Deploy trust is broad.** The AWS deploy role trusts every repository owned by the GitHub
  account. Mitigation: re-run `scripts/aws_bootstrap.sh` with `GITHUB_REPO=genhealth-assessment`.
- **Spoofed `X-Forwarded-For` can dodge per-IP limits.** Per-user, per-username and global upload
  limits do not depend on client headers and still apply.
- **A database connection is held during the AI call.** Heavy concurrent uploads could exhaust
  the pool. Mitigation: open the session after extraction, or move extraction to a job queue.
- **Rate limits are per process** and reset on restart; fine for one container.
- **Not done, and why:**
  - No NPPES registry lookup (an extra network dependency in the request path).
  - No storage of original PDFs (less patient data held).
  - No password reset, roles or admin (out of scope for the time box).
  - No BAA or HIPAA review (synthetic data only).

## Next steps

- A second-pass verifier agent that cross-checks each field against the cited page image.
- Split multi-patient packets into one order per patient.
- Accept DOCX and TXT input alongside PDF.
- Asynchronous extraction with a job queue, which also frees the database connection.
- Narrow the AWS deploy trust to this repository.

## Pre-existing work

The repository started from my own template,
[fastapi-service-template](https://github.com/SyedJawadShah/fastapi-service-template), published
before the assessment. Commit `496bd7b` is the template import: FastAPI skeleton, health check,
security headers, logging, CI, container and Lightsail deploy scripts. Everything after that
commit is new work for this assessment.
