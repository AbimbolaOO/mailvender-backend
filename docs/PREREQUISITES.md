# Prerequisites and learning resources

What you need installed, what you should know, and where to learn it, to work
on the Mailvender backend (and the parts of the frontend that talk to it).

Next steps: [GETTING_STARTED.md](GETTING_STARTED.md) to run it,
[ARCHITECTURE.md](ARCHITECTURE.md) for how it's built.

---

## 1. Tools to install

### Required

| Tool                                            | Version                    | Used for                                                      | Install                                                                  | Verify                                     |
| ----------------------------------------------- | -------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------ |
| **Docker Desktop** (includes Docker Compose v2) | Recent (2025+ recommended) | Runs Postgres, API, worker, Postfix, OpenDKIM, Mailpit, MinIO | https://docs.docker.com/get-started/get-docker/                          | `docker info` and `docker compose version` |
| **Node.js** (with npm)                          | 24.x                       | Running the studio, generating API types                      | https://nodejs.org/en/download (or [nvm](https://github.com/nvm-sh/nvm)) | `node --version`                           |
| **Git**                                         | any recent                 | Source control                                                | https://git-scm.com/downloads                                            | `git --version`                            |

> **Apple Silicon + old Docker Desktop:** Docker 20.10-era VMs crash on the
> newest `cryptography` wheels ("illegal instruction", exit 132). The project
> pins `cryptography<47` to cope. Updating Docker Desktop is still recommended.

### Needed for backend development (tests, running the API outside Docker)

| Tool       | Version                                    | Used for                                                       | Install                                                         | Verify                 |
| ---------- | ------------------------------------------ | -------------------------------------------------------------- | --------------------------------------------------------------- | ---------------------- |
| **Python** | 3.12 (pinned in `backend/.python-version`) | The API and worker                                             | https://www.python.org/downloads/ or `brew install python@3.12` | `python3.12 --version` |
| **uv**     | 0.5+                                       | Installs dependencies, runs tools (`uv sync`, `uv run pytest`) | https://docs.astral.sh/uv/getting-started/installation/         | `uv --version`         |

### Helpful

| Tool                                               | Used for                                                           | Link                                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| **DBeaver** (or TablePlus / pgAdmin)               | Browsing the database (`localhost:5434`)                           | https://dbeaver.io/download/                                                                           |
| **psql**                                           | Database shell                                                     | https://www.postgresql.org/download/ (or `docker compose exec postgres psql -U mailvender mailvender`) |
| **VS Code** + Python, ESLint and Docker extensions | Editing                                                            | https://code.visualstudio.com/                                                                         |
| **curl** or **HTTPie**                             | Calling the API from a terminal                                    | https://httpie.io/cli                                                                                  |
| **swaks**                                          | Sending test SMTP messages (e.g. fake bounces to `localhost:2525`) | https://github.com/jetmore/swaks                                                                       |

### Ports your machine must have free

`3000` studio · `8000` API · `5434` Postgres · `8025` Mailpit · `9000`/`9001`
MinIO · `2525` Postfix. If you run a local Postgres, it's probably on `5432`.
The project deliberately uses `5434` to avoid it.

---

## 2. Knowledge you'll need

You don't need to be an expert in everything below. The **Core** column is
what you'll use every day; **Deeper** helps when working on that area.

| Area                                             | Core | Deeper (when touching…)                                                               |
| ------------------------------------------------ | ---- | ------------------------------------------------------------------------------------- |
| Command line & Git                               | ✅   | —                                                                                     |
| Docker & Compose                                 | ✅   | `infra/`, `docker-compose.yml`                                                        |
| Python 3.12 & type hints                         | ✅   | —                                                                                     |
| HTTP & REST, JSON                                | ✅   | —                                                                                     |
| FastAPI & Pydantic                               | ✅   | `app/api/`                                                                            |
| SQL & PostgreSQL                                 | ✅   | `repositories/sql.py`, migrations                                                     |
| SQLAlchemy 2 & Alembic                           | ✅   | `models.py`, `alembic/`                                                               |
| pytest                                           | ✅   | `tests/`                                                                              |
| Repository / Unit of Work / outbox patterns      | ✅   | `services/`, `worker.py`                                                              |
| Web security (sessions, CSRF, hashing)           | —    | `security.py`, `deps.py`, `services/auth.py`                                          |
| Email delivery (SMTP, DKIM, SPF, DMARC, bounces) | —    | `services/sending.py`, `domains.py`, `feedback.py`, `infra/postfix`, `infra/opendkim` |
| AWS S3 & presigned uploads                       | —    | `services/assets.py`, `services/storage.py`                                           |
| TypeScript, React, Next.js                       | —    | `../mailvender` (frontend integration)                                                |
| Local-first sync, optimistic concurrency         | —    | `services/sync.py`, `../mailvender/lib/sync`                                          |

