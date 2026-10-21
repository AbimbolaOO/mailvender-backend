# Production runbook

## Components

| Service    | Image / command             | Notes                                                                                                                                                                                                                    |
| ---------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| API        | `backend/Dockerfile`, `api` | Runs `alembic upgrade head` (advisory-locked, safe with several replicas), then uvicorn on :8000. Readiness: `/health/ready` (fails without the database). Liveness: `/health/live`.                                     |
| Worker     | same image, `worker`        | Waits until migrations are at head. Consumes the outbox (customer sends, system emails, admin emails, customer webhooks, exports). Jobs: DNS rechecks (10 min), platform DKIM (5 min), account health (5 min), alerts (10 min), blocklists (`reputation`, 1 h), retention (daily), DKIM key publication (30 s). Jobs are advisory-locked – run as many workers as you like. |
| Postfix    | `infra/postfix`             | Relays only for the worker's address (`POSTFIX_MYNETWORKS`); accepts inbound mail only for `b-<id>@bounce.*` (bounces), piped to `/internal/dsn`. Queue on the `postfix-queue` volume. See _Production mail server_. |
| OpenDKIM   | `infra/opendkim`            | Sign-only milter. Reads keys the worker writes to a private volume shared with it only; reloads on change. Postfix defers (never sends unsigned) if it's down.                                                           |
| Next.js    | `mailvender/` (Vercel)      | The studio. Proxies `/api/*` to the API (`API_INTERNAL_URL`), so cookies are first-party.                                                                                                                                |
| Admin      | `mailvender-admin/` (Vercel) | Staff dashboard. Proxies only `/api/v1/admin/*`, adding `X-Mailvender-Admin-Secret` (`ADMIN_PROXY_SECRET`); without it the API answers 404 to all of `/v1/admin`. See docs/ADMIN_SETUP.md.                            |
| Email Worker | `infra/email-worker/` (Cloudflare) | Receives support@/postmaster@/abuse@/dmarc@ for the admin Inbox, keeps a Gmail copy, posts to `/internal/inbound-mail` signed with `INBOUND_MAIL_SECRET`. See docs/ADMIN_SETUP.md part C.                       |
| PostgreSQL | managed                     | ≥ 14.                                                                                                                                                                                                                    |
| S3 + CDN   | AWS                         | See _Asset storage_.                                                                                                                                                                                                     |

## Environment variables

All names are in [`.env.example`](../.env.example). Secrets come from your
secret store and are never committed: `DATABASE_URL`, `SECRET_KEY`,
`DKIM_ENCRYPTION_KEY`, `INTERNAL_API_TOKEN`, `API_PROXY_SECRET`, `ADMIN_PROXY_SECRET`,
`INBOUND_MAIL_SECRET`, `FEEDBACK_LOOP_SECRETS`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `ALERT_WEBHOOK_URL`. The API never returns any of them;
DKIM private keys, TOTP secrets and customers' webhook signing secrets are
encrypted at rest (Fernet) – DKIM keys are only decrypted onto the OpenDKIM
volume.

Optional groups (defaults are fine; each is described in `.env.example` and
`backend/app/config.py`): sending limits and account health (`DEFAULT_*`,
`HEALTH_*`), retention (`RETENTION_*`), the admin (`ADMIN_*`), alerts
(`ALERT_*`, `ADMIN_APP_URL`), blocklists (`BLOCKLIST_*`, `LINK_BLOCKLIST_ZONES`,
`KNOWN_SENDING_IPS`), webhooks (`WEBHOOK_*` – the two `WEBHOOK_ALLOW_*` flags are
for local development only and refused in production), and
`PWNED_PASSWORDS_CHECK`.

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
- Platform DKIM for the `SYSTEM_FROM_EMAIL` domain (system emails):
  `python -m app.cli platform-dkim` prints the `<selector>._domainkey` TXT
  record; the `platform_dkim` job activates the key once it's published, and
  `platform-dkim rotate` starts a rotation. Customers can't add this domain or
  its subdomains.

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
- **`API_PROXY_SECRET`**: set the new value on the frontend host (Vercel) and in
  the API's `.env`, then redeploy both. In between, requests via the frontend
  count against Vercel's IP in the per-IP rate limits.
- **`ADMIN_PROXY_SECRET`**: new value in the server's `.env` and in the admin's
  Vercel project; redeploy both back to back (the admin is down in between).
- **`INBOUND_MAIL_SECRET`**: `npx wrangler secret put INBOUND_MAIL_SECRET` in
  `infra/email-worker/` and the server's `.env`; until both match, the Gmail copy
  still arrives but the admin Inbox misses messages.
