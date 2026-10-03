# Production runbook

## Components

| Service    | Image / command             | Notes                                                                                                                                                                                                                    |
| ---------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| API        | `backend/Dockerfile`, `api` | Runs `alembic upgrade head` (advisory-locked, safe with several replicas), then uvicorn on :8000. Readiness: `/health/ready` (fails without the database). Liveness: `/health/live`.                                     |
| Worker     | same image, `worker`        | Waits until migrations are at head. Consumes the outbox; runs DNS rechecks (10 min), account health (5 min), retention (daily), DKIM key publication (30 s). Jobs are advisory-locked – run as many workers as you like. |
| Postfix    | `infra/postfix`             | Relays only for the worker's address (`POSTFIX_MYNETWORKS`); accepts inbound mail only for `b-<id>@bounce.*` (bounces), piped to `/internal/dsn`.                                                                        |
| OpenDKIM   | `infra/opendkim`            | Sign-only milter. Reads keys the worker writes to a private volume shared with it only; reloads on change. Postfix defers (never sends unsigned) if it's down.                                                           |
| Next.js    | `mailvender/`               | Proxies `/api/*` to the API (`API_INTERNAL_URL`), so cookies are first-party.                                                                                                                                            |
| PostgreSQL | managed                     | ≥ 14.                                                                                                                                                                                                                    |
| S3 + CDN   | AWS                         | See _Asset storage_.                                                                                                                                                                                                     |

## Environment variables

All names are in [`.env.example`](../.env.example). Secrets come from your
secret store and are never committed: `DATABASE_URL`, `SECRET_KEY`,
`DKIM_ENCRYPTION_KEY`, `INTERNAL_API_TOKEN`, `FEEDBACK_LOOP_SECRETS`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. The API never returns any of
them; DKIM private keys are encrypted at rest (Fernet) and only decrypted onto
the OpenDKIM volume.

Required in production: `ENVIRONMENT=production`, `APP_BASE_URL`,
`PUBLIC_API_BASE_URL`, `ALLOWED_ORIGINS`, `TRUST_FORWARDED_FOR=true` (behind
the proxy), `SMTP_HOST`, `SYSTEM_FROM_EMAIL`, `RETURN_PATH_MX_HOST`,
`SPF_INCLUDE_DOMAIN`, `DKIM_KEYS_DIR` (worker), `AWS_*`. Postfix needs
`POSTFIX_HOSTNAME`, `POSTFIX_MYNETWORKS` (worker IPs only), `POSTFIX_RELAYHOST`
(empty: deliver directly), `INTERNAL_API_TOKEN`, `MAILVENDER_API_URL`, and a
TLS certificate (`POSTFIX_TLS_CERT`/`KEY`) for inbound bounces.

## Database migrations

1. Back up (see below).
2. Deploy the new image; the API applies migrations on start. To migrate
   separately: `docker run … mailvender-api migrate`.
3. Migrations must be backwards compatible with the previous release (add
   columns nullable, backfill, then tighten in a later release) so old and new
   API replicas can run side by side.
4. Roll back code first; `alembic downgrade -1` only for migrations that
   haven't written data you need.

## DNS requirements

Mailvender's own records (once):

- `SPF_INCLUDE_DOMAIN` (e.g. `_spf.mailvender.example`) TXT:
  `v=spf1 ip4:<outbound IPs> -all`.
- `RETURN_PATH_MX_HOST` (e.g. `feedback.mailvender.example`) A record → the
  Postfix inbound IP; port 25 open for bounces.
- Reverse DNS (PTR) for every outbound IP matching `POSTFIX_HOSTNAME`.

Per customer domain (shown in Settings → Sending domains and by `GET
/v1/domains/{id}`):

| Record           | Name                             | Value                                      |
| ---------------- | -------------------------------- | ------------------------------------------ |
| DKIM TXT         | `<selector>._domainkey.<domain>` | `v=DKIM1; k=rsa; p=<2048-bit key>`         |
| Return-Path MX   | `bounce.<domain>`                | `10 <RETURN_PATH_MX_HOST>`                 |
| SPF TXT          | `bounce.<domain>`                | `v=spf1 include:<SPF_INCLUDE_DOMAIN> -all` |
| DMARC TXT (bulk) | `_dmarc.<domain>`                | `v=DMARC1; p=none; …; adkim=r; aspf=r`     |

DKIM signs with `d=<domain>`; the envelope sender is
`b-<recipient id>@bounce.<domain>`, which aligns with the From domain under
relaxed SPF alignment. Bulk sending requires DMARC with relaxed SPF alignment.
Verified domains are rechecked hourly; a domain whose records disappear goes
back to `pending` and its sends are rejected.

## Key rotation

