# Mailvender backend

The Python backend and infrastructure for Mailvender: accounts, cloud sync,
image hosting, authenticated sending, webhooks, and the admin API. The Next.js
studio is the sibling `mailvender/`, the staff dashboard `mailvender-admin/`.

```
mailvender-repo/
├── mailvender/            Next.js studio (customers)
├── mailvender-admin/      Next.js staff dashboard
├── backlog.md             what isn't built yet
├── todo.md, admin-todo.md acceptance criteria (delivered)
└── mailvender-backend/    this folder
    ├── backend/           FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL – API and worker
    ├── infra/             Postfix, OpenDKIM, S3 policy/CORS, backup timers, status/log scripts, Cloudflare Email Worker
    ├── docs/              Guides (below)
    ├── docker-compose.yml       local stack (the web apps run separately)
    └── docker-compose.prod.yml  production stack (Hetzner server)
```

## Documentation

| Read this | For |
| --- | --- |
| [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) | Plain-language setup, walk-through, how everything works, troubleshooting |
| [docs/PREREQUISITES.md](docs/PREREQUISITES.md) | Tools to install, knowledge needed, learning resources |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical architecture, diagrams, design decisions |
| [docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md) | Deploying mailvender.com step by step, and **redeploying** (step 15: backend, studio, admin, rollback) |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Production reference: environment, DNS, migrations, backups, key rotation, monitoring, alerts, blocklists, webhooks |
| [docs/ADMIN_SETUP.md](docs/ADMIN_SETUP.md) | The admin dashboard: local and production setup, role inboxes, writing email, day to day |
| [docs/WEBHOOKS.md](docs/WEBHOOKS.md) | For customers: events, payloads, verifying signatures, retries |
| [docs/RETENTION.md](docs/RETENTION.md) | How long each kind of data is kept; export, deletion and restore |
| [../backlog.md](../backlog.md) | Everything not built yet, and recommendations |

## Run it locally

```bash
cd mailvender-backend
docker compose up -d --build              # API, worker, Postgres, Postfix, OpenDKIM, Mailpit
cd ../mailvender && npm install && npm run dev   # the studio, in another terminal
```

Files are stored in the real AWS S3 bucket, so `.env` needs the `AWS_*`
values (see `.env.example`). One bucket holds both environments:

| Folder | Used by |
| --- | --- |
| `dev/` | `docker compose up` (this machine) |
| `prod/` | `docker compose -f docker-compose.prod.yml up` (production) |

Inside each: `images/<user id>/` (uploaded images) and `sheets/<user id>/`
(dataset copies).

| What | Where |
| --- | --- |
| Studio (`npm run dev` in `../mailvender`) | http://localhost:3000 – sign up, then open the verification email in Mailpit |
| Mailpit (every email sent locally) | http://localhost:8025 |
| API docs / contract | http://localhost:8000/docs · http://localhost:8000/openapi.json |

Migrations run automatically before the API accepts traffic. No real DNS is
needed:

* Images upload to the bucket's `dev/` folder.
* Mail goes worker → Postfix → OpenDKIM (DKIM-signed) → Mailpit, never the internet.
* A local domain can't publish DNS records, so mark it verified for testing:
  `docker compose exec api python -m app.cli dev-verify-domain example.test`
  (refused outside `ENVIRONMENT=development`).

The admin dashboard (http://localhost:3100) needs an admin:
`docker compose exec api python -m app.cli create-admin you@example.com`, then
see [docs/ADMIN_SETUP.md](docs/ADMIN_SETUP.md) part A. (The older operator API,
`/v1/operator/*`, needs `app.cli make-operator <email>`.)

Safari doesn't keep `Secure` cookies on `http://localhost`; use Chrome/Firefox
locally or set `SESSION_COOKIE_SECURE=false` for the `api` service.

## Tests

```bash
# Backend: needs PostgreSQL (docker compose up postgres creates mailvender_test)
cd backend && uv sync && TEST_DATABASE_URL=postgresql+psycopg://mailvender:mailvender@localhost:5434/mailvender_test uv run pytest

# Frontends (same for ../mailvender-admin)
cd ../mailvender && npm ci && npm run lint && npm run typecheck && npm test && npm run build
```

CI (`.github/workflows/ci.yml` in each project, also weekly):
- here: backend tests, both OpenAPI contracts current, image builds, and a
  `pip-audit` of the packages we ship;
- `mailvender/` and `mailvender-admin/`: lint, typecheck, tests, build, and
  `npm audit`.

Dependabot (`.github/dependabot.yml`) opens weekly update pull requests. Only
`mailvender/` is a git repository so far, so the other two run once they're
pushed (backlog 1.9).

## API contract

* `/docs` and `/openapi.json`. After changing the API:
  `cd backend && uv run python -m app.cli openapi > openapi.json`, then
  `cd ../../mailvender && npm run api:types` (`npm run api:check` verifies they
  match; the backend CI fails if `openapi.json` is stale). The admin API has its
  own contract: `openapi --admin > openapi-admin.json`, types in
  `mailvender-admin/`.
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
consumes the transactional outbox (sends, system and admin emails, webhooks,
exports) and runs scheduled jobs (DNS rechecks, platform DKIM, account health,
blocklists, alerts, retention, DKIM key publication). Details:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Cloud sync (frontend)

Editing stays local first (localStorage / IndexedDB); `../mailvender/lib/sync`
uploads changes in the background with revisions and polls for other devices'
changes. A stale write gets `409` with the current record; the client retries
its queued change against the current revision (last write wins) and shows a
notice. See the policy in the sync endpoints' OpenAPI descriptions.

More: [docs/RUNBOOK.md](docs/RUNBOOK.md) (production) ·
[docs/RETENTION.md](docs/RETENTION.md) (data lifecycle).