- **Customers' webhook secrets** are rotated by the customer (Settings →
  Webhooks → New secret, or `POST /v1/webhooks/{id}/rotate-secret`).
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
  `<bucket>/<prefix>*`, and `s3:ListBucket` on the bucket limited to that prefix
  (`"Condition": {"StringLike": {"s3:prefix": ["prod/*"]}}`) – the admin's
  backup checks list `prod/backups/db/` and `prod/logs/`. Without it they always
  say "none found".
- `AWS_S3_PUBLIC_BASE_URL` = the CDN URL. Finalized assets are immutable
  (`Cache-Control: max-age=31536000, immutable`); deleting one also needs a CDN
  invalidation if it must disappear immediately.

## Backup and restore

- PostgreSQL: on top of the managed service's own backups,
  `infra/db-backup/backup-db.sh` (6-hourly systemd timer) dumps the `public`
  schema with `pg_dump -Fc`, encrypts it with age to a public key whose private
  key is kept offline, and uploads it to
  `<prefix>backups/db/<YYYY>/<MM>/<DD>/` (`app.cli upload-backup` refuses
  unencrypted files). Expire with a lifecycle rule. Restore and setup:
  PRODUCTION_SETUP step 14. Test a restore every few months.
- S3: versioning on with a 30-day noncurrent-version expiry.
- Container logs: Docker keeps 250 MB per service; `infra/log-backup/backup-logs.sh`
  (hourly systemd timer) copies them to `<prefix>logs/<service>/<YYYY>/<MM>/<DD>/`
  in the bucket as gzip files. They contain email addresses and IPs: keep them
  private and expire them with a lifecycle rule (PRODUCTION_SETUP step 14).
- The OpenDKIM volume is derived data – the worker rebuilds it from the
  database within 30 s.
- Restore order: database → API/worker (migrations no-op) → verify
  `/health/ready` → resume Postfix.

## Monitoring and alerting

