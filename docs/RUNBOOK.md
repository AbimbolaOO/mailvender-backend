# Production runbook

## Components

| Service    | Image / command             | Notes                                                                                                                                                                                                                    |
| ---------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| API        | `backend/Dockerfile`, `api` | Runs `alembic upgrade head` (advisory-locked, safe with several replicas), then uvicorn on :8000. Readiness: `/health/ready` (fails without the database). Liveness: `/health/live`.                                     |
| Worker     | same image, `worker`        | Waits until migrations are at head. Consumes the outbox; runs DNS rechecks (10 min), account health (5 min), retention (daily), DKIM key publication (30 s). Jobs are advisory-locked – run as many workers as you like. |
| Postfix    | `infra/postfix`             | Relays only for the worker's address (`POSTFIX_MYNETWORKS`); accepts inbound mail only for `b-<id>@bounce.*` (bounces), piped to `/internal/dsn`. Queue on the `postfix-queue` volume. See _Production mail server_. |
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

With `ENVIRONMENT=production` the API and worker refuse to start with
development secrets or `*.localhost` mail settings; with
`POSTFIX_ENVIRONMENT=production` Postfix refuses a placeholder hostname, a
missing or snake-oil certificate, a Mailpit relay, `POSTFIX_SMTP_TLS_LEVEL=none`
and the development `INTERNAL_API_TOKEN`. The error names the setting.

## Production mail server

> First deployment of mailvender.com? Follow the step-by-step
> [PRODUCTION_SETUP.md](PRODUCTION_SETUP.md); this section is the reference.

[`docker-compose.prod.yml`](../docker-compose.prod.yml) runs the API, worker,
Postfix and OpenDKIM on one host (PostgreSQL and S3 are managed services):

```sh
cp .env.example .env               # fill in every value
mkdir certs                        # fullchain.pem + privkey.pem for POSTFIX_HOSTNAME
docker compose -f docker-compose.prod.yml up -d --build
```

Before the first send:

1. **A clean static IPv4 address** for the host. Check it isn't listed on
   Spamhaus/Barracuda before you start. Postfix sends over IPv4 only.
2. **Reverse DNS (PTR)** for that IP → `POSTFIX_HOSTNAME`, and an A record for
   `POSTFIX_HOSTNAME` → the IP (forward-confirmed). Gmail and Microsoft reject
   mail from IPs without it.
3. **TLS certificate** for `POSTFIX_HOSTNAME` in `./certs` (e.g. Let's Encrypt
   with a DNS challenge; restart Postfix after renewal).
4. **Port 25.** Inbound 25 must be open for bounces. Outbound 25 is blocked by
   default on AWS, GCP, Azure and DigitalOcean. Request removal, or relay
   through a smarthost (below).
5. **Mailvender's own DNS** (_DNS requirements_ below): SPF include with the
   outbound IP, `RETURN_PATH_MX_HOST` → the host.
6. **Warm the IP up.** A new IP has no reputation. Keep
   `DEFAULT_DAILY_RECIPIENT_LIMIT` low and raise per-account limits over 2–4
   weeks while watching bounce and complaint rates. Register the IP with
   [Google Postmaster Tools](https://postmaster.google.com/) and
   [Microsoft SNDS](https://sendersupport.olc.protection.outlook.com/snds/).

**Delivery modes.**

| `POSTFIX_RELAYHOST`                 | Behaviour                                                                                                                                                                                                                                             |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| empty (recommended)                 | Postfix delivers to each recipient's MX. Gmail, Microsoft and Yahoo/AOL get their own throttled transports (`infra/postfix/provider_transport`, limits in `main.cf.tmpl`). Bounces come back to `bounce.<domain>` and are processed by the API. |
| `[smtp.example.net]:587` + username | Authenticated smarthost (credentials go only over verified TLS). Use when outbound 25 is blocked. Messages are still DKIM-signed by OpenDKIM. Most providers rewrite the envelope sender, so bounces then go to the provider, not to Mailvender. Configure the provider's bounce webhook or accept that bounce suppression is reduced. |

**Operations.**

- Queue: `docker compose -f docker-compose.prod.yml exec postfix mailq`.
  Flush with `postqueue -f`. Delete one message with `postsuper -d <queue id>`.
  The queue lives on the `postfix-queue` volume, so redeploys don't lose
  accepted mail. Back it up like a database if you snapshot the host.
- Logs: Postfix logs to stdout (`docker compose logs postfix`). TLS use is
  logged per delivery (`smtp_tls_loglevel = 1`).
- Throttling: if a provider starts deferring (`421`, `4.7.x` in the logs),
  lower its `*_destination_concurrency_limit` and rebuild Postfix. Postfix
  retries deferred mail with backoff of 5 min to 1 h for up to 2 days, then
  returns a DSN that the API records as a bounce.
- Scaling: one Postfix host handles hundreds of thousands of messages a day.
  Beyond that, or for dedicated IPs per customer and automatic per-provider
  backoff, see _Known limitations_ in [ARCHITECTURE.md](ARCHITECTURE.md).

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

- `SPF_INCLUDE_DOMAIN` (e.g. `_spf.mailvender.com`) TXT:
  `v=spf1 ip4:<outbound IPs> -all`.
- `RETURN_PATH_MX_HOST` (e.g. `feedback.mailvender.com`) A record → the
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

- One bucket, two top-level folders: `dev/` (local `docker-compose.yml`) and
  `prod/` (`docker-compose.prod.yml`). The compose file sets `AWS_S3_PREFIX`;
  production refuses to start with `dev/`.
- Private bucket; Block Public Access may stay on if the CDN (CloudFront with
  Origin Access Control) is the only public reader. Otherwise apply
  [`infra/bucket-policy.production.json`](../infra/bucket-policy.production.json)
  (public `GetObject` only on `dev/images/*/public/*` and `prod/images/*/public/*`).
- CORS: allow `POST` and `GET` from the app origin (browsers upload with presigned POST
  and download dataset content with presigned GET):
  [`infra/bucket-cors.json`](../infra/bucket-cors.json).
- Dataset content lives only in S3 (private) at `<prefix>sheets/<user id>/<dataset id>/<random>.json`
  (Postgres keeps a small summary + the key; each save is a new object, the old one is deleted);
  the bucket policy must not make them public.
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
