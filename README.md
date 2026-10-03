# Mailvender backend

The Python backend and infrastructure for Mailvender: accounts, cloud sync,
image hosting and authenticated sending. The Next.js studio lives in the
sibling `mailvender/` folder.

```
mailvender-repo/
├── mailvender/            Next.js studio (frontend)
└── mailvender-backend/    this folder
    ├── backend/           FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL – API and worker
    ├── infra/             Postfix (outbound MTA + bounce intake), OpenDKIM, MinIO setup
    ├── docs/              Runbook, retention policy
    └── docker-compose.yml The whole stack, including the frontend (mounted from ../mailvender)
```

## Run it locally

```bash
cd mailvender-backend
docker compose up
```

| What | Where |
| --- | --- |
| Studio | http://localhost:3000 – sign up, then open the verification email in Mailpit |
| Mailpit (every email sent locally) | http://localhost:8025 |
| API docs / contract | http://localhost:8000/docs · http://localhost:8000/openapi.json |
| MinIO console | http://localhost:9001 (`mailvender` / `mailvender-secret`) |

Migrations run automatically before the API accepts traffic. No AWS
credentials or real DNS are needed:

* Images upload to MinIO (an S3-compatible store).
* Mail goes worker → Postfix → OpenDKIM (DKIM-signed) → Mailpit, never the internet.
* A local domain can't publish DNS records, so mark it verified for testing:
  `docker compose exec api python -m app.cli dev-verify-domain example.test`
  (refused outside `ENVIRONMENT=development`).

Make yourself an operator (for `/v1/operator/*`):
`docker compose exec api python -m app.cli make-operator you@example.com`.

Safari doesn't keep `Secure` cookies on `http://localhost`; use Chrome/Firefox
locally or set `SESSION_COOKIE_SECURE=false` for the `api` service.

## Tests

```bash
# Backend: needs PostgreSQL (docker compose up postgres creates mailvender_test)
cd backend && uv sync && TEST_DATABASE_URL=postgresql+psycopg://mailvender:mailvender@localhost:5432/mailvender_test uv run pytest

# Frontend
cd ../mailvender && npm ci && npm run lint && npm run typecheck && npm run build
```

CI: `.github/workflows/ci.yml` here runs the backend tests, checks the OpenAPI
contract is current and builds the images; `mailvender/.github/workflows/ci.yml`
lints, typechecks and builds the frontend.

## API contract

* `/docs` and `/openapi.json`. After changing the API:
  `cd backend && uv run python -m app.cli openapi > openapi.json`, then
  `cd ../../mailvender && npm run api:types` (`npm run api:check` verifies they
  match; the backend CI fails if `openapi.json` is stale).
* Errors: `{"error": {"code", "message", "fields"?, "details"?, "request_id"}}`.
* Lists: `?limit=` (≤ 100) and `?cursor=` → `{"items", "next_cursor"}`.
* Ids are UUIDs, timestamps UTC ISO 8601.
* Browser auth: HttpOnly `mv_session` cookie, `X-Account-Id` for the active
  account, `X-Requested-With: mailvender` on unsafe requests (CSRF). API keys:
  `Authorization: Bearer mv_…` on endpoints marked "API key: yes".

## Backend design

Route handlers → application services (`backend/app/services`) → repository
interfaces (`app/repositories/interfaces.py`). Only the SQLAlchemy
implementations (`app/repositories/sql.py`) touch the database; S3, DNS and
SMTP are behind interfaces too (faked in tests). The worker (`app/worker.py`)
consumes the transactional outbox and runs scheduled jobs (DNS rechecks,
account health, retention, DKIM key publication).

## Cloud sync (frontend)

Editing stays local first (localStorage / IndexedDB); `../mailvender/lib/sync`
uploads changes in the background with revisions and polls for other devices'
changes. A stale write gets `409` with the current record; the client retries
its queued change against the current revision (last write wins) and shows a
notice. See the policy in the sync endpoints' OpenAPI descriptions.

More: [docs/RUNBOOK.md](docs/RUNBOOK.md) (production) ·
[docs/RETENTION.md](docs/RETENTION.md) (data lifecycle).