---

## 3. Learning resources

### Tooling

- **Docker:** [Get started](https://docs.docker.com/get-started/) ·
  [Compose overview](https://docs.docker.com/compose/) ·
  [Compose file reference](https://docs.docker.com/reference/compose-file/) ·
  [Networking in Compose](https://docs.docker.com/compose/how-tos/networking/)
- **Git:** [Pro Git book (free)](https://git-scm.com/book/en/v2)
- **uv:** [Documentation](https://docs.astral.sh/uv/) ·
  [Working on projects](https://docs.astral.sh/uv/guides/projects/)
- **Mailpit** (local inbox): https://mailpit.axllent.org/docs/
- **MinIO** (local S3): https://github.com/minio/minio ·
  [`mc` client](https://github.com/minio/mc)

### Python and the API stack

- **Python:** [Official tutorial](https://docs.python.org/3.12/tutorial/) ·
  [Type hints (typing)](https://docs.python.org/3.12/library/typing.html) ·
  [Protocols (structural typing)](https://typing.python.org/en/latest/spec/protocol.html)
- **FastAPI:** [Tutorial – user guide](https://fastapi.tiangolo.com/tutorial/) ·
  [Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/) ·
  [Dependencies with yield](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/) ·
  [Handling errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) ·
  [Testing & dependency overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/) ·
  [Middleware](https://fastapi.tiangolo.com/tutorial/middleware/)
- **Pydantic v2:** [Models](https://docs.pydantic.dev/latest/concepts/models/) ·
  [Settings management](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- **Uvicorn:** https://github.com/encode/uvicorn
- **OpenAPI:** [Specification](https://spec.openapis.org/oas/latest.html) ·
  [Swagger UI](https://swagger.io/tools/swagger-ui/)

### Database

- **PostgreSQL:** [Tutorial](https://www.postgresql.org/docs/current/tutorial.html) ·
  [Transactions & isolation](https://www.postgresql.org/docs/current/transaction-iso.html) ·
  [Explicit locking (row locks, advisory locks)](https://www.postgresql.org/docs/current/explicit-locking.html) ·
  [`SELECT … FOR UPDATE SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE) ·
  [`INSERT … ON CONFLICT`](https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT) ·
  [JSONB](https://www.postgresql.org/docs/current/datatype-json.html) ·
  [Triggers](https://www.postgresql.org/docs/current/plpgsql-trigger.html) ·
  [Sequences](https://www.postgresql.org/docs/current/sql-createsequence.html) ·
  [Partial indexes](https://www.postgresql.org/docs/current/indexes-partial.html)
- **SQLAlchemy 2.0:** [Unified tutorial](https://docs.sqlalchemy.org/en/20/tutorial/) ·
  [ORM quick start](https://docs.sqlalchemy.org/en/20/orm/quickstart.html) ·
  [Session basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html) ·
  [Savepoints (`begin_nested`)](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#using-savepoint)
- **Alembic:** [Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html) ·
  [Autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- **psycopg 3:** https://www.psycopg.org/psycopg3/docs/

### Architecture patterns used here

- **Repository** and **Unit of Work** (Martin Fowler):
  https://martinfowler.com/eaaCatalog/repository.html ·
  https://martinfowler.com/eaaCatalog/unitOfWork.html
- **Architecture Patterns with Python** (free online book; repository, UoW,
  service layer in Python): https://www.cosmicpython.com/book/preface.html
- **Transactional outbox:** https://microservices.io/patterns/data/transactional-outbox.html
- **Idempotency keys:** [Stripe's design](https://stripe.com/blog/idempotency) ·
  [Stripe API docs](https://docs.stripe.com/api/idempotent_requests)
- **Optimistic concurrency / revisions:** https://martinfowler.com/eaaCatalog/optimisticOfflineLock.html
- **Cursor (keyset) pagination:** https://use-the-index-luke.com/no-offset
- **Local-first software:** https://www.inkandswitch.com/local-first/
- **Structured logging & request IDs:** https://www.structlog.org/en/stable/why.html
- **Twelve-Factor App** (config in env, logs as streams): https://12factor.net/

### Security

- **OWASP cheat sheets:**
  [Password storage (Argon2id)](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) ·
  [Session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) ·
  [CSRF prevention (custom request headers)](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) ·
  [Authentication (generic error messages, rate limiting)](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) ·
  [Forgot password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html) ·
  [File upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) ·
  [Multi-tenant / authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
- **Cookies:** [MDN – Set-Cookie (HttpOnly, Secure, SameSite)](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie)
- **CORS:** [MDN – CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS)
- **argon2-cffi:** https://argon2-cffi.readthedocs.io/
- **cryptography – Fernet** (encrypted DKIM keys, unsubscribe tokens):
  https://cryptography.io/en/latest/fernet/

### Email delivery (the most specialised area – read before touching sending)

- **Big picture:**
  [Google – Email sender guidelines (bulk sender rules)](https://support.google.com/a/answer/81126) ·
  [Yahoo – Sender best practices](https://senders.yahooinc.com/best-practices/) ·
  [Learn DMARC (interactive walkthrough of SPF/DKIM/DMARC)](https://www.learndmarc.com/)
- **SMTP:** [RFC 5321](https://www.rfc-editor.org/rfc/rfc5321) ·
  message format [RFC 5322](https://www.rfc-editor.org/rfc/rfc5322) (998-character
  lines, headers) · [Python `smtplib`](https://docs.python.org/3.12/library/smtplib.html) ·
  [Python `email` package](https://docs.python.org/3.12/library/email.html)
- **DKIM:** [RFC 6376](https://www.rfc-editor.org/rfc/rfc6376) ·
  [OpenDKIM](http://www.opendkim.org/) ·
  [`opendkim.conf` manual](http://www.opendkim.org/opendkim.conf.5.html)
- **SPF:** [RFC 7208](https://www.rfc-editor.org/rfc/rfc7208)
- **DMARC & alignment:** [RFC 7489](https://www.rfc-editor.org/rfc/rfc7489) ·
  [dmarc.org overview](https://dmarc.org/overview/)
- **One-click unsubscribe (List-Unsubscribe-Post):** [RFC 8058](https://www.rfc-editor.org/rfc/rfc8058) ·
  `List-Unsubscribe` [RFC 2369](https://www.rfc-editor.org/rfc/rfc2369)
- **Bounces (DSN):** [RFC 3464](https://www.rfc-editor.org/rfc/rfc3464) ·
  status codes [RFC 3463](https://www.rfc-editor.org/rfc/rfc3463)
- **Complaints (ARF feedback reports):** [RFC 5965](https://www.rfc-editor.org/rfc/rfc5965)
- **VERP (per-recipient return paths):** https://cr.yp.to/proto/verp.txt
- **Postfix:** [Documentation index](https://www.postfix.org/documentation.html) ·
  [Basic configuration](https://www.postfix.org/BASIC_CONFIGURATION_README.html) ·
  [Milters (OpenDKIM integration)](https://www.postfix.org/MILTER_README.html) ·
  [Relay and access control](https://www.postfix.org/SMTPD_ACCESS_README.html) ·
  [`pipe` delivery (DSN forwarding)](https://www.postfix.org/pipe.8.html) ·
  [`main.cf` parameters](https://www.postfix.org/postconf.5.html)
- **DNS lookups in Python:** [dnspython](https://dnspython.readthedocs.io/)
- **Checking real domains:** [MXToolbox](https://mxtoolbox.com/SuperTool.aspx) ·
  [mail-tester](https://www.mail-tester.com/) ·
  [Google Postmaster Tools](https://postmaster.google.com/)
- **CAN-SPAM** (US rules: postal address, unsubscribe):
  https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business

### File storage

- **AWS S3:** [User guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html) ·
  [Uploading with presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html) ·
  [Bucket policies](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-policies.html) ·
  [CORS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/cors.html) ·
  [Block Public Access](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html)
- **boto3:** [Presigned URLs and POSTs](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html)
- **CloudFront with S3 (Origin Access Control):**
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html
- **Pillow** (image verification): https://pillow.readthedocs.io/

### Testing

- **pytest:** [Getting started](https://docs.pytest.org/en/stable/getting-started.html) ·
  [Fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) ·
  [monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html)
- **HTTPX** (TestClient underneath): https://www.python-httpx.org/

### Frontend side of the integration

- **TypeScript:** [Handbook](https://www.typescriptlang.org/docs/handbook/intro.html)
- **React:** [Learn](https://react.dev/learn) ·
  [`useSyncExternalStore`](https://react.dev/reference/react/useSyncExternalStore)
- **Next.js:** [Docs](https://nextjs.org/docs). This project uses **Next.js 16**,
  where middleware is called **proxy** (`proxy.ts`). Read the version-matched
  docs in `mailvender/node_modules/next/dist/docs/`, especially
  `01-app/03-api-reference/03-file-conventions/proxy.md` and the `rewrites` config.
- **openapi-typescript / openapi-fetch** (generated, typed API client):
  https://openapi-ts.dev/
- **IndexedDB:** [MDN guide](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API/Using_IndexedDB)
- **Web Crypto (UUIDv5 hashing):** [MDN `SubtleCrypto.digest`](https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/digest)

### Diagrams

- **Mermaid** (used in ARCHITECTURE.md): https://mermaid.js.org/intro/ ·
  [Live editor](https://mermaid.live/)

---

## 4. Suggested learning path for a new developer

1. **Day 1 – run it.** Install Docker, Node and uv. Follow
   [GETTING_STARTED.md](GETTING_STARTED.md) through the walk-through (sign up
   via Mailpit, verify a dev domain, send a test). Browse
   http://localhost:8000/docs.
2. **Day 1–2 – read the code top-down.** `app/main.py` → one router (e.g.
   `api/routes_accounts.py`) → its service (`services/accounts.py`) → the
   repository methods it calls (`repositories/sql.py`). Then read
   [ARCHITECTURE.md §3–5](ARCHITECTURE.md#3-code-structure-and-layering).
3. **Day 2 – run the tests** (`uv run pytest`), then read
   `tests/conftest.py` (fakes, helpers) and `tests/test_isolation.py`.
4. **Day 3 – the sending path.** Read the email-delivery resources above
   (at least Google's sender guidelines and Learn DMARC), then
   `services/sending.py`, `worker.py` and
   [ARCHITECTURE.md §7–9](ARCHITECTURE.md#7-sending-pipeline). Send a message and follow it in
   `docker compose logs -f worker postfix`.
5. **Day 4 – sync and the frontend.** `services/sync.py`, then
   `mailvender/lib/sync/engine.ts` and `adapters.ts`.
6. **Before production work:** [RUNBOOK.md](RUNBOOK.md) and
   [RETENTION.md](RETENTION.md).

---

## 5. Checklist before your first change

- [ ] `docker compose up` works and `/health/ready` returns ok
- [ ] You can sign up (via Mailpit), log in and send a test email locally
- [ ] `uv run pytest` passes (76 tests)
- [ ] `npm run lint && npm run typecheck && npm run build` pass in `mailvender/`
- [ ] You know where your change belongs: route (HTTP only) → service (rules) → repository (SQL)
- [ ] If you change models: you'll add an Alembic migration
- [ ] If you change the API: you'll regenerate `openapi.json` and `npm run api:types`