- **DKIM** (per domain, e.g. yearly): Settings → _Rotate DKIM key_ (or `POST
/v1/domains/{id}/dkim/rotate`). The customer publishes the `dkim_next`
  record; the next verification promotes it, the old key is retired and
  deleted after `RETENTION_RETIRED_DKIM_DAYS`. Keep the old selector's DNS
  record for a few days so in-flight mail still verifies.
- **`DKIM_ENCRYPTION_KEY`**: re-encrypt `dkim_keys.private_key_encrypted` with
  a script using `MultiFernet([new, old]).rotate(...)`, then switch the env var.
- **`SECRET_KEY`**: rotating it signs out everyone, invalidates outstanding
  email links, API keys (hashes are keyed) and unsubscribe links in sent mail.
  Rotate only when compromised; announce API-key reissue.
- **`INTERNAL_API_TOKEN` / feedback-loop secrets**: update Postfix / the FBL
  provider and the API together.
- **AWS keys**: create a new IAM key, deploy, then delete the old one.

## Asset storage (S3)

- Private bucket; Block Public Access may stay on if the CDN (CloudFront with
  Origin Access Control) is the only public reader. Otherwise apply
  [`infra/bucket-policy.production.json`](../infra/bucket-policy.production.json)
  (public `GetObject` only on `<prefix>accounts/*/public/*`).
- CORS: allow `POST` from the app origin (browsers upload with presigned POST).
- IAM for the API: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on
  `<bucket>/<prefix>*`.
- `AWS_S3_PUBLIC_BASE_URL` = the CDN URL. Finalized assets are immutable
  (`Cache-Control: max-age=31536000, immutable`); deleting one also needs a CDN
  invalidation if it must disappear immediately.

## Backup and restore

- PostgreSQL: continuous archiving / point-in-time recovery (managed service
  snapshots + WAL), retained 30 days. Test a restore monthly into a staging
  database and run `alembic current`.
- S3: versioning on with a 30-day noncurrent-version expiry.
- The OpenDKIM volume is derived data – the worker rebuilds it from the
  database within 30 s.
- Restore order: database → API/worker (migrations no-op) → verify
  `/health/ready` → resume Postfix.

## Monitoring and alerting

Logs are JSON lines (`event`, `request_id`, `user_id`, `account_id`,
`message_id`). Alert on:

| Signal            | Source                                                                                                  | Suggested threshold                         |
| ----------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| Queue backlog     | `SELECT count(*) FROM outbox_events WHERE status='pending' AND available_at < now() - interval '5 min'` | > 100 for 10 min                            |
| Dead letters      | `outbox.dead_letter` log / `status='dead'`                                                              | any                                         |
| Delivery failures | `message.failed` log rate                                                                               | > 5 % of `message.accepted_by_mta` over 1 h |
| Bounces           | `message.bounced` (classification=hard)                                                                 | per-account rate (health job)               |
| Complaints        | `message.complained`, `complaint.unauthenticated`                                                       | any unauthenticated; rate per account       |
| Suspensions       | `account.suspended`                                                                                     | any (review)                                |
| Domain loss       | `domain.verification_lost`                                                                              | any                                         |
| Jobs              | `job_runs.last_finished_at`, `job.*.failed`                                                             | stale > 2× interval or any failure          |
| Rate limiting     | `rate_limit.exceeded`                                                                                   | spikes (credential stuffing)                |
| Postfix           | `postfix/smtp … status=deferred`, queue size (`mailq`)                                                  | growing queue                               |
| API               | 5xx rate, `/health/ready`                                                                               | 5xx > 1 %, readiness failing                |

## Account health

Rolling window `HEALTH_WINDOW_DAYS` (7) over recipient-level events: bounce
rate = hard bounces / accepted, complaint rate = complaints / accepted,
evaluated only with ≥ `HEALTH_MIN_VOLUME` (100) accepted recipients. Above
`HEALTH_MAX_BOUNCE_RATE` (5 %) or `HEALTH_MAX_COMPLAINT_RATE` (0.1 %) the
account is suspended automatically; so is an account that hits its hourly or
daily limit `LIMIT_VIOLATIONS_BEFORE_SUSPENSION` (20) times in an hour.
Operators review with `GET /v1/operator/accounts` and act with
`…/suspend`, `…/reinstate`, `PUT …/limits`, `…/reviews` – all audited.

Soft bounces suppress an address after `SOFT_BOUNCE_THRESHOLD` (3) within
`SOFT_BOUNCE_WINDOW_DAYS` (30); hard bounces and complaints suppress at once.

## Feedback loops (complaints)

Configure each provider to POST ARF reports to
`https://<api>/internal/feedback-loop/<name>` with header
`X-Mailvender-Signature: sha256=<HMAC-SHA256(body, secret)>`, and add
`<name>:<secret>` to `FEEDBACK_LOOP_SECRETS`. (For providers that only email
ARF reports, forward them through a small relay that signs the body.)
