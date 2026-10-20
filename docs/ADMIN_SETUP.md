# Mailvender Admin: setup guide

Mailvender Admin is the private dashboard for Mailvender staff. In it you can:

- read mail sent to `postmaster@`, `abuse@` and `dmarc@mailvender.com`;
- see daily usage, spam-complaint and bounce rates, and the health of the server;
- find customers who are stuck, with a plain-words fix for each problem;
- block and unblock users, and suspend, reinstate or change limits on accounts.

The code is in `mailvender-admin/` (Next.js, next to `mailvender/`). The admin API it
uses is part of the backend (`/v1/admin/*`).

This guide has four parts:

- [A. Run it on your Mac](#a-run-it-on-your-mac)
- [B. Put it in production](#b-put-it-in-production)
- [C. Role inboxes: postmaster@, abuse@, dmarc@](#c-role-inboxes-postmaster-abuse-dmarc)
- [D. Day to day](#d-day-to-day)

---

## How it stays private

```text
  your browser ──► admin.mailvender.com (Vercel) ──► api.mailvender.com (Hetzner)
                   proxy.ts adds the admin key          /v1/admin/* answers only
                   (ADMIN_PROXY_SECRET)                 requests carrying that key
```

- **The browser never talks to the API.** It only calls the admin app's own
  `/api/v1/admin/...`. The admin app's server (on Vercel) adds the secret admin
  key and forwards the request. The key and the API's address never reach the
  browser.
- **Without the key, the admin API doesn't exist.** Anyone who calls
  `api.mailvender.com/v1/admin/...` directly gets the same `404 Not Found` as
  for a made-up address. The customer app's key (`API_PROXY_SECRET`) doesn't
  work here either.
- **Admins are separate from customers.** They have their own table, their own
  cookie and their own sign-in. A customer's password reset can never touch an
  admin.
- **Sign-in has two steps**: a password, then a 6-digit code from an
  authenticator app on your phone. The first time, you must change the
  temporary password and set up the app before anything else works.
- **Sessions end on their own**: after 30 minutes without activity, and after
  8 hours in total. Ten wrong attempts lock the admin for 30 minutes.
- **Every action is written to the audit log** (who, from which IP, and why).
  This includes opening a customer or an inbox message. The log can't be
  changed. Customers never see admin entries.
- **Customer content stays private.** The admin shows counts, statuses and
  error messages. It never shows customers' email bodies, templates, lists or
  API keys.
- The pages carry strict security headers: CSP with nonces, no framing,
  `noindex`, and `robots.txt` disallows everything.

---

## A. Run it on your Mac

You need the backend running locally (`docker compose up`, see
GETTING_STARTED.md) and Node.js.

### A1. Update the backend

The admin needs the new database tables (migration `0004`) and the admin API.
Rebuild the API and the worker; the migration runs on its own when the API
starts:

```sh
cd mailvender-backend
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose up -d --build api worker
docker compose exec api alembic current        # → 0004 (head)
```

`docker-compose.yml` already sets the local secrets:
`ADMIN_PROXY_SECRET=dev-admin-proxy-secret` and
`INBOUND_MAIL_SECRET=dev-inbound-mail-secret`.

### A2. Install the admin app

```sh
cd ../mailvender-admin
npm install
cp .env.example .env.local
```

The values in `.env.example` are already right for local use:

```sh
API_INTERNAL_URL=http://localhost:8000
ADMIN_PROXY_SECRET=dev-admin-proxy-secret     # same as docker-compose.yml
```

### A3. Create your admin

```sh
cd ../mailvender-backend
docker compose exec api python -m app.cli create-admin you@example.com
```

It prints a **temporary password** once. Copy it.

Only **one active admin** is allowed. If it says "Only 1 active admin allowed", run `list-admins` to see who it is, and either use `reset-admin` on that one or `disable-admin` it first.

### A4. Start it and sign in

```sh
cd ../mailvender-admin
npm run dev
```

Open **http://localhost:3100** in Chrome or Firefox, then:

1. Sign in with your email and the temporary password.
2. **Choose your own password**: at least 14 characters, ideally a few
   unrelated words.
3. **Set up two-factor sign-in.** Install Google Authenticator, Microsoft
   Authenticator, 1Password or Authy. Click **Show my QR code**, scan it, and
   type the 6-digit code the app shows.
4. **Save the 10 recovery codes** in your password manager. Each lets you sign
   in once if you lose your phone.

From then on, signing in asks for your password, then a code from the app.

### A5. Try the inbox locally

Send a test message the way the Cloudflare Email Worker would:

```sh
cd ../mailvender-backend
INBOUND_MAIL_SECRET=dev-inbound-mail-secret infra/email-worker/send-test.sh postmaster
INBOUND_MAIL_SECRET=dev-inbound-mail-secret infra/email-worker/send-test.sh abuse
```

Each answers `{"id": "...", "created": true, "kind": "plain"}`. The messages
then show up in the admin under **Inbox**.

### A6. Checks while developing

```sh
cd mailvender-admin
npm run lint && npm run typecheck && npm test && npm run build
```

Backend tests (`cd mailvender-backend/backend && uv run pytest`) cover the
admin API in `tests/test_admin.py`.

**After changing the admin API**, regenerate the contract and the admin app's
types:

```sh
cd mailvender-backend/backend
uv run python -m app.cli openapi --admin > openapi-admin.json
cd ../../mailvender-admin && npm run api:types
```

### If something doesn't work locally

| What you see | Why, and what to do |
| --- | --- |
| A different website opens on the admin's address | A leftover service worker from another project is answering in your browser. The admin uses port 3100 to avoid this. If it still happens, open DevTools → Application → Service workers → Unregister. |
| You're sent back to the sign-in page after signing in | The session cookie is `Secure`. Chrome and Firefox accept it on `http://localhost`, but Safari doesn't and neither does `127.0.0.1`. Use `localhost` in Chrome or Firefox. |
| “ADMIN_PROXY_SECRET isn't set on the admin app's server” | `.env.local` is missing. Copy `.env.example` (A2) and restart `npm run dev`. |
| Every request fails with “Not found” | The two keys don't match. `ADMIN_PROXY_SECRET` in `.env.local` must equal the one in `docker-compose.yml`. |
| “Too many attempts” | Admin sign-in allows 5 tries per 15 minutes. Wait 15 minutes; it resets by itself. |

---

## B. Put it in production

Do these in order. B1–B4 are on the backend (server), B5–B8 on Vercel.

### B1. Make two new secrets (on your Mac)

```sh
openssl rand -hex 32     # → ADMIN_PROXY_SECRET
openssl rand -hex 32     # → INBOUND_MAIL_SECRET
```

Save both in your password manager. Each must be different from every other
secret, including `API_PROXY_SECRET`.

### B2. Add them to the backend's settings

Edit `mailvender-backend/.env.production` on your Mac and add:

```sh
ADMIN_PROXY_SECRET=<the first secret>
INBOUND_MAIL_SECRET=<the second secret>
KNOWN_SENDING_IPS=2.29.100.188
```

`KNOWN_SENDING_IPS` is our server's IP. Anyone else sending as mailvender.com
is flagged in **Spam & delivery**.

Copy the file to the server, as in PRODUCTION_SETUP step 7:

```sh
scp mailvender-backend/.env.production root@2.29.100.188:/opt/mailvender/mailvender-backend/.env
```

### B3. Redeploy the backend

Follow PRODUCTION_SETUP step 15 (rsync, then rebuild). The API runs migration
`0004` by itself when it starts.

On the server, check that the migration ran and that the admin API is hidden:

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml exec api alembic current   # → 0004 (head)
curl -s -o /dev/null -w "%{http_code}\n" https://api.mailvender.com/v1/admin/auth/me   # → 404 (good)
```

### B4. Create your admin account (on the server)

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml exec api python -m app.cli create-admin you@yourdomain.com
```

Copy the temporary password it prints. You'll use it in B8.

Only **one active admin** is allowed (`ADMIN_MAX_ACCOUNTS`, default 1), so this is the only admin the system will have.

### B5. Create the Vercel project

The admin is a second Vercel project, separate from the customer app. The
simplest way is the Vercel command line, run from your Mac:

```sh
cd mailvender-admin
npx vercel login
npx vercel link          # "Set up and deploy?" → yes · create a NEW project · name it e.g. mailvender-admin
```

*(Alternatively, put `mailvender-admin/` in its own GitHub repository and import
it in Vercel → Add New → Project. Framework: Next.js, root directory: the
project folder.)*

### B6. Add the two settings in Vercel

Go to Vercel → **mailvender-admin → Settings → Environment Variables** and add
these for **Production**. Tick **Sensitive** on the secret.

| Name | Value |
| --- | --- |
| `API_INTERNAL_URL` | `https://api.mailvender.com` |
| `ADMIN_PROXY_SECRET` | the first secret from B1, exactly the same as on the server |

Never add `NEXT_PUBLIC_` in front of either name: that would send it to the
browser.

Then deploy:

```sh
npx vercel deploy --prod
```

### B7. Give it its own address

1. Vercel → **mailvender-admin → Settings → Domains → Add**, and type
   `admin.mailvender.com`. Any less obvious name is a bit more private, for
   example `ops-7k2.mailvender.com`.
2. Vercel shows a CNAME record. In Cloudflare → **mailvender.com → DNS → Add
   record**:

   | Type | Name | Target | Proxy |
   | --- | --- | --- | --- |
   | CNAME | `admin` (or your name) | `cname.vercel-dns.com` | **DNS only** (grey cloud) |

3. Wait until Vercel shows the domain as **Valid** (a few minutes). It gets an
   HTTPS certificate automatically.

Don't link to this address anywhere public: the website, the customer app or
emails.

### B8. First sign-in

Open `https://admin.mailvender.com`, sign in with the temporary password from
B4, and follow the steps: new password, authenticator app, save the recovery
codes (A4 explains each one).

Check that it works:

- **Overview** loads. Its "Is everything OK?" card should mostly be green.
  Port 25 shows red until Hetzner unblocks it.
- **System** lists the background jobs with recent times.

### B9. Optional: an extra lock

You can let only your own devices reach the sign-in page:

- **Vercel → Settings → Deployment Protection**: password protection or
  Vercel Authentication (paid plans); or
- **Vercel → Firewall → Add rule**: "IP address is not in [your home/office
  IPs]" → Deny.

This is on top of the admin key, the password and the code. It isn't
required.

---

## C. Role inboxes: postmaster@, abuse@, dmarc@

First finish PRODUCTION_SETUP step 13a. That sets up Cloudflare Email Routing,
which forwards these three addresses to your Gmail, and the `rua=` address in
`_dmarc`.

Here, a small **Cloudflare Email Worker** takes over for those three
addresses. For each message, it:

1. still forwards a copy to your Gmail, as a backup;
2. sends the message to the API, signed with `INBOUND_MAIL_SECRET`. It then
   appears in the admin's **Inbox**.

None of this involves port 25 or the server's Postfix, so it already works
while port 25 is blocked.

### C1. Deploy the worker (from your Mac)

```sh
cd mailvender-backend/infra/email-worker
```

Open `wrangler.toml` and put your Gmail address in `FORWARD_TO`. It must be a
verified destination in Email Routing (you did this in 13a). Then:

```sh
npx wrangler login
npx wrangler secret put INBOUND_MAIL_SECRET      # paste the second secret from B1
npx wrangler deploy
```

### C2. Point the three addresses at the worker

Cloudflare → **mailvender.com → Email → Email Routing → Routing rules**. For
`postmaster@`, `abuse@` and `dmarc@`:

1. Click **Edit**.
2. Set **Action** to **Send to a Worker**.
3. Set **Destination** to **mailvender-role-mail**.
4. Click **Save**.

The worker forwards to your Gmail itself, so you still get every message
there.

### C3. Test it

Send an email from your personal Gmail to `postmaster@mailvender.com`.
Within a minute it should appear in the admin's **Inbox → postmaster@** and in
your Gmail.

Or test the API part from your Mac:

```sh
cd mailvender-backend
INBOUND_MAIL_SECRET=<second secret> infra/email-worker/send-test.sh postmaster https://api.mailvender.com mailvender.com
```

If nothing arrives, check Cloudflare → **Workers & Pages →
mailvender-role-mail → Logs**. The worker logs the API's answer when it isn't
OK.

### What happens to each kind of message

| Mailbox | What the admin does with it |
| --- | --- |
| postmaster@ | Shown in the Inbox. Microsoft SNDS sends its confirmation link here (13a). |
| abuse@ | Shown in the Inbox. Complaint reports (e.g. from Microsoft JMRP) are also matched to the customer's email and suppressed, like any spam complaint, and count towards that account's complaint rate. |
| dmarc@ | Shown in the Inbox, and the daily DMARC reports are read: **Spam & delivery → DMARC reports** shows who sent as mailvender.com and whether it passed. |

Reading safely: emails open as plain text. **Formatted (safe)** shows the
design in a sandboxed frame where scripts can't run. Pictures from the
internet stay hidden until you click **Show pictures**, so senders can't track
you. Attachments are only ever downloaded, never opened in the page.

Messages are kept for 180 days and DMARC report rows for a year. The daily
retention job deletes older ones.

---

## D. Day to day

| Task | How |
| --- | --- |
| Hand the admin over to someone else | Only one active admin is allowed. On the server: `... python -m app.cli disable-admin you@example.com`, then `docker compose -f docker-compose.prod.yml exec api python -m app.cli create-admin them@example.com`, and send them the temporary password through a password manager, not plain email. |
| Lost phone or forgotten password | `... python -m app.cli reset-admin you@example.com`. You get a new temporary password, two-factor is cleared and all sessions end. Sign in and set it up again. |
| Remove an admin | `... python -m app.cli disable-admin them@example.com`. Their sessions end immediately. A disabled admin can only be brought back (`reset-admin`) when no other admin is active. |
| See all admins | `... python -m app.cli list-admins` |
| Change the admin key | Make a new one (`openssl rand -hex 32`). Put it in `.env.production` and `scp` it (B2), and in Vercel (B6). Redeploy the backend (step 15) and the admin (`npx vercel deploy --prod`). The admin stops working between the two redeploys, so do them back to back. |
| Update the admin app | `cd mailvender-admin && npx vercel deploy --prod` (or push, if it's connected to GitHub). |
| Who did what | **Audit log** in the admin. Every sign-in, view, block and suspension, with the admin's email, IP and reason. |
| Turn the admin API off | Remove `ADMIN_PROXY_SECRET` from the server's `.env` and redeploy. All `/v1/admin` addresses then answer 404. |

### Settings reference

Backend (`.env` on the server; defaults in `backend/app/config.py`):

| Setting | Default | What it does |
| --- | --- | --- |
| `ADMIN_PROXY_SECRET` | blank (admin off) | The admin key. ≥ 32 characters; must differ from `API_PROXY_SECRET`. |
| `INBOUND_MAIL_SECRET` | blank (inbox off) | Shared with the Email Worker. ≥ 32 characters. |
| `KNOWN_SENDING_IPS` | blank | Our sending IPs, comma-separated (`2.29.100.188`). |
| `ADMIN_SESSION_IDLE_MINUTES` | 30 | Sign out after this long without activity. |
| `ADMIN_SESSION_TTL_HOURS` | 8 | Sign out after this long in total. |
| `ADMIN_MAX_FAILED_LOGINS` / `ADMIN_LOCKOUT_MINUTES` | 10 / 30 | Lockout after wrong passwords or codes. |
| `ADMIN_MAX_ACCOUNTS` | 1 | How many admins can be active (not disabled) at once. `create-admin` refuses beyond it. |
| `RETENTION_INBOUND_MAIL_DAYS` / `RETENTION_DMARC_ROWS_DAYS` | 180 / 365 | How long inbox mail and DMARC rows are kept. |

Admin app (Vercel environment variables, server-only):

| Setting | Value |
| --- | --- |
| `API_INTERNAL_URL` | `https://api.mailvender.com` (local: `http://localhost:8000`) |
| `ADMIN_PROXY_SECRET` | Same as the backend's |
