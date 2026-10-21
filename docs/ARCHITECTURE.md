# Mailvender backend – architecture

This document describes how the backend is built and why. It's written for
engineers working on the code. For setup and day-to-day use, see
[GETTING_STARTED.md](GETTING_STARTED.md). For operations, see
[RUNBOOK.md](RUNBOOK.md).

Diagrams use [Mermaid](https://mermaid.js.org/). GitHub, GitLab and most
editors render them.

---

## Contents

1. [Context and responsibilities](#1-context-and-responsibilities)
2. [System overview](#2-system-overview)
3. [Code structure and layering](#3-code-structure-and-layering)
4. [Request lifecycle](#4-request-lifecycle)
5. [Identity, sessions and tenancy](#5-identity-sessions-and-tenancy)
6. [Data model](#6-data-model)
7. [Sending pipeline](#7-sending-pipeline)
8. [Domains, DKIM and DNS verification](#8-domains-dkim-and-dns-verification)
9. [Bounces, complaints and suppression](#9-bounces-complaints-and-suppression)
10. [Abuse prevention and account health](#10-abuse-prevention-and-account-health)
11. [Asset hosting](#11-asset-hosting)
12. [Cloud sync](#12-cloud-sync)
13. [Data lifecycle](#13-data-lifecycle)
14. [Worker and scheduled jobs](#14-worker-and-scheduled-jobs)
15. [Cross-cutting concerns](#15-cross-cutting-concerns)
16. [Testing strategy](#16-testing-strategy)
17. [Deployment topology](#17-deployment-topology)
18. [Design decisions](#18-design-decisions)
19. [Known limitations](#19-known-limitations)
20. [Admin dashboard](#20-admin-dashboard)
21. [Webhooks](#21-webhooks)
22. [Reputation, alerts and sign-in security](#22-reputation-alerts-and-sign-in-security)

---

## 1. Context and responsibilities

Mailvender is a local-first email-template builder (Next.js) with datasets and
mail merge. The backend adds:

| Responsibility                                | Owned by                                      |
| --------------------------------------------- | --------------------------------------------- |
| Editing and rendering email HTML              | **Frontend** (stays in the browser)           |
| Identity, sessions, accounts, roles           | Backend                                       |
| Durable, cross-device storage of work         | Backend (sync) – frontend remains local-first |
| Authorised sending, DKIM, bounces, compliance | Backend + Postfix + OpenDKIM                  |
| Image hosting                                 | Backend + S3/CDN                              |
| Audit, health, data lifecycle                 | Backend                                       |
| Telling customers' systems what happened      | Backend (webhooks, §21)                       |
| Running the platform (staff)                  | Admin dashboard `mailvender-admin/` (§20)     |

**Out of scope:** mailboxes for customers (the platform receives only DSNs at
return-path addresses, and – for staff – mail to its own role addresses).
Campaign scheduling, analytics, billing and collaborative editing aren't built
yet: see [backlog.md](../../backlog.md).

---

## 2. System overview

```mermaid
flowchart LR
    subgraph Browser
        Studio["Next.js studio<br/>(localStorage + IndexedDB)"]
    end

    subgraph Edge["Frontend host"]
        Next["Next.js server<br/>proxy.ts + /api rewrite"]
    end

    subgraph Backend["Backend (this repo)"]
        API["FastAPI API<br/>(uvicorn)"]
        Worker["Worker<br/>(outbox + jobs)"]
        PG[("PostgreSQL")]
        Postfix["Postfix MTA"]
        DKIM["OpenDKIM milter"]
        Vol[/"Private volume<br/>KeyTable, SigningTable, keys"/]
    end

    S3[("S3 bucket")]
    CDN["CDN"]
    MX["Recipients' MX"]
    DNS["Public DNS<br/>(+ blocklists)"]
    FBL["Feedback-loop providers"]
    HOOK["Customers' webhook<br/>endpoints"]
    ADMIN["Admin app (Vercel)<br/>/api/v1/admin/*"]

    Studio -- "/api/* (cookie)" --> Next
    Next -- "rewrite" --> API
    Studio -- "presigned POST (image)" --> S3
    API <--> PG
    Worker <--> PG
    API -- "presign / verify / publish" --> S3
    CDN --> S3
    Worker -- "SMTP submit" --> Postfix
    Postfix <-- "milter" --> DKIM
    Worker -- "writes decrypted keys" --> Vol
    DKIM -- "reads" --> Vol
    Postfix -- "SMTP" --> MX
    MX -- "DSN to b-id@bounce.domain" --> Postfix
    Postfix -- "pipe → POST /internal/dsn" --> API
    FBL -- "signed ARF → /internal/feedback-loop" --> API
    API -. "TXT/MX lookups,<br/>link blocklist" .-> DNS
    Worker -. "rechecks, blocklists" .-> DNS
    Worker -- "signed POST (webhooks)" --> HOOK
    ADMIN -- "X-Mailvender-Admin-Secret" --> API
```

The admin app and the Cloudflare Email Worker that feeds the role inboxes are
shown in §20.

**Local development** uses the same S3 bucket under a `dev/` folder
(production uses `prod/`), delivers all mail to
Mailpit (`POSTFIX_RELAYHOST=[mailpit]:1025`), and runs everything except the
frontend with `docker compose`.

---

## 3. Code structure and layering

```{}
backend/
├── app/
│   ├── main.py                 # app factory, middleware, router registration
│   ├── config.py               # Settings (pydantic-settings, env vars)
│   ├── db.py                   # engine + session factory (UTC connections)
│   ├── models.py               # SQLAlchemy 2 mapped classes (the schema)
│   ├── errors.py               # ApiError hierarchy + the single error shape
│   ├── logging.py              # JSON logs + request/user/account contextvars
│   ├── security.py             # Argon2id, token hashing, Fernet, DKIM keygen, signed tokens
│   ├── pagination.py           # cursor pagination
│   ├── cli.py                  # make-operator, run-job, openapi [--admin], dev-verify-domain, platform-dkim,
│   │                           # create/reset/disable/list-admin(s), upload-log, upload-backup, move-dataset-content
│   ├── worker.py               # outbox consumer + scheduler + DKIM key sync
│   ├── api/
│   │   ├── deps.py             # DI: UoW, adapters, auth, account context
│   │   ├── schemas.py          # request/response models (the public contract)
│   │   ├── schemas_admin.py    # the admin contract (openapi-admin.json)
│   │   └── routes_*.py         # thin HTTP handlers: auth, accounts, sending, data, webhooks, public, admin
│   ├── services/               # application services (business rules)
│   │   ├── common.py           # AccountContext, audit, RateLimiter, email normalisation
│   │   ├── auth.py  two_factor.py  pwned.py  accounts.py  domains.py  dns.py  platform_dkim.py
│   │   ├── sending.py  mta.py  feedback.py  health.py  webhooks.py  reputation.py  emails.py
│   │   ├── assets.py  storage.py  sync.py  lifecycle.py
│   │   ├── admin_auth.py  admin_dashboard.py  admin_customers.py  admin_issues.py  admin_mail.py
│   │   └── inbound.py  dmarc.py  alerts.py
│   └── repositories/
│       ├── interfaces.py       # Protocols + UnitOfWork
│       └── sql.py              # SQLAlchemy implementations (the only persistence code)
├── alembic/                    # migrations 0001 (initial schema + trigger + sequence) … 0008 (webhooks)
├── tests/                      # pytest against real Postgres, fakes for S3/DNS/SMTP
├── openapi.json                # published contract (CI-checked)
├── Dockerfile  scripts/entrypoint.sh
```

### Layers

```mermaid
flowchart TB
    R["Route handlers<br/>(api/routes_*.py)"] -->|"call"| S["Application services<br/>(services/*.py)"]
    S -->|"depend on"| I["Repository interfaces + UnitOfWork<br/>(repositories/interfaces.py)"]
    S -->|"depend on"| P["Ports: ObjectStorage, DnsResolver,<br/>MailTransport (Protocols)"]
    I -. "implemented by" .-> SQL["SqlUnitOfWork / Sql* repos<br/>(repositories/sql.py)"]
    P -. "implemented by" .-> AD["S3Storage, SystemDnsResolver,<br/>SmtpTransport"]
    SQL --> DB[("PostgreSQL")]
```

Rules:

- **Route handlers** parse HTTP input, resolve auth through dependencies, call
  one service method and map the result to a response schema. No business
  logic.
- **Services** hold the rules (permissions beyond role checks, validation,
  state transitions, audit). They receive a `UnitOfWork` and adapters. They
  never import SQLAlchemy and never see a `Session`.
- **Repositories** (in `sql.py`) are the only code that builds queries. Each
  method maps to an intention (`count_recipients_since`, `claim`,
  `suppressed_among`) rather than generic CRUD.
- **Unit of work.** Services call `uow.commit()` at the end of a use case. Any
  exception before that leaves the transaction uncommitted, and the session
  closes, which rolls it back. Entities returned by repositories are SQLAlchemy
  objects whose changes are persisted by the commit (a pragmatic Unit of Work
  rather than a full domain/persistence split).
- **Ports for infrastructure** (`ObjectStorage`, `DnsResolver`,
  `MailTransport`) are `Protocol`s, swapped for in-memory fakes in tests via
  FastAPI `dependency_overrides` and the worker constructor.

---

## 4. Request lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant N as Next.js (/api rewrite)
    participant M as Middleware
    participant D as Dependencies (deps.py)
    participant H as Route handler
    participant S as Service
    participant U as UnitOfWork / repos
    participant DB as PostgreSQL

    B->>N: POST /api/v1/domains (cookie, X-Account-Id, X-Requested-With)
    N->>M: POST /v1/domains
    M->>M: assign request_id, reject bodies > 25 MB
    M->>D: resolve dependencies
    D->>U: open session (UoW)
    D->>U: sessions.get_by_token_hash(hmac(cookie))
    D->>D: CSRF header check, membership of X-Account-Id
    D->>H: AccountContext(account, role, user)
    H->>S: DomainService.register(ctx, name)
    S->>U: domains.get_live_by_name / add / dkim_keys.add / audit.add
    S->>U: commit()
    U->>DB: COMMIT
    S-->>H: DomainView
    H-->>M: 201 DomainOut
    M->>M: JSON log line (event=http.request, status, duration, user, account)
    M-->>B: response + X-Request-ID
```

Errors raised anywhere (`ApiError` subclasses, validation errors, unexpected
exceptions) are converted by handlers in `errors.py` to one shape:

```json
{"error": {"code": "…", "message": "…", "fields": [{"field","code","message"}], "details": {}, "request_id": "…"}}
```

---

## 5. Identity, sessions and tenancy

### Authentication

| Mechanism                                            | Used by                  | Details                                                                                                                                                                                 |
| ---------------------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session cookie `mv_session`                          | Browsers                 | 256-bit random token. Only `HMAC-SHA256(SECRET_KEY, token)` is stored. `HttpOnly`, `Secure`, `SameSite=Lax`, 14-day expiry, revocable. Checked on every request (immediate revocation). |
| API key `Authorization: Bearer mv_<lookup>_<secret>` | Servers                  | Per account, keyed hash stored, plaintext shown once. Accepted only on endpoints using the `CtxOrKey` dependency (send, read messages, domains, senders, suppressions).                 |
| `X-Internal-Token`                                   | Postfix DSN pipe         | Shared secret, constant-time compare.                                                                                                                                                   |
| `X-Mailvender-Signature`                             | Feedback-loop sources, Email Worker | `sha256=HMAC(body, per-source secret)`; `INBOUND_MAIL_SECRET` for `/internal/inbound-mail`.                                                                                  |
| Authenticator code (TOTP, RFC 6238)                  | Customers who turn it on | After the password: `401 totp_required` until `code` is sent; each code accepted once; 10 hashed recovery codes (§22).                                                                  |
| `__Host-mv_admin_session` + `X-Mailvender-Admin-Secret` | Admin app             | Separate admins, sessions and proxy key (§20).                                                                                                                                         |
| Signed recipient tokens                              | Unsubscribe / view links | Fernet (AES-CBC + HMAC with timestamp). Opaque, tamper-proof, expire after 90 days.                                                                                                     |

**CSRF:** cookie-authenticated unsafe requests must carry
`X-Requested-With: mailvender`. A cross-site HTML form can't set custom
headers, and cross-site `fetch` with one triggers a CORS preflight the API
rejects for unknown origins. Login and signup require the header too.

### Signup and verification

```mermaid
sequenceDiagram
    participant U as User
    participant A as API
    participant DB as DB
    participant W as Worker
    participant P as Postfix → inbox

    U->>A: POST /v1/auth/signup (email, password)
    A->>DB: rate-limit counters (own transaction)
    A->>DB: user(password=Argon2id) + email_token(hash, 48h) + outbox(system_email)
    A-->>U: 202 (same response whether or not the email exists)
    W->>DB: claim outbox event
    W->>P: verification email
    W->>DB: erase email body (it contained the token)
    U->>A: POST /v1/auth/verify-email (token)
    A->>DB: lock token, mark used, set verified,<br/>create personal account + owner membership + audit<br/>(one transaction)
```

Password reset follows the same pattern (single-use, 60 minutes, revokes all
sessions). Signed-in users can also change the password (`POST /v1/auth/password`,
current password required; every *other* session is revoked, and an email
tells them) and see and revoke their sessions (`GET /v1/auth/sessions`,
`DELETE /v1/auth/sessions/{id}`, `POST /v1/auth/sessions/revoke-others`). Neither signup nor reset reveals whether an address is registered.
Both refuse passwords found in breaches (§22).

### Tenancy

- Every account-owned table has `account_id`. The active account comes from
  the **authenticated context**: `X-Account-Id` (checked against
  `memberships`) for sessions, or the key's account for API keys. A body field
  is never trusted (there's a test for this).
- Repositories take `account_id` for every account-scoped read
  (`get(id, account_id)`). Another account's resource is indistinguishable
  from a missing one (**404**). Selecting an account you're not a member of is
  **403** `not_a_member`.
- Roles: `owner` and `member`. Owner-only actions: invitations, removing
  members, ownership transfer, API keys, sender identities, webhooks, export,
  deletion and restore.
  The last owner can't leave or be removed (409 `last_owner`).
- **Operators** are users with `is_operator` (set via CLI). They use
  `/v1/operator/*` and are not account members. Day-to-day operations moved to
  the admin dashboard (§20), whose admins aren't users at all.

---

## 6. Data model

```mermaid
erDiagram
    users ||--o{ sessions : has
    users ||--o{ email_tokens : has
    users ||--o{ memberships : has
    accounts ||--o{ memberships : has
    accounts ||--o{ invitations : has
    accounts ||--o{ api_keys : has
    accounts ||--o{ domains : owns
    domains ||--o{ dkim_keys : signs_with
    domains ||--o{ sender_identities : allows
    accounts ||--o{ messages : sends
    messages ||--o{ message_recipients : addressed
    message_recipients ||--o{ delivery_attempts : tried
    message_recipients ||--o{ delivery_events : reported
    accounts ||--o{ suppressions : blocks
    accounts ||--o{ assets : hosts
    accounts ||--o{ sync_records : stores
    accounts ||--o{ account_exports : exports
    accounts ||--o{ audit_events : logs
    accounts ||--o{ outbox_events : queues
    accounts ||--o{ webhook_endpoints : notifies
    webhook_endpoints ||--o{ webhook_deliveries : sent
```

| Table                                                 | Notes                                                                                                                                                     |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`                                               | Email unique (normalised lowercase). `default_account_id` = personal account.                                                                             |
| `accounts`                                            | `kind` personal/team, `status` active/deleted, sending limits, suspension fields, postal address.                                                         |
| `memberships`                                         | Unique (account, user), role.                                                                                                                             |
| `sessions`, `email_tokens`, `invitations`, `api_keys` | Hashes only; expiry/revocation timestamps.                                                                                                                |
| `domains`                                             | Unique **live** name (partial unique index where `disabled_at IS NULL`), so a deleted account frees the name. `record_status` JSON caches the last check. |
| `dkim_keys`                                           | Fernet-encrypted PKCS#8 private key, base64 SPKI public key, status pending/active/retired.                                                               |
| `sender_identities`                                   | Unique (account, email), status pending/verified/disabled.                                                                                                |
| `messages`                                            | Unique (account, idempotency_key) plus `request_hash` for replay detection.                                                                               |
| `message_recipients`                                  | One per recipient: status, attempts, timestamps. Indexed (account, created_at) for limits and health.                                                     |
| `outbox_events`                                       | Transactional outbox, indexed (status, available_at).                                                                                                     |
| `delivery_attempts` / `delivery_events`               | MTA hand-off attempts / post-delivery bounces and complaints.                                                                                             |
| `suppressions`                                        | Unique (account, email), reason and source.                                                                                                               |
| `sync_records`                                        | PK (account, collection, id), `revision`, `deleted`, `seq` (global sequence `sync_seq`), `parent_id` (versions → page).                                   |
| `audit_events`                                        | **Immutable**: a `BEFORE UPDATE OR DELETE` trigger raises.                                                                                                |
| `rate_limit_counters`                                 | PK (key, window_start).                                                                                                                                   |
| `job_runs`                                            | Last run/error per scheduled job.                                                                                                                         |
| `webhook_endpoints`, `webhook_deliveries`             | Customer URLs (secret Fernet-encrypted, events, failure counters) and each event sent with the last attempt's outcome (§21).                               |
| `platform_dkim_keys`                                  | Mailvender's own DKIM key(s), not owned by any account.                                                                                                   |
| `system_state`                                        | Small JSON state kept by jobs: blocklist results, alerts already sent (§22).                                                                              |
| Admin tables                                          | `admin_users`, `admin_sessions`, `admin_issue_states`, `admin_mails`, `admin_mail_attachments`, `inbound_messages`, `dmarc_report_rows`, `user_activity_days` (§20). |

All ids are UUIDs (v4 server-side; the frontend derives v5 UUIDs for synced
records). All timestamps are `timestamptz`, and connections use `timezone=utc`.

Migrations are Alembic: `0001_initial_schema` (all original tables, the
`sync_seq` sequence and the audit trigger), `0002` dataset content in S3,
`0003` platform DKIM, `0004` admin, `0005` login deletion, `0006` admin mail,
`0007` customer two-factor + `system_state`, `0008` webhooks. A test asserts that the migrated
schema equals the models (`compare_metadata == []`) and that downgrade and
upgrade round-trip.

---

## 7. Sending pipeline

### Accepting a send

```mermaid
flowchart TB
    A["POST /v1/messages<br/>+ Idempotency-Key"] --> B{"Key seen for<br/>this account?"}
    B -- "yes, same hash" --> R1["202: original message"]
    B -- "yes, different hash" --> R2["409 idempotency_key_reused"]
    B -- no --> C["SELECT account FOR UPDATE<br/>(serialises sends per account)"]
    C --> D{"active and<br/>not suspended?"}
    D -- no --> R3["403 account_suspended / account_inactive"]
    D -- yes --> E["Validate everything, collect field errors:<br/>recipients (syntax, duplicates, count ≤ 50), subject (one line),<br/>html ≤ 1 MB, links not on Spamhaus DBL, approved sender on verified<br/>domain of this account, bulk rules, suppressions per recipient"]
    E -- errors --> R4["422 with fields – nothing queued"]
    E -- ok --> F{"hourly/daily limit OK?<br/>(count recipients under the lock)"}
    F -- no --> R5["429 sending_limit_exceeded<br/>(+ violation counter → maybe suspend)"]
    F -- yes --> G["INSERT message, recipients (queued),<br/>one outbox event per recipient"]
    G --> H["COMMIT (single transaction)"]
    H --> R6["202 Accepted + message id"]
```

**Bulk rules** (`category=bulk`): a usable text part; `{{unsubscribe_url}}`
in the HTML; the account's postal address set and present (literally or as
`{{physical_address}}`); the domain `bulk_eligible` (DMARC present, SPF
alignment relaxed).

### Delivering

```mermaid
sequenceDiagram
    participant W as Worker
    participant DB as PostgreSQL
    participant PF as Postfix
    participant OD as OpenDKIM

    W->>DB: SELECT … FROM outbox_events WHERE pending AND due<br/>FOR UPDATE SKIP LOCKED LIMIT 20
    loop each event (savepoint)
        W->>DB: lock recipient
        alt account suspended/deleted, domain not verified, or address now suppressed
            W->>DB: recipient → failed / suppressed
        else
            W->>W: build MIME: per-recipient unsubscribe/view links (Fernet),<br/>List-Unsubscribe + List-Unsubscribe-Post (bulk),<br/>Message-ID, X-Mailvender-Recipient-Id
            W->>PF: SMTP, MAIL FROM b-‹recipient id›@bounce.‹domain›
            PF->>OD: milter
            OD-->>PF: DKIM-Signature d=‹domain› s=‹selector›
            alt 250
                W->>DB: attempt(accepted), recipient → accepted_by_mta
            else 4xx / connection error
                W->>DB: attempt(temporary_failure), event.available_at += backoff
                Note over W,DB: 30 s × 2^(n-1), max 1 h, failed after 6 attempts
            else 5xx
                W->>DB: attempt(permanent_failure), recipient → failed
            end
        end
    end
    W->>DB: COMMIT
```

Recipient states:

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> accepted_by_mta: Postfix 250
    queued --> failed: 5xx / retries exhausted / account or domain no longer allowed
    queued --> suppressed: suppressed after queueing
    accepted_by_mta --> bounced: DSN (failed)
    accepted_by_mta --> complained: feedback-loop report
```

Delivery is **at least once**: if the worker crashes after Postfix accepted
but before the commit, the message is resent. That trade-off is accepted
rather than risking lost mail.

Each transition to `accepted_by_mta`, `failed` or `suppressed` (and later
`bounced` and `complained`, and unsubscribes) also queues a webhook event in the
same transaction (§21).

---

## 8. Domains, DKIM and DNS verification

On registration the API normalises the name (IDNA, label checks) and enforces
global uniqueness among live domains without revealing the owning account
(`409 domain_unavailable`). It generates a **2048-bit RSA key**, stores the
private key Fernet-encrypted, and creates a unique selector (`mvYYYYMM…`).

Required records (`DomainService.expected_records`):

| Key              | Type | Name                                 | Value                                      | Required |
| ---------------- | ---- | ------------------------------------ | ------------------------------------------ | -------- |
| `dkim`           | TXT  | `<selector>._domainkey.<domain>`     | `v=DKIM1; k=rsa; p=…`                      | yes      |
| `return_path_mx` | MX   | `bounce.<domain>`                    | `10 <RETURN_PATH_MX_HOST>`                 | yes      |
| `spf`            | TXT  | `bounce.<domain>`                    | `v=spf1 include:<SPF_INCLUDE_DOMAIN> -all` | yes      |
| `dmarc`          | TXT  | `_dmarc.<domain>`                    | `v=DMARC1; p=none; …; adkim=r; aspf=r`     | for bulk |
| `dkim_next`      | TXT  | `<new selector>._domainkey.<domain>` | during rotation                            | no       |

Each record is reported `verified`, `missing` or `mismatched`. Lookup failures
count as missing, so a DNS outage never verifies a domain.

**Alignment:** DKIM signs with `d=<domain>`, which always aligns with the From
domain. SPF is evaluated on the envelope sender `bounce.<domain>`, which
aligns under **relaxed** alignment. A DMARC record with `aspf=s` makes the
domain not bulk-eligible, and the API says why.

```mermaid
stateDiagram-v2
    [*] --> pending: register
    pending --> verified: all required records valid
    verified --> pending: hourly recheck finds a record missing/changed
    pending --> disabled: disable
    verified --> disabled: disable / account deletion
    disabled --> pending: enable
```

**Key distribution to OpenDKIM.** The worker's `dkim_key_sync` job (every
30 s) decrypts the active keys of verified domains, plus the platform key for
Mailvender's own domain (`platform_dkim_keys`, not owned by any account),
onto a private volume shared only with OpenDKIM
(`keys/<selector>.private`, `KeyTable`, `SigningTable` written atomically) and
bumps a `generation` file. OpenDKIM's entrypoint watches it and sends
`SIGUSR1` to reload. **Rotation:** a pending key is published as `dkim_next`;
when its record verifies it becomes active and the old key is retired, then
deleted after 30 days.

**Sender identities** are exact addresses on a domain, created and approved by
owners. A send needs `identity.status == verified` **and**
`domain.status == verified`, so losing DNS stops sends immediately without
touching identities.

---

## 9. Bounces, complaints and suppression

```mermaid
flowchart LR
    MX["Remote MX"] -- "DSN to b-‹hex›@bounce.domain" --> PF["Postfix<br/>relay_domains /^bounce\./<br/>recipient_access /^b-[0-9a-f]{32}@/"]
    PF -- "transport mailvender-dsn → pipe" --> SH["dsn-forward.sh (curl)"]
    SH -- "POST /internal/dsn?recipient=… + X-Internal-Token" --> API
    API --> P["parse multipart/report:<br/>Action, Status, Final-Recipient"]
    P --> C{"classify"}
    C -- "5.x.x (except 5.2.2, 5.4.7, 5.7.0)" --> H["hard → bounced + suppression"]
    C -- "4.x.x / delayed" --> S["soft → event; suppress at 3 in 30 days"]
```

- **Correlation** prefers the VERP envelope recipient
  (`b-<recipient uuid hex>`) and falls back to `X-Mailvender-Recipient-Id`
  in the returned headers.
- **Complaints** (ARF) arrive at `/internal/feedback-loop/{source}`, signed
  per source. They're correlated by the original's
  `X-Mailvender-Recipient-Id`, mark the recipient `complained`, and suppress
  immediately.
- **Unsubscribe**: `GET /u/{token}` shows a confirmation form (link scanners
  prefetch GETs, so a GET never unsubscribes). `POST /u/{token}` (RFC 8058
  one-click, or the form) records the suppression **idempotently** (unique
  constraint plus savepoint).
- Every suppression, bounce classification and complaint writes an
  `audit_events` row. Only `manual` suppressions can be removed.
- Suppressions are checked **at acceptance** (field error per recipient,
  deliberately vague: "can't receive email from this account") **and again at
  delivery**.
- Bounces, complaints and new unsubscribes queue `message.bounced`,
  `message.complained` and `recipient.unsubscribed` webhook events (§21).

---

## 10. Abuse prevention and account health

- **Limits.** `hourly_recipient_limit` / `daily_recipient_limit` per account
  (defaults 100/500). Enforced atomically: the send path locks the account row
  (`SELECT … FOR UPDATE`) before counting recipients in the window, so
  concurrent requests serialise per account. A test fires 10 concurrent sends
  at a limit of 5 and expects exactly 5 accepted.
- **Violations.** Each over-limit attempt increments a per-hour counter. At
  `LIMIT_VIOLATIONS_BEFORE_SUSPENSION` (20) the account is suspended.
- **Health job** (every 5 min): over `HEALTH_WINDOW_DAYS` (7), bounce rate =
  hard bounces / accepted and complaint rate = complaints / accepted. It acts
  only when at least `HEALTH_MIN_VOLUME` (100) were accepted. Thresholds are 5%
  and 0.1%.
- **Suspension** blocks new sends (403 `account_suspended`) and fails queued
  recipients at delivery. Login, sync and export keep working.
- **Operator API** lists metrics, limits, state and the latest decision, and
  suspends, reinstates, changes limits or records reviews. Every action is an
  immutable audit row with actor and reason. The admin dashboard (§20) uses the
  same `HealthService` logic.
- **Reputation:** links in an email are checked against Spamhaus DBL at
  acceptance, and our sending IPs and domain are checked against blocklists
  hourly (§22).

---

## 11. Asset hosting

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as API
    participant S3 as S3
    participant C as CDN

    B->>A: POST /v1/assets/uploads {filename, content_type, size}
    A->>A: allow png/jpeg/gif/webp, size ≤ 5 MB
    A->>S3: presigned POST for ‹prefix›images/‹user›/uploads/‹uuid›-‹rand›<br/>conditions: exact key, Content-Type, content-length-range 1..size, 5 min
    A-->>B: {url, fields} (no AWS credentials)
    B->>S3: multipart POST (fields + file)
    B->>A: POST /v1/assets/{id}/finalize
    A->>S3: HEAD + GET upload (≤ 5 MB)
    A->>A: Pillow open + verify, format must match declared type,<br/>≤ 8000 px, size ≤ declared
    alt invalid
        A->>S3: delete upload → 422 (never published)
    else valid
        A->>S3: PUT ‹prefix›images/‹user›/public/‹uuid›/‹safe-name›.‹ext›<br/>Cache-Control immutable
        A->>S3: delete upload
        A-->>B: asset with public_url (CDN)
    end
    B->>C: emails reference public_url
```

- The bucket policy allows anonymous `GetObject` **only** on
  `images/*/public/*`. Uploads and unfinalized objects stay private (the E2E
  check confirms 403).
- Keys are unguessable (random UUIDs) and segregated by account prefix.
- Delete removes the public object. Emails already delivered keep whatever the
  client cached. CDN invalidation is an operational step.
- `AWS_S3_UPLOAD_ENDPOINT_URL` exists for S3-compatible endpoints the browser
  reaches under a different host than the API does (it was used with MinIO
  locally). A presigned POST's signature doesn't cover the host.

---

## 12. Cloud sync

### Server contract

- Generic records: `(account_id, collection, id)` with `data` (JSON),
  `revision`, `deleted`, `seq`, `parent_id`, `updated_at`/`by`.
- Collections: `pages`, `workspace` (page order), `saved_pages`,
  `components`, `brand`, `datasets`, `versions` (`parent_id` = page).
- `PUT` / `DELETE` take `expected_revision` (0 = create). A mismatch returns
  `409 revision_conflict` with `details.current`, and nothing is overwritten.
- `seq` comes from a global sequence, assigned while holding a per-account
  advisory lock (`pg_advisory_xact_lock`). Within an account, `seq` therefore
  commits in order, and `GET /v1/sync/changes?since=` can't skip a change.
- `GET /v1/sync/snapshot` pages live records and returns a `watermark` (max
  seq at the first page). A new device fetches the snapshot, then changes
  since the watermark.
- `POST /v1/pages/{id}/versions/{vid}/restore` first saves the current page as
  a version, then writes the restored doc as a new revision.

### Client engine (`mailvender/lib/sync`)

```mermaid
flowchart TB
    subgraph Local["Browser (source of edits)"]
        LS["localStorage stores<br/>(pages, workspace, saved pages,<br/>components, brand)"]
        IDB["IndexedDB<br/>(datasets, versions)"]
        AD["Adapters: fingerprints() / load() / apply() / clear()"]
        META["mailvender:sync meta<br/>{accountId, since, records: key→{id, revision, fp}, queue}"]
    end
    LS --> AD
    IDB --> AD
    AD --> E["SyncEngine"]
    E <--> META
    E -- "scan: fingerprint ≠ last synced → queue key" --> E
    E -- "push: PUT/DELETE with expected_revision" --> API["/v1/sync/*"]
    API -- "409 current" --> E
    E -- "retry with current.revision (LWW) + notice" --> API
    E -- "pull: /changes since" --> API
    E -- "apply (skip queued keys)" --> AD
```

Key choices:

- **Local first.** Every edit writes locally (synchronously for
  localStorage), and the editor never awaits the network. The queue stores
  only keys, and data is re-read at push time, so large datasets never land in
  localStorage.
- **Change detection by fingerprint** (cyrb53 of the JSON, or `updatedAt` for
  datasets) instead of instrumenting every mutation. Existing stores stay
  untouched apart from small sync hooks.
- **Deterministic ids.** Server ids are UUIDv5(`collection:localId`), so local
  ids (`page_x1y2`) stay unchanged and every device derives the same UUID
  without a mapping table.
- **Conflict policy (documented in the OpenAPI descriptions).** A queued local
  change that hits 409 is retried against the current revision (last write
  wins, the user's offline change being the last write), with a non-blocking
  notice. Remote changes for keys still in the queue aren't applied; the push
  decides.
- **First sign-in in a browser adopts local data** (made before sync existed):
  server records are applied first, local-only ones uploaded, and the blank
  "Page 1" of a fresh browser is dropped when the account already has pages.
  **Switching accounts** refuses while the queue isn't empty, then clears and
  re-hydrates. **Sign-out** clears local data (privacy on shared machines).
- The open page is reloaded in the editor when another device replaces it
  (`onPageReplaced`).
- `DatasetsProvider` receives a **local-first `DatasetRepository`**
  (`lib/datasets.ts`) that delegates to IndexedDB and asks the engine to
  upload. Autosave and undo/redo are unchanged.

---

## 13. Data lifecycle

- **Export:** owner requests it, the worker builds a ZIP (workspace records,
  members, domains, identities, assets with image files, message metadata
  without bodies, suppressions) in a temporary file and streams it to private
  storage. Download is owner-only through a 5-minute presigned link, expires
  after 24 h, and is audited. `POST /v1/auth/me/export` returns the person's
  own data (profile, memberships, invitations, sessions, activity).
- **Deletion:** one transaction marks the account deleted, revokes its API
  keys and its members' sessions, disables domains (freeing names) and
  identities, fails queued recipients and cancels their outbox events. Owners
  can restore it for 30 days (`POST /v1/accounts/{id}/restore`); then the
  retention job erases images, workspace data and memberships. Suppressions and
  audit are retained.
- **Login deletion** (`POST /v1/auth/me/deletion`, password): refused while the
  person owns an account others use; deletes accounts only they are in, removes
  their memberships, sessions and tokens, and replaces their email and password
  (the row stays for audit, `users.deleted_at`).
- **Retention job** (daily): purge bodies at 30 days, messages at 90,
  delivery events at 180, expired tokens/sessions/counters/exports, retired
  DKIM keys at 30, abandoned uploads after 1 h, webhook deliveries at 30 days,
  admin inbox and sent mail at 180, DMARC rows at 365, deleted accounts after
  30. See [RETENTION.md](RETENTION.md).

---

## 14. Worker and scheduled jobs

```mermaid
flowchart LR
    L["loop"] --> O["process_outbox(20)<br/>FOR UPDATE SKIP LOCKED"]
    O --> J["run_due_jobs()"]
    J --> L
    J --> J1["dns_recheck – 10 min"]
    J --> J5["platform_dkim – 5 min"]
    J --> J2["account_health – 5 min"]
    J --> J3["retention – 24 h"]
    J --> J4["dkim_key_sync – 30 s"]
    J --> J6["reputation – 1 h"]
    J --> J7["alerts – 10 min"]
```

| Outbox kind             | Handler                                                       |
| ----------------------- | ------------------------------------------------------------- |
| `deliver_recipient`     | `DeliveryService.deliver` (retry/backoff inside)              |
| `system_email`          | Verification/reset/invitation mail, body erased after sending |
| `account_export`        | `LifecycleService.build_export`                               |
| `webhook`               | `WebhookService.deliver` (signed POST; its own retry schedule) |
| `admin_mail`            | `AdminMailService.deliver` (email written in the admin)       |
| `account_assets_delete` | Delete an account's assets (only events queued before restore existed; the purge does it now) |

- Each event runs in a **savepoint**. An unexpected exception backs off
  (30 s × 2ⁿ) and dead-letters after 10 attempts (`outbox.dead_letter` log).
- Jobs take `pg_try_advisory_xact_lock(job name)`, so any number of workers can
  run without double execution. Results are recorded in `job_runs`.
- `SKIP LOCKED` lets several workers share the outbox safely.
- Graceful stop on SIGTERM/SIGINT. The worker waits for migrations to reach
  head before starting.

---

## 15. Cross-cutting concerns

| Concern              | Implementation                                                                                                                                                                                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Errors**           | One JSON shape; stable `code`; user-safe `message`; `fields` for validation (`to[2]`, `X-Account-Id`, …). Documented responses on every route.                                                                                                                                             |
| **Pagination**       | Keyset on `(created_at, id)`, opaque base64 cursor, default 25, max 100 (422 above). Sync snapshot uses `(collection, id)` plus watermark.                                                                                                                                                 |
| **Logging**          | JSON to stdout: `time, level, logger, event, request_id, user_id, account_id` + fields (`message_id`, …). `X-Request-ID` echoed. Health probes aren't logged.                                                                                                                              |
| **Rate limiting**    | Fixed windows in `rate_limit_counters` via `INSERT … ON CONFLICT DO UPDATE … RETURNING`, committed in its **own** transaction so failed attempts still count. Per IP and per email for signup, login, verification resend and reset. 429 with `Retry-After`; `rate_limit.exceeded` logged. |
| **Secrets**          | Env vars only. Argon2id for passwords. HMAC for tokens and keys. Fernet for DKIM keys, TOTP secrets, webhook signing secrets and recipient tokens. Nothing secret is returned by the API (export excludes keys; a webhook secret is shown once).                                       |
| **Outbound HTTP**    | Only webhooks (customer URLs), the HIBP range API and the alert webhook. Webhook targets must resolve to public addresses (checked on save and before each request), redirects aren't followed, 10 s timeout, 1 KB of the answer kept.                                                   |
| **Contract**         | FastAPI generates `/openapi.json`, committed as `backend/openapi.json` (CI diff). The frontend generates `lib/api/schema.d.ts` with `openapi-typescript` and calls the API through typed `openapi-fetch`. No handwritten duplicate types.                                                  |
| **Timestamps / ids** | UTC `timestamptz`, ISO 8601 with offset; UUIDs everywhere.                                                                                                                                                                                                                                 |
| **Migrations**       | Alembic. The API entrypoint runs `upgrade head` under an advisory lock before serving.                                                                                                                                                                                                     |
| **Security headers** | `X-Content-Type-Options`, `Referrer-Policy`; view-in-browser served with a script-blocking CSP.                                                                                                                                                                                            |

---

## 16. Testing strategy

- **pytest against real PostgreSQL** (`TEST_DATABASE_URL`). The session fixture
  drops the schema and runs Alembic (testing migrations). Each test truncates
  all tables afterwards.
- **Fakes** for `ObjectStorage`, `DnsResolver` and `MailTransport` go through
  the same interfaces as production.
- Coverage: migrations (round-trip, models = schema, audit immutability),
  auth (hashing, verification, cookies, revocation, expiry, reset, CSRF, rate
  limits), accounts and roles, **cross-account isolation** (reads, writes,
  sends, API keys, domains, body-supplied account ids), domains and DNS
  states and rotation, sending (queueing, MIME, idempotency, validation,
  retries, permanent failures, concurrency limits, suspension, API keys),
  feedback (unsubscribe, hard/soft bounces, complaints, signatures), health
  and operator, assets, sync (conflicts, tombstones, snapshot and changes,
  versions), lifecycle (export, deletion, restore, login deletion, retention),
  the admin API (proxy key, sign-in and TOTP, inbox and DMARC parsing, admin
  mail), webhooks (signing, retries, auto-disable, SSRF), blocklists, alerts,
  breached passwords and customer two-factor.
- A manual **end-to-end** pass through the real stack (Next.js proxy →
  API → worker → Postfix + OpenDKIM → Mailpit, DSN back through Postfix,
  MinIO policy) verified the infrastructure wiring.

---

## 17. Deployment topology

```mermaid
flowchart TB
    subgraph Public
        U["Users"]
        CDN["CDN"]
    end
    subgraph App["App tier"]
        FE["Next.js studio (Vercel)"]
        ADM["Admin app (Vercel)"]
        API1["API replica"]
        API2["API replica"]
        W1["Worker(s)"]
    end
    subgraph Data
        PG[("PostgreSQL (managed, PITR)")]
        S3[("S3 (versioned)")]
    end
    subgraph Mail["Mail tier (static IPs, PTR, port 25)"]
        PF["Postfix"]
        OD["OpenDKIM"]
        V[/"private DKIM volume"/]
    end
    U --> FE --> API1 & API2
    ADM --> API1
    U --> CDN --> S3
    API1 & API2 & W1 --> PG
    API1 & API2 --> S3
    W1 --> PF --> OD
    W1 --> V
    OD --> V
    PF --> Internet["Recipients' MX"]
    Internet -- "bounces" --> PF
```

API replicas are stateless (sessions live in Postgres). Workers scale
horizontally. Postfix needs stable outbound IPs with reverse DNS, and
`POSTFIX_MYNETWORKS` limited to worker addresses. In the compose file the mail
network has fixed addresses for exactly this reason.

---

## 18. Design decisions

| Decision                                                                            | Why                                                                                                                                                 | Trade-off / alternative                                                                                       |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **FastAPI + Pydantic + SQLAlchemy 2 + Alembic + Postgres** (given)                  | Typed contracts and automatic OpenAPI; a mature ORM and migrations; Postgres gives transactions, `SKIP LOCKED`, advisory locks, JSONB.              | —                                                                                                             |
| **Repository + Unit of Work, sync (not async) SQLAlchemy**                          | Required layering. Services stay testable and persistence-agnostic. Sync code is simpler and FastAPI runs sync endpoints in a thread pool.          | Async would raise per-process concurrency. Revisit if the API becomes I/O-bound at scale.                     |
| **Postgres for everything (outbox, rate limits, locks, sync seq)**                  | One dependency to operate. Every state change commits atomically with its side effects.                                                             | Redis/Kafka would scale higher. Not needed at this stage.                                                     |
| **Transactional outbox + worker**                                                   | "202 after durable queueing" with no lost or orphaned messages. Retries and backoff are explicit.                                                   | At-least-once delivery (rare duplicates on crash).                                                            |
| **One SMTP message per recipient**                                                  | Per-recipient unsubscribe tokens, VERP bounce correlation, individual status.                                                                       | More SMTP transactions than BCC batches. The frontend's batch mode still sends one copy per recipient.        |
| **Postfix + OpenDKIM milter** (given)                                               | Mature and observable. The API owns authorisation, the MTA only relays. `milter_default_action = tempfail` means mail is never sent unsigned.       | Signing in Python (dkimpy) would remove a moving part but contradicts the requirement.                        |
| **Postfix rather than Postal or KumoMTA**                                           | Postal is a full mail platform (its own domains, DKIM keys, bounce handling, web UI, MariaDB) that duplicates what the API already owns. Postfix is a pure relay, so domains, keys and bounces keep one source of truth. Production mode adds throttled per-provider transports, a persistent queue and fail-fast checks. | No automatic per-provider backoff or IP pools. KumoMTA is the upgrade path at high volume (see §19). |
| **DKIM keys encrypted in the DB, decrypted to a private volume**                    | One source of truth, at-rest encryption, rotation through the API. OpenDKIM reads plain files and reloads on SIGUSR1.                               | The volume holds plaintext keys: restrict it to the worker and OpenDKIM. A KMS/HSM could replace Fernet.      |
| **VERP return path `b-<id>@bounce.<domain>`**                                       | Exact bounce-to-recipient correlation even when DSNs are malformed. SPF aligns relaxed with the From domain.                                        | Requires the customer to publish MX + SPF on `bounce.`.                                                       |
| **Session cookies (not JWT)**                                                       | Instant revocation (AC: revoked sessions rejected immediately), no token in JS or localStorage.                                                     | A DB lookup per request (indexed, cheap).                                                                     |
| **Keyed HMAC for tokens and API keys, Argon2id for passwords**                      | High-entropy secrets don't need slow hashing, and HMAC allows indexed lookup. Passwords do need it.                                                 | Rotating `SECRET_KEY` invalidates keys and links (documented).                                                |
| **CSRF via custom header + SameSite=Lax**                                           | Simple, stateless, works with the same-origin proxy.                                                                                                | Requires every client to send the header (the typed client does it centrally).                                |
| **Same-origin `/api` proxy through Next.js**                                        | First-party cookies, no CORS in the browser, API not exposed separately.                                                                            | An extra hop. Body size limits of the proxy apply (datasets ≤ 25 MB).                                         |
| **404 for other accounts' resources; vague suppression and domain-conflict errors** | No cross-tenant information leaks.                                                                                                                  | Slightly less helpful errors.                                                                                 |
| **Atomic limits via account row lock**                                              | Correct under concurrency with no extra infrastructure.                                                                                             | Serialises sends within one account (fine at these volumes).                                                  |
| **Fernet for recipient tokens**                                                     | Opaque (encrypted), signed and expiring in one primitive.                                                                                           | Long tokens (≈200 chars); MIME uses a 998-character line policy so `List-Unsubscribe` isn't RFC 2047-encoded. |
| **Presigned POST + server-side verification for images**                            | No AWS credentials in browsers. Size and type enforced by S3 _and_ re-verified by decoding the image before publishing. SVG excluded (script risk). | The API downloads each image once on finalize.                                                                |
| **Generic sync records + revisions + change feed**                                  | One endpoint family for all collections. Optimistic concurrency without locks. Tombstones propagate deletions.                                      | Whole-record LWW, not field-level merging (documented policy). Large datasets are uploaded whole.             |
| **Fingerprint scanning on the client**                                              | Minimal intrusion into existing editor stores. Robust to any mutation path.                                                                         | Periodic re-hashing of local data (cheap at these sizes).                                                     |
| **Immutable audit via DB trigger**                                                  | Guaranteed even against application bugs or manual SQL.                                                                                             | Corrections must be new rows.                                                                                 |
| **Community MinIO images (`pgsty/*`)**                                              | Retired: local development now uses the real S3 bucket (`dev/` folder).                                                                                              | Revisit if a maintained official image returns.                                                               |
| **`cryptography<47` pin**                                                           | Newer wheels crash (SIGILL) on some older Docker VMs on Apple Silicon.                                                                              | Remove once all developer Docker installs are current (backlog 2.2); CI ignores four advisories in unused code until then. |
| **Webhooks: Stripe-style signature over `"<t>.<body>"`, delivered through the outbox** | A familiar scheme for customers (replay-safe with the timestamp); events commit atomically with the state change that caused them.                 | At-least-once and unordered; receivers dedupe on `Mailvender-Event-Id`.                                       |
| **Admin as a separate app, API gated by a proxy key**                               | The admin API is invisible (404) without the key; admins aren't customer users, so a customer bug can't grant admin rights.                       | A second Vercel project and secret to manage.                                                                 |
| **Alerts from inside the platform, by email and webhook**                           | No extra monitoring service to run; the webhook still works when our own mail is broken.                                                            | If the whole server is down nothing inside alerts, so an outside monitor checks the URLs and the worker's heartbeat (PRODUCTION_SETUP step 20).                      |
| **Breached-password check fails open**                                              | An HIBP outage must not block sign-ups.                                                                                                             | During an outage a leaked password can get through.                                                           |

---

## 19. Known limitations

- Feedback loops must POST signed reports. Providers that only email ARF
  reports need a small signing relay (or an inbound address routed like DSNs).
- Sync conflict resolution is whole-record last-write-wins. Collaborative
  editing would need operational transforms or CRDTs.
- `dev-verify-domain` exists because local domains can't publish DNS. It's
  refused outside `ENVIRONMENT=development`.
- The frontend sync engine is verified manually and through the backend
  contract tests. It has no automated browser tests yet.
- Health metrics are computed per request for the operator list. Materialise
  them if the number of accounts grows large.
- One outbound IP per Postfix host, with static per-provider concurrency
  limits. Dedicated IPs per customer, IP pools and adaptive backoff when a
  provider defers (`421 4.7.x`) need an ESP-grade MTA. KumoMTA fits that:
  it's a pure MTA like Postfix, signs DKIM from files and accepts SMTP from
  the worker, so it can replace Postfix + OpenDKIM behind `SmtpTransport`
  without changing the API.
- Not built yet (scheduling, A/B tests, analytics, billing, IP pools, …):
  [backlog.md](../../backlog.md).

---

## 20. Admin dashboard

```mermaid
flowchart LR
    B[Admin's browser] -->|/api/v1/admin/*| V[mailvender-admin on Vercel<br/>proxy.ts adds X-Mailvender-Admin-Secret]
    V -->|rewrite, server to server| A[API /v1/admin/*]
    X[Anyone else] -->|/v1/admin/* without the key| A
    A -.->|404 before routing| X
    CF[Cloudflare Email Routing<br/>support@ postmaster@ abuse@ dmarc@] --> W[Email Worker]
    W -->|copy| G[Gmail]
    W -->|HMAC-signed raw message| I[API /internal/inbound-mail]
    I --> S3[(S3 inbox/…eml)]
    I --> DB[(inbound_messages,<br/>dmarc_report_rows)]
```

- **Two locks.** The admin key (`ADMIN_PROXY_SECRET`) is checked by a middleware
  before routing, so without it every `/v1/admin` path, method or body gets the
  same 404. Behind it is the admin's own session.
- **Admins aren't customers.** They live in `admin_users` and `admin_sessions`,
  with their own cookie (`__Host-mv_admin_session`, SameSite=Strict, 30 min idle,
  8 h max). The first sign-in forces a password change and TOTP setup;
  recovery codes are stored hashed. Ten failures lock the account.
- **Read-mostly, metadata only.** `SqlAdminStats` counts across all accounts
  (with indexes on `message_recipients.created_at/accepted_at`,
  `delivery_events.created_at` and `audit_events(actor_type, created_at)`).
  It never reads message bodies. Daily active users come from
  `user_activity_days`, written when a customer session is used (at most every
  5 minutes).
- **Decisions reuse the domain logic.** Suspend, reinstate, limits and review go
  through `HealthService` with an operator `Actor`, so customers see the same
  `account.*` audit events as before. Admin-only rows (`admin.*`, `user.blocked`)
  have no `account_id`, so they're invisible to customers.
- **"Needs help"** is computed on request from existing data: unverified
  signups, pending or broken domains, pending senders, suspensions, rates near
  the limits, limit violations, failed deliveries, bulk without an address.
  Only the admin's contacted/resolved note is stored (`admin_issue_states`).
- **Role inboxes.** The API stores the raw message and a row. ARF reports at
  abuse@ feed `FeedbackService.process_complaint`; DMARC aggregate reports are
  unpacked with size and file-count limits, and XML with DOCTYPE/ENTITY is
  refused. HTML is only served by `/v1/admin/inbox/{id}/body` with
  `default-src 'none'`, the CSP `sandbox` directive and images opt-in, inside a
  sandboxed iframe.
- **Writing email.** Admins send from `ADMIN_MAIL_FROM_MAILBOXES` at the platform
  domain (default support@, postmaster@, abuse@). Attachments are uploaded one
  per request as the raw body (each ≤ 4 MB, under Vercel's request limit; ≤ 7 MB
  per email, under Postfix's 10 MB once base64-encoded) to private S3; the
  `admin_mail` outbox event builds the MIME message (branded HTML + text, or
  text only; In-Reply-To/References for replies) and sends it through Postfix,
  so it's platform-DKIM signed. The From address is also the envelope sender,
  so replies and bounces return to that role inbox.

---

## 21. Webhooks

```mermaid
sequenceDiagram
    participant S as Service (sending / feedback)
    participant DB as PostgreSQL
    participant W as Worker
    participant C as Customer's endpoint

    S->>DB: state change (e.g. recipient → bounced)
    S->>DB: webhook_deliveries row per listening endpoint<br/>+ outbox event "webhook" (same transaction)
    W->>DB: claim outbox event
    W->>W: resolve host – public addresses only
    W->>C: POST JSON, Mailvender-Signature: t=…,v1=HMAC(secret, "t.body")
    alt 2xx
        W->>DB: delivered; endpoint.consecutive_failures = 0
    else other / timeout
        W->>DB: attempts += 1; outbox event pending until next backoff step
        Note over W,DB: 30 s, 2 m, 10 m, 30 m, 1 h, 3 h, 6 h, 12 h, 12 h – then failed
    end
```

- **Events:** `message.sent`, `message.failed`, `message.bounced`,
  `message.complained`, `recipient.unsubscribed` (one per recipient), and
  `webhook.test`. `emit()` builds the payload once (`id`, `type`, `created_at`,
  `account_id`, `data` with the message's `metadata`) and skips accounts that
  aren't active.
- **Endpoints** belong to an account (owners manage them, max 10). The secret
  (`whsec_…`) is shown once and stored Fernet-encrypted because it must be used
  to sign. Rotating replaces it at once.
- **Retries** are handled inside `WebhookService.deliver` (not by raising), so
  each attempt's status code, error and the first 1 KB of the answer are kept
  for the customer's delivery log. Tests are tried once.
- **Auto-disable:** when a delivery finally fails and the endpoint hasn't
  succeeded for 3 days, it's turned off and the owners are emailed. Turning it
  back on resets the counter; failed deliveries can be resent (same event id).
- **SSRF protection:** https only (http and private targets only with the
  development flags, refused in production), the host must resolve only to
  global addresses – checked on save and again before every request (DNS can
  change) – and redirects aren't followed.
- Customer guide: [WEBHOOKS.md](WEBHOOKS.md).

---

## 22. Reputation, alerts and sign-in security

- **Blocklists** (`services/reputation.py`): `DnsResolver.a()` looks up
  `<reversed IP>.<zone>` and `<domain>.<zone>`. `127.0.0.x` = listed;
  `127.255.255.x` = the list refused the query (unknown, never listed). The
  hourly `reputation` job stores results in `system_state["blocklists"]` (with
  the first time each listing was seen); the admin's health checks turn them
  into problems. At send time, registered domains of http(s) links are looked up
  in `LINK_BLOCKLIST_ZONES`, cached in-process for an hour.
- **Alerts** (`services/alerts.py`): every 10 minutes the `alerts` job runs
  `AdminDashboardService.checks()`. Checks carry a stable `key`; problems not in
  `system_state["alerts"]` are new (alert), ones that disappeared are fixed
  (alert), and open ones get a daily reminder. Email goes through the normal
  system-email outbox; the optional webhook is posted directly.
- **Breached passwords** (`services/pwned.py`): the first five hex characters of
  the SHA-1 go to the HIBP range API with padding; a match refuses the password
  for customers and admins. Errors or timeouts allow it.
- **Customer two-factor** (`services/two_factor.py`): the same TOTP code as
  admins (`security.py`), optional per user. Login asks for the code after the
  password; recovery codes are single-use hashes; turning it off needs the
  password and a code; a password reset doesn't turn it off; admins can reset it
  (audited, the user is emailed).
- **Password change and sessions** (`AuthService.change_password`, `sessions`,
  `revoke_session`, `revoke_other_sessions`): Settings → You
  (`component/Account/SignInSecurity.tsx`). The current session can't be
  revoked that way (409 `current_session`; use logout). All audited.
- **Worker heartbeat** (`worker.heartbeat`, `HEARTBEAT_URL`): the outside
  monitor's dead-man's switch.

