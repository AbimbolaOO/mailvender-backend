# Getting started with the Mailvender backend

This guide explains, in plain terms, what the backend is, how to run it on your
computer, how to use it, and what to do when something goes wrong. You don't
need to know the code to follow it.

> New to some of the tools mentioned here (Docker, PostgreSQL, Python)? Read
> [PREREQUISITES.md](PREREQUISITES.md) first. For the technical design, see
> [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Contents

1. [What the backend does](#1-what-the-backend-does)
2. [The pieces, in plain words](#2-the-pieces-in-plain-words)
3. [What you need installed](#3-what-you-need-installed)
4. [Set it up (first time)](#4-set-it-up-first-time)
5. [Start the studio (frontend)](#5-start-the-studio-frontend)
6. [Your first walk-through](#6-your-first-walk-through)
7. [How everyday things work](#7-how-everyday-things-work)
8. [Useful addresses and logins](#8-useful-addresses-and-logins)
9. [Everyday commands](#9-everyday-commands)
10. [Running the tests](#10-running-the-tests)
11. [Looking inside the database](#11-looking-inside-the-database)
12. [Using the API directly](#12-using-the-api-directly)
13. [Changing the API safely](#13-changing-the-api-safely)
14. [Troubleshooting](#14-troubleshooting)
15. [Going to production](#15-going-to-production)
16. [Glossary](#16-glossary)

---

## 1. What the backend does

Mailvender is an email designer. The **frontend** (the `mailvender/` folder)
is the studio where you design emails. It works in your browser and keeps your
work in the browser first.

The **backend** (this folder) is the server behind it. It:

- **Knows who you are.** It handles sign-up, email verification, login, logout
  and password resets.
- **Keeps your work safe.** It saves your pages, components, brand settings,
  data sources (spreadsheets) and version history in a database. You can
  then sign in on another computer and find everything there.
- **Lets teams share.** It manages accounts (a personal one for each user, plus
  team accounts), members, roles (owner/member), invitations and API keys.
- **Hosts images.** Images you upload get a permanent public link you can
  use in emails.
- **Sends email properly.** It only sends from domains you've proven you own,
  signs every email (DKIM), adds unsubscribe links, handles bounces and
  complaints, and never emails people who unsubscribed.
- **Protects everyone's reputation.** It enforces sending limits and suspends
  accounts that bounce or get too many spam complaints.

---

## 2. The pieces, in plain words

When you run `docker compose up`, these programs start. Each runs in its own
container (a small isolated box):

| Piece          | What it is                                   | Why it's there                                                                                                                           |
| -------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **postgres**   | The database                                 | Stores users, accounts, synced work, messages, everything durable.                                                                       |
| **api**        | The Mailvender API (Python/FastAPI)          | Answers the frontend's requests. Updates the database structure automatically when it starts.                                            |
| **worker**     | The background helper (same code as the API) | Does slow jobs so the API stays fast: sends queued emails, re-checks domains, enforces limits, cleans up old data.                       |
| **postfix**    | The mail server                              | Takes emails from the worker and delivers them. Locally it hands them to Mailpit instead of the internet. Also receives bounce messages. |
| **opendkim**   | The email signer                             | Adds a digital signature (DKIM) to each email so inboxes trust it.                                                                       |
| **mailpit**    | A fake inbox for development                 | Catches **every** email sent locally so you can read it in your browser. Nothing reaches real inboxes.                                   |
| **minio**      | Local file storage (like Amazon S3)          | Stores uploaded images locally.                                                                                                          |
| **minio-init** | A one-time setup job                         | Creates the image bucket and its "public read" rule, then **stops on purpose**. Showing `exited (0)` is normal.                          |

The **frontend** is _not_ started by Docker. You run it yourself (step 5),
because it's hosted separately in production.

How they connect:

```
 Browser ──► Studio (localhost:3000) ──/api/*──► API (localhost:8000) ──► Postgres
                                                     │
                                                     ▼ queues work
                                                  Worker ──► Postfix ──► OpenDKIM (signs)
                                                     │           └──► Mailpit (local inbox)
                                                     └──► MinIO (images)
```

---

## 3. What you need installed

| Tool                                                       | Why                                             | Check it's installed |
| ---------------------------------------------------------- | ----------------------------------------------- | -------------------- |
| Docker Desktop (running)                                   | Runs the backend pieces                         | `docker info`        |
| Node.js 24+ and npm                                        | Runs the studio (frontend)                      | `node --version`     |
| Git                                                        | Getting the code                                | `git --version`      |
| _Optional:_ Python 3.12 + [uv](https://docs.astral.sh/uv/) | Running backend tests or the API outside Docker | `uv --version`       |
| _Optional:_ DBeaver or `psql`                              | Looking at the database                         | —                    |

Details and learning links are in [PREREQUISITES.md](PREREQUISITES.md).

**Ports that must be free:** 3000 (studio), 8000 (API), 5434 (database),
8025 (Mailpit), 9000 and 9001 (MinIO), 2525 (mail server). If something else
uses one of them, see [Troubleshooting](#14-troubleshooting).

---

## 4. Set it up (first time)

The folder layout:

```
mailvender-repo/
├── mailvender/            ← the studio (frontend)
└── mailvender-backend/    ← this folder
    ├── backend/           ← API + worker code (Python)
    ├── infra/             ← mail server, signer and storage setup
    ├── docs/              ← you are here
    └── docker-compose.yml ← describes all the pieces
```

**Step 1. Open Docker Desktop** and wait until it says it's running.

**Step 2. Start the backend:**

```bash
cd mailvender-repo/mailvender-backend
docker compose up
```

The first run downloads and builds everything, which takes a few minutes. Later
runs take seconds. Keep this terminal open; it shows the logs. To run it in
the background instead, use `docker compose up -d`.

**Step 3. Check it's healthy.** In another terminal:

```bash
curl http://localhost:8000/health/ready
# {"status":"ok","database":"ok"}
```

or open http://localhost:8000/docs in a browser. You should see the
interactive API documentation.

**You don't need to configure anything.** All the development settings
(database password, secret keys, storage keys) are already in
`docker-compose.yml`. They're for local use only and must never be used in
production.

---

## 5. Start the studio (frontend)

In a new terminal:

```bash
cd mailvender-repo/mailvender
npm install        # first time, or after package.json changes
npm run dev
```

Open **http://localhost:3000**. You'll be sent to the login page.

The studio sends everything under `/api/...` to the API at
`http://localhost:8000`. That's why the browser only ever talks to
`localhost:3000`. If your API runs somewhere else, start the studio with
`API_INTERNAL_URL=http://that-address npm run dev`.

---

## 6. Your first walk-through

### 6.1 Create an account

1. On http://localhost:3000, click **Create an account**.
2. Enter any email address and a password (at least 10 characters).
3. **Open Mailpit at http://localhost:8025.** The verification email is
   there, not in your real inbox. Local development never sends real email.
4. Click the link in the email. You'll see "Your email is verified".
5. Log in. The studio opens.

> ⚠️ **Don't verify users by editing the database.** Verification also
> creates the user's personal account and makes them its owner. Setting
> `email_verified_at` by hand skips that, and the studio has no account to
> open. Always use the link in Mailpit.

### 6.2 Look around

- **The person icon** (bottom of the left toolbar) opens your account menu. It
  shows your email, the sync status ("All changes saved to the cloud"), your
  accounts (to switch between them), **Account settings** and **Log out**.
- Your existing designs are uploaded to your account the first time you sign
  in on this browser.

### 6.3 Set up sending (needed to send a test email)

Real sending requires a domain you own, proven through DNS records. Locally,
you can't publish DNS records, so there's a development-only shortcut.

1. Go to **Account settings → Sending domains**, type a made-up domain such
   as `mytest.example`, and click **Add domain**. You'll see the DNS records a
   real domain would need.
2. In a terminal, mark it verified (development only):
   ```bash
   cd mailvender-repo/mailvender-backend
   docker compose exec api python -m app.cli dev-verify-domain mytest.example
   ```
3. Refresh Settings. The domain shows **VERIFIED**.
4. Under **Senders**, add a sender (e.g. name "My News", address `news`) and
   click **Approve**.
5. Optional: under **Account**, fill in **Postal address**. Bulk emails
   (newsletters) require one by law.

### 6.4 Send a test email

1. In the studio, click the **eye icon** (Preview).
2. In the right panel, under **Send a test**, pick your sender. Leave "To" empty
   to send to yourself.
3. Click **Send test**. You'll see "Queued…" and then "Accepted by the mail
   server" with a message ID.
4. Open http://localhost:8025 to read it. It's DKIM-signed, just like the real
   thing.

### 6.5 Upload an image

Click the **Images** icon in the left toolbar, then **Upload images**. Click an
image to insert it, or select an image widget first to swap its picture. You
can also use **Upload an image** in an image widget's settings.

---

## 7. How everyday things work

### Signing in

- Your password is stored only as a scrambled hash (Argon2id). Nobody can
  read it back.
- Logging in gives your browser a **session cookie** the browser keeps hidden
  from JavaScript. It lasts 14 days, or until you log out.
- Too many attempts from one address or for one email get you a "Too many
  attempts" message for a while. This stops password guessing.
- "Forgot password" emails a one-time link (valid 60 minutes). Using it signs
  you out everywhere.

### Accounts and teams

- Everyone gets a **personal account** when they verify their email.
- You can create **team accounts** and invite people by email (Settings →
  Members). They must sign in with the invited address to accept.
- **Owners** can invite, remove members, transfer ownership and manage API
  keys. **Members** can do everything else: designs, domains, sending.
- An account always needs an owner. The last owner must hand over ownership
  before leaving.
- Everything you see belongs to the **active account** (chosen in the account
  menu). Switching accounts swaps the designs shown in the studio.

### Saving and sync

- The studio saves to your browser first. It works offline and never waits
  for the network.
- In the background it uploads changes and downloads changes made on your
  other computers (every ~20 seconds, and whenever you come back to the tab).
- If you edited something offline that someone else also changed, **your
  change wins** and you see a notice saying the other version was replaced.
- Logging out removes your designs from that browser (they're safe on the
  server). They come back when you sign in again.

### Sending email

1. You (or your server, with an API key) ask the API to send.
2. The API checks everything **before** accepting: valid addresses, an
   approved sender, a verified domain, nobody unsubscribed, within your
   limits. If anything is wrong, **nothing** is sent and you're told exactly
   which field is the problem.
3. If all is well, the API saves the message and answers right away with a
   **message ID**. The email is now "queued".
4. The worker picks it up, personalises the unsubscribe link for each
   recipient, and hands it to Postfix. OpenDKIM signs it. The status becomes
   "accepted by the mail server".
5. If Postfix is temporarily unavailable, the worker retries (waiting longer
   each time, up to 6 attempts) and then marks it "failed".

**Every request to send needs an `Idempotency-Key`.** That's a unique label
for that send. If your request is retried with the same key, the email is not
sent twice.

### Unsubscribes, bounces and complaints

- Bulk emails get an **unsubscribe link** and a one-click unsubscribe header
  (the button Gmail and Yahoo show). Clicking it adds the address to the
  account's **suppression list**. Future sends to it are refused.
- A **bounce** is the "couldn't deliver" reply a mail server sends back.
  Permanent bounces (e.g. "no such user") suppress the address immediately.
  Temporary ones only suppress after 3 within 30 days.
- A **complaint** is someone pressing "Report spam". It suppresses the address
  immediately.
- Suppressions from unsubscribes, bounces and complaints can't be removed (it
  would be illegal to email those people again). Ones you add manually can.

### Limits and suspension

- New accounts can send to **100 recipients per hour and 500 per day**.
  Operators can change this.
- If more than 5% of an account's emails bounce, or more than 0.1% get spam
  complaints (once it has sent at least 100), sending is **suspended**
  automatically. The account can still sign in and export its data.
  An operator reviews it and can reinstate it.

### Your data

- **Settings → Your data → Request export** builds a ZIP of everything
  (designs, data sources, image list, message history, suppressions). It's
  downloadable for 24 hours.
- **Delete this account** revokes access, stops pending sends, deletes images
  and schedules the rest for deletion after 30 days. How long each kind of
  data is kept is listed in [RETENTION.md](RETENTION.md).

---

## 8. Useful addresses and logins

| What                            | Address                                      | Login                              |
| ------------------------------- | -------------------------------------------- | ---------------------------------- |
| Studio                          | http://localhost:3000                        | your account                       |
| API docs (try requests here)    | http://localhost:8000/docs                   | —                                  |
| API contract (machine-readable) | http://localhost:8000/openapi.json           | —                                  |
| Mailpit (all local email)       | http://localhost:8025                        | —                                  |
| MinIO console (stored images)   | http://localhost:9001                        | `mailvender` / `mailvender-secret` |
| Database                        | `localhost:5434`, database `mailvender`      | `mailvender` / `mailvender`        |
| Test database                   | `localhost:5434`, database `mailvender_test` | `mailvender` / `mailvender`        |
| Mail server (bounce intake)     | `localhost:2525`                             | —                                  |

These are development-only values.

---

## 9. Everyday commands

Run these from `mailvender-backend/`:

| Task                               | Command                                                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Start everything                   | `docker compose up` (or `-d` for background)                                                                             |
| Stop everything (keeps data)       | `docker compose down`                                                                                                    |
| Stop and **erase all data**        | `docker compose down -v`                                                                                                 |
| See what's running                 | `docker compose ps`                                                                                                      |
| Follow the API's logs              | `docker compose logs -f api`                                                                                             |
| Follow the worker's logs           | `docker compose logs -f worker`                                                                                          |
| Rebuild after changing Python code | `docker compose up -d --build api worker`                                                                                |
| Mark a domain verified (dev only)  | `docker compose exec api python -m app.cli dev-verify-domain example.test`                                               |
| Make a user an operator            | `docker compose exec api python -m app.cli make-operator you@example.com`                                                |
| Run a scheduled job now            | `docker compose exec worker python -m app.cli run-job retention` (also `dns_recheck`, `account_health`, `dkim_key_sync`) |
| Apply database migrations by hand  | `docker compose exec api alembic upgrade head` (normally automatic)                                                      |
| Open a database shell              | `docker compose exec postgres psql -U mailvender mailvender`                                                             |

**About the logs:** each line is JSON with an `event` name (e.g.
`message.queued`), a `request_id`, and where known the `user_id`,
`account_id` and `message_id`. To find everything about one request, search
for its `request_id`. Every API response returns it in the `X-Request-ID`
header.

---

## 10. Running the tests

The tests use a real PostgreSQL database (`mailvender_test`, created
automatically by Docker).

```bash
cd mailvender-repo/mailvender-backend
docker compose up -d postgres          # the database must be running
cd backend
uv sync                                # first time: installs Python packages
uv run pytest
```

You should see `76 passed`. The tests wipe the `mailvender_test` database on
each run, so never point them at a database you care about.

Frontend checks:

```bash
cd mailvender-repo/mailvender
npm run lint && npm run typecheck && npm run build
```

---

## 11. Looking inside the database

Connect any PostgreSQL client (DBeaver, TablePlus, `psql`):

| Field           | Value                                                                |
| --------------- | -------------------------------------------------------------------- |
| Host            | `localhost`                                                          |
| Port            | **`5434`** (not 5432: that's often a Postgres installed on your Mac) |
| Database        | `mailvender`                                                         |
| User / password | `mailvender` / `mailvender`                                          |

Main tables:

| Table                                                                    | Holds                                                                                        |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `users`                                                                  | People who can sign in                                                                       |
| `accounts`, `memberships`                                                | Personal and team accounts, and who belongs to which (with role)                             |
| `sessions`, `email_tokens`, `invitations`, `api_keys`                    | Login sessions and one-time links. Only **hashes** are stored, never the secrets.            |
| `domains`, `dkim_keys`, `sender_identities`                              | Sending setup. DKIM private keys are **encrypted**.                                          |
| `messages`, `message_recipients`, `delivery_attempts`, `delivery_events` | Everything sent and what happened to it                                                      |
| `suppressions`                                                           | Addresses that must not be emailed, per account                                              |
| `outbox_events`                                                          | The worker's to-do list                                                                      |
| `sync_records`                                                           | Synced designs, data sources, brand and versions (JSON)                                      |
| `assets`                                                                 | Uploaded images                                                                              |
| `audit_events`                                                           | A permanent log of important actions. **Can't be edited or deleted** (the database refuses). |
| `account_exports`, `rate_limit_counters`, `job_runs`                     | Exports, rate-limit counters, scheduled job history                                          |

**Please don't fix things by editing rows by hand.** Many actions update
several tables together (see the warning in 6.1). Use the app, the API or the
CLI commands instead.

---

## 12. Using the API directly

The easiest way is **http://localhost:8000/docs**. Every endpoint is listed
there with its inputs and outputs.

### From a browser session

The studio does this for you. For reference, a browser request needs:

- the `mv_session` cookie (set by `POST /v1/auth/login`);
- `X-Account-Id: <account id>`, the account you're working in;
- `X-Requested-With: mailvender` on anything that changes data (POST, PUT,
  PATCH, DELETE). This protects against cross-site attacks.

### From your own server (API key)

1. **Settings → API keys → Create key** (owners only). Copy it; it's shown once.
2. Send with it:

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Authorization: Bearer mv_xxxx_yyyy" \
  -H "Idempotency-Key: order-1234-receipt" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "news@mytest.example",
    "to": ["someone@example.org"],
    "subject": "Your receipt",
    "html": "<p>Thanks for your order!</p>",
    "category": "transactional"
  }'
```

You get `202 Accepted` with a message `id`. Check its progress with
`GET /v1/messages/{id}`. API keys can send, read messages, domains and senders,
and read suppressions. They can't manage members or keys.

### Bulk email requirements

With `"category": "bulk"` (newsletters, mail merge), the request must have:

- a plain-text version (`text`);
- `{{unsubscribe_url}}` somewhere in the HTML (the API swaps in a personal
  link for each recipient);
- the account's postal address set in Settings, and shown in the email
  (`{{physical_address}}`);
- a domain with a DMARC record (shown as "Bulk ready").

In the studio, the matching merge tags are `@unsubscribe_url`,
`@view_in_browser_url` and `@physical_address`. Type `@` in any text to pick
them.

### What errors look like

Every error has the same shape:

```json
{
  "error": {
    "code": "validation_failed",
    "message": "Nothing was sent – fix the listed fields and try again.",
    "fields": [
      {
        "field": "to[1]",
        "code": "invalid_email",
        "message": "This isn't a valid email address."
      }
    ],
    "request_id": "3f0c…"
  }
}
```

`code` is stable, so your program can check it. `message` is safe to show to
users.

### Lists

Lists return up to 25 items by default (maximum 100 with `?limit=100`). If
`next_cursor` isn't null, pass it as `?cursor=…` to get the next page.

---

## 13. Changing the API safely

1. Change the Python code in `backend/app/`.
2. If you changed the database models (`backend/app/models.py`), create a
   migration:
   ```bash
   cd backend
   uv run alembic revision --autogenerate -m "describe the change"
   ```
   Read the generated file in `backend/alembic/versions/` before committing.
3. Run the tests: `uv run pytest`.
4. If endpoints or their inputs/outputs changed, refresh the contract and the
   frontend's types:
   ```bash
   uv run python -m app.cli openapi > openapi.json
   cd ../../mailvender && npm run api:types
   ```
   CI fails if `openapi.json` doesn't match the code.
5. Rebuild the containers: `docker compose up -d --build api worker`.

Where things live in the code is explained in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 14. Troubleshooting

| Problem                                                       | Cause and fix                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No verification email**                                     | Local email goes to **Mailpit**, http://localhost:8025, not your real inbox. If it isn't there, check `docker compose logs worker` for `system_email.sent`.                                                                                                                              |
| **Login shows "no workspace to open"**                        | The user has no account. This happens if they were verified by editing the database. A developer must create the missing personal account, an `owner` membership and `users.default_account_id`, in one transaction, or sign up again with a different email and verify through Mailpit. |
| **"Verify your email address first"**                         | Open the link in Mailpit, or click "Send the verification email again" on the login page.                                                                                                                                                                                                |
| **"Too many attempts"**                                       | Rate limiting. Wait (up to an hour for sign-up/reset, 15 minutes for login), or for local testing clear it: `docker compose exec postgres psql -U mailvender mailvender -c "TRUNCATE rate_limit_counters"`.                                                                              |
| **DBeaver: `role "mailvender" does not exist`**               | You reached another Postgres on port 5432. Use port **5434**.                                                                                                                                                                                                                            |
| **`port is already allocated` when starting**                 | Another program uses that port. Stop it, or change the left-hand number in `ports:` in `docker-compose.yml`.                                                                                                                                                                             |
| **`minio-init` shows "exited (0)"**                           | Normal: it's a one-time setup job.                                                                                                                                                                                                                                                       |
| **Image upload fails**                                        | Check MinIO is running (`docker compose ps`) and that `minio-init` exited with 0. If you erased volumes, run `docker compose up minio-init`. Only PNG, JPEG, GIF and WebP up to 5 MB are allowed.                                                                                        |
| **Test send: "Send from an approved sender"**                 | Add a domain, mark it verified (6.3), add a sender and click **Approve**.                                                                                                                                                                                                                |
| **Test send stays "Queued"**                                  | Check `docker compose logs worker` and `docker compose logs postfix`. If Postfix says `milter-reject … Service unavailable`, OpenDKIM isn't reachable: `docker compose restart opendkim postfix`.                                                                                        |
| **"Sending is suspended"**                                    | Limits, bounces or complaints. Make yourself an operator and reinstate it (`POST /v1/operator/accounts/{id}/reinstate`), or for local testing start fresh.                                                                                                                               |
| **"sending_limit_exceeded"**                                  | Over 100 recipients/hour or 500/day. Change the limits with the operator API (`PUT /v1/operator/accounts/{id}/limits`).                                                                                                                                                                  |
| **Studio says "Offline – changes are kept here"**             | The API isn't reachable from the studio. Is the backend running? Is `API_INTERNAL_URL` right? Your changes upload once it's back.                                                                                                                                                        |
| **Signed in on Safari but immediately signed out**            | Safari doesn't keep "Secure" cookies on `http://localhost`. Use Chrome/Firefox, or add `SESSION_COOKIE_SECURE: "false"` to the API's environment in `docker-compose.yml` (local only).                                                                                                   |
| **API container exits with code 132 ("illegal instruction")** | An old Docker Desktop on Apple Silicon can't run the newest `cryptography` library. It's pinned below 47 for this reason; update Docker Desktop before raising the pin.                                                                                                                  |
| **Everything is broken and I want a clean slate**             | `docker compose down -v && docker compose up --build`. This **erases all local data**.                                                                                                                                                                                                   |

---

## 15. Going to production

Local settings are not production settings. Before deploying, read
[RUNBOOK.md](RUNBOOK.md). It covers the required environment variables (all
listed in [`../.env.example`](../.env.example)), real secrets, DNS records,
AWS S3 setup, the mail server, migrations, backups, key rotation and
monitoring.

---

## 16. Glossary

| Term                             | Meaning                                                                                               |
| -------------------------------- | ----------------------------------------------------------------------------------------------------- |
| **API**                          | The server program the studio talks to.                                                               |
| **Account**                      | A workspace that owns designs, domains and messages. Personal or team.                                |
| **Owner / member**               | Roles within an account. Owners manage people and keys.                                               |
| **Session**                      | Your signed-in state, kept in a cookie.                                                               |
| **API key**                      | A password-like key for programs (not people) to send email.                                          |
| **Domain verification**          | Proving you own a domain by adding DNS records.                                                       |
| **DNS record**                   | A setting on a domain (at your domain registrar) that the internet can look up.                       |
| **DKIM**                         | A digital signature on each email that proves it came from your domain.                               |
| **SPF**                          | A DNS record listing which servers may send for a domain.                                             |
| **DMARC**                        | A DNS policy telling inboxes what to do if DKIM/SPF fail. Required for bulk email.                    |
| **Return-Path / bounce address** | Where "couldn't deliver" replies go. Mailvender uses `bounce.<your domain>`.                          |
| **Bounce**                       | A "couldn't deliver" reply. **Hard** = permanent, **soft** = temporary.                               |
| **Complaint**                    | Someone marked your email as spam.                                                                    |
| **Suppression**                  | An address the account must not email any more.                                                       |
| **Bulk vs transactional**        | Bulk = marketing/newsletters (strict rules). Transactional = one-off messages like receipts or tests. |
| **Idempotency key**              | A unique label per send, so retries never send twice.                                                 |
| **Outbox**                       | The worker's to-do list in the database.                                                              |
| **Worker**                       | The background program that sends email and runs scheduled jobs.                                      |
| **Migration**                    | A versioned change to the database structure (Alembic).                                               |
| **Sync / revision**              | How designs are kept the same across computers. Each saved version gets a revision number.            |
| **Tombstone**                    | A marker that a record was deleted, so other devices delete it too.                                   |
| **Mailpit**                      | A fake inbox for development.                                                                         |
| **MinIO / S3**                   | File storage. MinIO is the local stand-in for Amazon S3.                                              |
| **Postfix**                      | The mail server that delivers email.                                                                  |
| **OpenDKIM**                     | The program that adds DKIM signatures.                                                                |
| **Operator**                     | Mailvender staff who can review and suspend accounts.                                                 |