**Alerts are automatic.** The worker's `alerts` job runs the admin's health
checks every 10 minutes (port 25, background jobs, the queue, platform DKIM,
backups, blocklists) and, when one turns red, emails `ALERT_EMAILS` (blank = the
active admins) and posts to `ALERT_WEBHOOK_URL` – once; again when it's fixed;
and a daily reminder while it isn't. Set the webhook (a Slack "Incoming
Webhook", a Discord channel webhook, …): email alerts can't arrive while our own
mail is broken. Alerts link to the admin at `ADMIN_APP_URL`
(https://backoffice.mailvender.com, the default in `docker-compose.prod.yml`).

**From outside** (PRODUCTION_SETUP step 20): Better Stack checks
`https://api.mailvender.com/health/ready`, the studio's and admin's `/login`
pages and port 25 on `mail.mailvender.com`. The worker requests `HEARTBEAT_URL`
every `HEARTBEAT_MINUTES` (5) while it can reach the database (job
`heartbeat`, logged as `job.heartbeat.completed result=sent|failed`; a failed
ping is only a warning). If the server, worker or database is down, the pings
stop and Better Stack alerts. Nothing inside the server could report that.

### Blocklists

The `reputation` job checks every IP in `KNOWN_SENDING_IPS` against
`BLOCKLIST_IP_ZONES` (Spamhaus ZEN, SpamCop, Barracuda) and the platform domain
against `BLOCKLIST_DOMAIN_ZONES` (Spamhaus DBL) each hour. A listing shows on
the admin's Overview/System (with the removal page) and triggers an alert.
Then: find the account that caused it (Spam & delivery → accounts with the most
complaints), suspend it, and request removal.

Spamhaus refuses queries that come through big public resolvers (8.8.8.8,
1.1.1.1) – the admin then says "Blocklists couldn't be checked". The server's
default (Hetzner's) resolvers work; if not, run a local resolver (`unbound`).
Their free service is for low volumes; once we send a lot, get a free
**Spamhaus DQS** key and set the zones to `<key>.zen.dq.spamhaus.net` and
`<key>.dbl.dq.spamhaus.net`.

Links in customers' emails are checked against `LINK_BLOCKLIST_ZONES`
(Spamhaus DBL) when they send: an email linking to a known phishing, malware or
spam domain is refused (422 `blocklisted_link`), before it can hurt our IP.

Logs are JSON lines (`event`, `request_id`, `user_id`, `account_id`,
`message_id`), backed up hourly to S3 (see Backup and restore).

`infra/status.sh` (read-only) checks containers, health, migrations, port 25,
platform DKIM, the mail queue, reverse DNS, TLS expiry, last log/database
backups, recent errors and disk/memory, and exits 1 if anything needs
attention: `ssh root@<server> /opt/mailvender/mailvender-backend/infra/status.sh`.

Beyond the automatic alerts, these log events are worth a dashboard or a
log-based alert once there's volume:

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
| Customer webhooks | `webhook.failed`, `webhook.disabled`                                                                    | many disabled at once = our side is failing |

## Account health

Rolling window `HEALTH_WINDOW_DAYS` (7) over recipient-level events: bounce
rate = hard bounces / accepted, complaint rate = complaints / accepted,
evaluated only with ≥ `HEALTH_MIN_VOLUME` (100) accepted recipients. Above
`HEALTH_MAX_BOUNCE_RATE` (5 %) or `HEALTH_MAX_COMPLAINT_RATE` (0.1 %) the
account is suspended automatically; so is an account that hits its hourly or
daily limit `LIMIT_VIOLATIONS_BEFORE_SUSPENSION` (20) times in an hour.
Operators review and act in the admin dashboard (**Accounts**, **Needs help**;
docs/ADMIN_SETUP.md) – or through `GET /v1/operator/accounts` with
`…/suspend`, `…/reinstate`, `PUT …/limits`, `…/reviews`. All audited.

Soft bounces suppress an address after `SOFT_BOUNCE_THRESHOLD` (3) within
`SOFT_BOUNCE_WINDOW_DAYS` (30); hard bounces and complaints suppress at once.

## Webhooks (customer endpoints)

Owners add https endpoints in Settings → Webhooks (or `/v1/webhooks`); the
customer guide is [WEBHOOKS.md](WEBHOOKS.md). When an event happens the API
writes one `webhook_deliveries` row per listening endpoint and an outbox event;
the worker POSTs it signed (`Mailvender-Signature`), with a 10 s timeout, and
retries non-2xx answers with backoff (30 s … 12 h, 10 tries, about 1.5 days).
An endpoint whose every event fails for 3 days is turned off and its owners are
emailed. Deliveries are kept `RETENTION_WEBHOOK_DELIVERIES_DAYS` (30).

Safety: only https, never to private, loopback, link-local or cloud-metadata
addresses (checked on save and again before every request, no redirects
followed), only the first 1 KB of the answer is stored. A customer saying "we
don't get webhooks": look at their endpoint's deliveries in the app (status,
HTTP code, their server's answer) – most often their server answers 4xx/5xx or
times out.

## Feedback loops (complaints)

Configure each provider to POST ARF reports to
`https://<api>/internal/feedback-loop/<name>` with header
`X-Mailvender-Signature: sha256=<HMAC-SHA256(body, secret)>`, and add
`<name>:<secret>` to `FEEDBACK_LOOP_SECRETS`. (For providers that only email
ARF reports, forward them through a small relay that signs the body.)

## Admin dashboard

Setup and daily use: docs/ADMIN_SETUP.md. Recovery, on the server
(`docker compose -f docker-compose.prod.yml exec api python -m app.cli …`):

| Situation | Command |
| --- | --- |
| Lost phone / forgotten password | `reset-admin <email>` – new temporary password, 2FA cleared, sessions ended |
| Admin leaves | `disable-admin <email>` |
| New admin | Only one active admin is allowed: `disable-admin <old>`, then `create-admin <email>` (temporary password printed once) |
| Who are the admins | `list-admins` |
| Admin key leaked | New `ADMIN_PROXY_SECRET` on the server and in Vercel; redeploy both |
| Admin API must be off | Remove `ADMIN_PROXY_SECRET`, redeploy – all of `/v1/admin` answers 404 |

Admins can also: write email with attachments from support@/postmaster@/abuse@
(sent by the worker through Postfix, platform-DKIM signed; Inbox → Sent), sign a
customer out everywhere, send a password reset, and turn off a customer's
two-factor sign-in after checking it's really them – all audited.

Role inboxes (support@, postmaster@, abuse@, dmarc@) reach the API from the Cloudflare
Email Worker (`infra/email-worker/`) at `/internal/inbound-mail`, signed with
`INBOUND_MAIL_SECRET`. If messages stop appearing, check the worker's logs in
Cloudflare; the Gmail copy keeps arriving either way. ARF reports at abuse@ go
through the same complaint processing as `/internal/feedback-loop` (source
`abuse-mailbox`).
