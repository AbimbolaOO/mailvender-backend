# Production setup for mailvender.com

A step-by-step guide to deploying the Mailvender backend to production and
sending real email. For the reference behind each step, see
[RUNBOOK.md](RUNBOOK.md).

The production layout:

| Address                                 | Where it runs        | What it is                                               |
| --------------------------------------- | -------------------- | -------------------------------------------------------- |
| `https://www.mailvender.com`            | Vercel               | The Next.js app; it forwards `/api/*` to the API         |
| `mailvender.com`                        | Vercel               | Redirects to `www`                                       |
| `https://api.mailvender.com`            | Hetzner server       | The API (Caddy → FastAPI on `localhost:8000`)            |
| `mail.mailvender.com`                   | Hetzner server       | The outbound mail server (Postfix + OpenDKIM)            |
| `feedback.mailvender.com`               | Hetzner server       | Where bounces come back                                  |
| `_spf.mailvender.com`                   | Cloudflare DNS       | The SPF list of IPs allowed to send for Mailvender       |

The production server is a **Hetzner Cloud** server with the static IPv4
address **`2.29.100.188`**. It runs the API, the worker, Postfix and OpenDKIM
(`docker-compose.prod.yml`). PostgreSQL and S3 are managed services, and the
frontend is on Vercel, so the server doesn't need Node.js.

> **Why not from your Mac?** Home internet providers block port 25 (the port mail
> servers use to talk to each other), and Gmail treats home IP addresses as spam
> sources. Real sending needs a server with a fixed IP.

---

## Deployment status

Where the mailvender.com deployment stands (last checked 2026-10-17). Tick
items off as you go.

### Done

- [x] DNS in Cloudflare; `www` and `@` serve the frontend from Vercel.
- [x] Reverse DNS: `2.29.100.188 → mail.mailvender.com` (step 2).
- [x] DNS records `api`, `mail`, `feedback`, `_spf`, `@` SPF and `_dmarc` (step 4).
- [x] Server is up; SSH key login with `~/.ssh/hetzner-key` works (step 3).
- [x] Docker, certbot and Caddy installed; `ufw` active (step 3).
- [x] Backend copied to `/opt/mailvender/mailvender-backend` (step 5).
- [x] TLS certificate in `certs/` and the three renewal hooks (step 6).

### To do

- [ ] Step 2: request the port 25 unblock (the server's check still says
      `port 25 BLOCKED`), turn off auto delete on the primary IP.
- [ ] Step 1: create the Supabase database (an EU region near Helsinki, e.g.
      Stockholm or Frankfurt) and copy its connection string.
- [ ] Step 3: turn off password logins (`PasswordAuthentication` is still `yes`).
- [ ] Step 7: copy `.env.production` to the server with `scp`.
- [ ] Step 8: start the stack; `/health/ready` returns `ok`.
- [ ] Step 9: Caddy serves `https://api.mailvender.com` (ports 80/443 currently refuse connections).
- [ ] Step 10: set `API_INTERNAL_URL` and `API_PROXY_SECRET` on Vercel and redeploy.
- [ ] Step 11: sign up and receive the verification email.
- [ ] Step 12: verify `mailvender.com` as a sending domain (DKIM, `bounce` MX/TXT).
- [ ] Step 13: test email shows SPF, DKIM and DMARC PASS.
- [ ] Step 14: enable the hourly log backup.

Steps 3 and 5–9 are best done in one sitting on the server.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Hetzner: server, port 25 and reverse DNS](#2-hetzner-server-port-25-and-reverse-dns)
3. [Prepare the server](#3-prepare-the-server)
4. [DNS records in Cloudflare](#4-dns-records-in-cloudflare)
5. [Copy the backend to the server](#5-copy-the-backend-to-the-server)
6. [TLS certificate for the mail server](#6-tls-certificate-for-the-mail-server)
7. [Copy the `.env` file to the server](#7-copy-the-env-file-to-the-server)
8. [Start the backend](#8-start-the-backend)
9. [HTTPS for the API](#9-https-for-the-api)
10. [Point Vercel at the API](#10-point-vercel-at-the-api)
11. [First email to your Gmail](#11-first-email-to-your-gmail)
12. [Verify mailvender.com as a sending domain](#12-verify-mailvendercom-as-a-sending-domain)
13. [Send a test and check it](#13-send-a-test-and-check-it)
14. [Back up logs to S3](#14-back-up-logs-to-s3)
15. [Updating after code changes](#15-updating-after-code-changes)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What you need

- **The domain `mailvender.com`**, with DNS in Cloudflare.
- **The frontend deployed on Vercel**, serving `mailvender.com` and
  `www.mailvender.com`.
- **A Hetzner Cloud server** (step 2): Ubuntu 24.04, at least 2 GB RAM
  (CX22 is enough), static IPv4 `2.29.100.188`.
- **A PostgreSQL database.** Hetzner has no managed PostgreSQL; use
  [Neon](https://neon.tech) or [Supabase](https://supabase.com) in a region
  close to the server (the server is in Helsinki, `hel1`, so e.g. Stockholm or
  Frankfurt). Copy its
  connection string.
- **The AWS S3 bucket** (images and dataset content). Required: the production
  stack stores files under `prod/` in the bucket. Bucket setup:
  [RUNBOOK.md](RUNBOOK.md).

## 2. Hetzner: server, port 25 and reverse DNS

All of this is in the [Hetzner Console](https://console.hetzner.com).

**Port 25.** Hetzner blocks outbound port 25 (and 465) on new Cloud accounts.
Request the unblock under **Limits → Request change** (or a support ticket):
"I run a transactional email service; please allow outbound SMTP on port 25 for
2.29.100.188." Hetzner usually approves only after the first invoice is paid,
so ask early. Until then, everything except delivery works; to deliver in the
meantime, use a relay ([Troubleshooting](#16-troubleshooting)).

**Keep the IP.** Mail reputation belongs to the IP. Under **Networking →
Primary IPs**, open `2.29.100.188` and turn **auto delete off**, so the IP
survives if the server is deleted or rebuilt.

**Check the IP isn't blocklisted** at
[MXToolbox](https://mxtoolbox.com/blacklists.aspx). `2.29.100.188` was clean
(0 of 60 lists, including Spamhaus and UCEPROTECT) when the server was
created. Recheck it if delivery suddenly gets worse.

**Reverse DNS.** Reverse DNS (PTR) maps the IP back to a name. Gmail checks
that `2.29.100.188 → mail.mailvender.com` and `mail.mailvender.com →
2.29.100.188` match, and that the name matches the Postfix hostname. Mail from
IPs without it usually goes to spam or is rejected. Only Hetzner can set it,
because they own the IP: **Primary IPs → `2.29.100.188` → ⋯ → Edit Reverse
DNS** → `mail.mailvender.com`.

```sh
dig +short -x 2.29.100.188              # → mail.mailvender.com.
```

The reverse DNS names the **server**, not the sender. Customers still send
from their own domains; their DKIM, Return-Path and DMARC records (step 12)
prove that.

**Optional: Hetzner Cloud Firewall.** Allow inbound TCP 22, 25, 80 and 443.
It works alongside `ufw` (step 3).

## 3. Prepare the server

**Log in with an SSH key.** If you didn't add an SSH key when creating the
server, Hetzner emailed the root password (or reset it under **Servers → the
server → Rescue → Reset root password**). All of the following runs on your
**Mac**.

1. Create a key named `hetzner-key` in `~/.ssh`:

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/hetzner-key
   ```

   Enter a passphrase when asked (nothing shows while you type) and repeat it.
   It's done when it prints `Your identification has been saved…` and a box of
   characters. Two files appear: `~/.ssh/hetzner-key` (the private key, never
   share it) and `~/.ssh/hetzner-key.pub` (the public key, which goes on the
   server).

   > Without `-f`, `ssh-keygen` asks where to save the key. Typing just a name
   > such as `hetzner-key` saves it in the **current folder** (e.g. the project
   > folder), not in `~/.ssh`, and `ssh-copy-id` then reports
   > `ERROR: No identities found`. Move it into place:
   > `mv hetzner-key hetzner-key.pub ~/.ssh/ && chmod 600 ~/.ssh/hetzner-key`.
   > A private key must never stay in the project folder.

2. Tell SSH to use this key for the server, and to keep the passphrase in the
   macOS Keychain:

   ```sh
   cat >> ~/.ssh/config <<'EOF'
   Host 2.29.100.188
     User root
     IdentityFile ~/.ssh/hetzner-key
     IdentitiesOnly yes
     UseKeychain yes
     AddKeysToAgent yes
   EOF
   chmod 600 ~/.ssh/config
   ```

3. Install the public key on the server. This asks for the server's **root
   password** one last time:

   ```sh
   ssh-copy-id -i ~/.ssh/hetzner-key.pub root@2.29.100.188
   ```

4. Save the passphrase in the Keychain (asks for it once), then log in:

   ```sh
   ssh-add --apple-use-keychain ~/.ssh/hetzner-key
   ssh root@2.29.100.188
   ```

   From now on it logs in straight away. If it shows
   `Enter passphrase for key '…/hetzner-key'`, that's the **key passphrase**
   from step 1, not the server password: the key login works, and step 4's
   `ssh-add` stops the prompt. If it asks for `root@2.29.100.188's password`
   instead, the key isn't installed: repeat step 3.

On the server, turn off password logins (test a new login in a second terminal
before closing the first):

```sh
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

**Install Docker, certbot and Caddy:**

```sh
curl -fsSL https://get.docker.com | sh
apt install -y certbot caddy dnsutils
systemctl stop caddy     # it needs port 80 free for step 6; started again in step 9

ufw allow 22,25,80,443/tcp
ufw --force enable
```

Check that the server is allowed to send on port 25:

```sh
timeout 5 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' && echo "port 25 OPEN" || echo "port 25 BLOCKED"
```

**BLOCKED** means Hetzner hasn't unblocked port 25 yet (step 2).

## 4. DNS records in Cloudflare

In Cloudflare → **mailvender.com → DNS → Records**.

**Leave the Vercel records alone:** the `@` A record and the `www` CNAME serve
the frontend.

**Delete the records you added while testing locally:** anything pointing to
`feedback.mailvender.localhost` or `_spf.mailvender.localhost`, and any old
`…._domainkey.mailvender.com` DKIM record. The production database generates
new values in step 12.

Then add these. Set **Proxy status to "DNS only" (grey cloud)** on the A
records: mail can't pass through Cloudflare's proxy, and Caddy needs a direct
connection to get its HTTPS certificate.

| Type | Name       | Content                                   | Proxy    |
| ---- | ---------- | ----------------------------------------- | -------- |
| A    | `api`      | `2.29.100.188`                            | DNS only |
| A    | `mail`     | `2.29.100.188`                            | DNS only |
| A    | `feedback` | `2.29.100.188`                            | DNS only |
| TXT  | `_spf`     | `v=spf1 ip4:2.29.100.188 -all`            | –        |
| TXT  | `@`        | `v=spf1 include:_spf.mailvender.com -all` | –        |
| TXT  | `_dmarc`   | `v=DMARC1; p=none`                        | –        |

Type the names and values exactly: no spaces (`_dmarc`, not `_ dmarc`) and a
lowercase `v=`. Receivers ignore a DMARC record starting with `V=`. Browser
spell-checkers such as Grammarly can change these as you type.

What the SPF records do:

- **`_spf.mailvender.com`** is the only place the server's IP is written.
  Every other SPF record, including each customer's `bounce.<their domain>`,
  points to it with `include:_spf.mailvender.com`. Moving server or adding an
  IP means editing this one record.
- **`@` (mailvender.com)** is checked for Mailvender's own system emails
  (verification, welcome, password reset, invitations). They are sent with
  `no-reply@mailvender.com` as the Return-Path, so this record must exist.

> **Already receive email at mailvender.com** (Google Workspace, Zoho, …)?
> A domain may have only **one** SPF record. Don't add a second `@` TXT
> record. Add `include:_spf.mailvender.com` to the existing one instead, e.g.
> `v=spf1 include:_spf.google.com include:_spf.mailvender.com -all`. Leave
> your existing MX records alone.

Check from your Mac (it can take a few minutes):

```sh
dig +short api.mailvender.com           # → 2.29.100.188
dig +short mail.mailvender.com          # → 2.29.100.188
dig +short TXT _spf.mailvender.com      # → "v=spf1 ip4:2.29.100.188 -all"
dig +short TXT mailvender.com           # → "v=spf1 include:_spf.mailvender.com -all"
dig +short TXT _dmarc.mailvender.com    # → "v=DMARC1; p=none"
```

## 5. Copy the backend to the server

Only `mailvender-backend/` goes to the server. Run this in a terminal on your
**Mac**, not in the SSH session: if the prompt shows `root@ubuntu-…`, you're
on the server, where `~/Desktop/mailvender-repo` doesn't exist and rsync fails
with `change_dir "/root/Desktop/…" failed: No such file or directory`. Open a
new local tab (Cmd+T) instead.

```sh
ssh root@2.29.100.188 'mkdir -p /opt/mailvender'    # first time only: rsync won't create the parent folder
rsync -a --exclude .venv --exclude .env --exclude .env.production --exclude certs --exclude .git --exclude .vscode --exclude .DS_Store --exclude '__pycache__' \
  ~/Desktop/mailvender-repo/mailvender-backend/ root@2.29.100.188:/opt/mailvender/mailvender-backend/
```

Keep the destination (`root@2.29.100.188:/opt/…`) on the end; the command
copies nothing without it. Check on the server:

```sh
ls /opt/mailvender/mailvender-backend    # backend/ infra/ docs/ docker-compose.prod.yml …
```

`.env` and `certs` are excluded so that later updates (step 15) never
overwrite the server's production secrets and certificate with your local
ones. `.env` is created (or copied once) in step 7. `.env.production` (your
Mac's copy of the production settings, step 7) must never be synced this way.
`.git` and `.vscode` aren't needed on the server, so they're left out too.

## 6. TLS certificate for the mail server

On the server, get a free Let's Encrypt certificate for `mail.mailvender.com`
and copy it to where Postfix reads it:

```sh
certbot certonly --standalone -d mail.mailvender.com --agree-tos -m you@gmail.com -n

mkdir -p /opt/mailvender/mailvender-backend/certs
cp -L /etc/letsencrypt/live/mail.mailvender.com/fullchain.pem \
      /etc/letsencrypt/live/mail.mailvender.com/privkey.pem \
      /opt/mailvender/mailvender-backend/certs/
```

Set up automatic renewal. Certificates last 90 days, and certbot renews them
by itself. These hooks free port 80 from Caddy during renewal, then copy the
new certificate to Postfix:

```sh
cat > /etc/letsencrypt/renewal-hooks/pre/caddy.sh <<'EOF'
#!/bin/sh
systemctl stop caddy
EOF
cat > /etc/letsencrypt/renewal-hooks/post/caddy.sh <<'EOF'
#!/bin/sh
systemctl start caddy
EOF
cat > /etc/letsencrypt/renewal-hooks/deploy/mailvender-postfix.sh <<'EOF'
#!/bin/sh
cp -L /etc/letsencrypt/live/mail.mailvender.com/fullchain.pem \
      /etc/letsencrypt/live/mail.mailvender.com/privkey.pem \
      /opt/mailvender/mailvender-backend/certs/
docker compose -f /opt/mailvender/mailvender-backend/docker-compose.prod.yml restart postfix
EOF
chmod +x /etc/letsencrypt/renewal-hooks/*/*.sh
```

## 7. Copy the `.env` file to the server

The production settings are in `mailvender-backend/.env.production` on your
Mac. Copy it to the server as `.env`. Run this on your **Mac**:

```sh
scp ~/Desktop/mailvender-repo/mailvender-backend/.env.production root@2.29.100.188:/opt/mailvender/mailvender-backend/.env
ssh root@2.29.100.188 'chmod 600 /opt/mailvender/mailvender-backend/.env'
```

`scp` copies the file over SSH (encrypted). `chmod 600` makes it readable only
by root.

To change a setting later, edit `.env.production` on your Mac and run the two
commands again, then restart on the server:
`docker compose -f docker-compose.prod.yml up -d`.

## 8. Start the backend

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

After a minute, `api` and `postfix` should be **healthy**, and `worker` and
`opendkim` **running**. If a service keeps restarting, look at its logs:

```sh
docker compose -f docker-compose.prod.yml logs api postfix
```

The production checks print exactly which setting is wrong, e.g.
`postfix: TLS certificate not found` or
`invalid production configuration: SECRET_KEY must be at least 32 random characters`.

Check the API answers:

```sh
curl -s http://localhost:8000/health/ready     # → {"status":"ok","database":"ok"}
```

## 9. HTTPS for the API

Caddy gets and renews the certificate for `api.mailvender.com` automatically
and forwards requests to the API, which only listens on `localhost:8000`.
`header_up` sets `X-Forwarded-For` to the address that actually connected, so a
caller can't fake its IP; requests from Vercel name the visitor with the
`API_PROXY_SECRET` headers instead (step 10).

```sh
cat > /etc/caddy/Caddyfile <<'EOF'
api.mailvender.com {
	reverse_proxy localhost:8000 {
		header_up X-Forwarded-For {remote_host}
	}
}
EOF
systemctl enable --now caddy
systemctl reload caddy
```

From your Mac:

```sh
curl -s https://api.mailvender.com/health/ready     # → {"status":"ok","database":"ok"}
```

## 10. Point Vercel at the API

The frontend forwards `/api/*` to `API_INTERNAL_URL` (`next.config.ts`).

1. In Vercel → the project → **Settings → Environment Variables**, add for
   **Production**:
   - `API_INTERNAL_URL` = `https://api.mailvender.com`
   - `API_PROXY_SECRET` = the same value as in the server's `.env` (mark it
     **Sensitive**)
2. **Redeploy.** The rewrite is fixed at build time, so changing the variables
   alone isn't enough.

`proxy.ts` adds the secret and the visitor's IP to every `/api/*` request
(and removes any a browser sent itself). To check it works, sign in on
www.mailvender.com, then in Supabase → **SQL Editor** run:

```sql
select ip_address, created_at from sessions order by created_at desc limit 1;
```

`ip_address` should be your own IP (search "what is my IP"). If it's a Vercel
address, the two `API_PROXY_SECRET` values don't match or Vercel wasn't
redeployed.

Open **https://www.mailvender.com**. You should see the login page.

## 11. First email to your Gmail

1. On https://www.mailvender.com, **sign up with your Gmail address**.
2. Mailvender sends you a verification email from `no-reply@mailvender.com`.
   **This is your first real email.** Look in your Gmail inbox, and if it isn't
   there, in **Spam**. New servers have no reputation yet, and the email isn't
   DKIM-signed until step 12, so spam is normal at first. Click "Not spam".
3. Click the link in the email to verify your account and sign in.

Didn't arrive? See [Troubleshooting](#16-troubleshooting).

## 12. Verify mailvender.com as a sending domain

This sets up DKIM signing, which Gmail needs to trust your mail. **Do it
before inviting real users:** until mailvender.com is verified, the worker has
no DKIM key for it, so system emails go out unsigned.

1. In the app, open **Settings → Sending domains** and add `mailvender.com`.
2. The app lists DNS records to add. Add each one in Cloudflare exactly as
   shown, with **DNS only** where Cloudflare offers the proxy option:
   - **TXT** `<selector>._domainkey` → `v=DKIM1; k=rsa; p=…` (DKIM)
   - **MX** `bounce` → `feedback.mailvender.com`, priority 10 (bounces come back here)
   - **TXT** `bounce` → `v=spf1 include:_spf.mailvender.com -all`
   - **TXT** `_dmarc`: you already added this in step 4; the app checks it.
3. Wait a few minutes and click **Verify**. All records should turn green.
4. In **Settings → Senders**, approve the address you'll send from, e.g.
   `hello@mailvender.com`.

From now on, all mail from `@mailvender.com` is DKIM-signed, including the
verification and password-reset emails.

Customers go through the same steps for their own domains: their records point
at `feedback.mailvender.com` and `_spf.mailvender.com`, and their mail is
signed with their own DKIM key.

## 13. Send a test and check it

1. Open an email in the studio, use **Send test**, and send it to your Gmail.
2. In Gmail, open the message → **⋮ (More) → Show original**. You want:

   ```
   SPF:   PASS with IP 2.29.100.188
   DKIM:  'PASS' with domain mailvender.com
   DMARC: 'PASS'
   ```

3. On the server, the Postfix log shows the delivery:

   ```sh
   cd /opt/mailvender/mailvender-backend
   docker compose -f docker-compose.prod.yml logs postfix | grep status=
   # … to=<you@gmail.com>, relay=gmail-smtp-in.l.google.com[…]:25, … status=sent (250 2.0.0 OK …)
   ```

**Before sending to real customers:**

- **Warm up the IP.** Start with a few dozen emails a day to people who expect
  them, and increase gradually over 2–4 weeks. Sending a big list from a new IP
  on day one gets it blocked. All customers share this IP, so one bad sender
  hurts everyone: keep new accounts on low limits and watch bounce and
  complaint rates.
- Register `mailvender.com` in [Google Postmaster Tools](https://postmaster.google.com/)
  to see your Gmail reputation, and `2.29.100.188` in
  [Microsoft SNDS](https://sendersupport.olc.protection.outlook.com/snds/).
- Once deliverability looks good, tighten DMARC from `p=none` to
  `p=quarantine`.

## 14. Back up logs to S3

Docker keeps only the last 250 MB of logs per service. A systemd timer copies
them to the bucket every hour, one gzip file per service:
`s3://mailvender/prod/logs/<service>/<YYYY>/<MM>/<DD>/<service>-<time>.log.gz`.
The upload runs in the API image with the AWS credentials from `.env`; the
API's IAM access to `prod/*` already covers `prod/logs/`.

```sh
cd /opt/mailvender/mailvender-backend
cp infra/log-backup/mailvender-log-backup.service infra/log-backup/mailvender-log-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mailvender-log-backup.timer

systemctl start mailvender-log-backup.service      # first run now: uploads everything Docker still has
journalctl -u mailvender-log-backup.service -n 20  # one "prod/logs/…" line per uploaded file
systemctl list-timers mailvender-log-backup.timer   # next run
```

The logs contain email addresses and IP addresses, so they stay private (the
bucket policy only makes `prod/images/*/public/*` public). Expire them: in
the AWS Console → S3 → `mailvender` → **Management → Create lifecycle rule**:
prefix `prod/logs/`, move to Standard-IA after 30 days, expire after 365 days.

To read a backup (from any machine with the AWS CLI and access to the bucket):

```sh
aws s3 cp s3://mailvender/prod/logs/api/2026/10/17/api-20261017T140000Z.log.gz - | gunzip | less
```

## 15. Updating after code changes

**Backend:** from your Mac, copy the changes (the rsync in step 5), then on
the server:

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml up -d --build    # migrations run automatically
```

Queued mail is kept across restarts (the `postfix-queue` volume).

**Frontend:** deploy on Vercel as usual. Nothing changes on the server.

`infra/log-backup/` comes along with the rsync. Re-run the `cp` and
`systemctl daemon-reload` from step 14 only if the `.service` or `.timer` file
changed.

## 16. Troubleshooting

All `docker compose` commands below run in `/opt/mailvender/mailvender-backend`
with `-f docker-compose.prod.yml`.

**No email arrived.** Follow the message through the system:

```sh
docker compose -f docker-compose.prod.yml logs --tail 100 worker | grep -i "message\|outbox"
docker compose -f docker-compose.prod.yml logs --tail 100 postfix | grep status=
docker compose -f docker-compose.prod.yml exec postfix mailq     # mail waiting in the queue
```

| You see in the Postfix log                                  | Meaning and fix                                                                                                                              |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `status=sent (250 …)`                                       | Gmail accepted it. Look in Spam, and under All Mail.                                                                                         |
| `connect to gmail-smtp-in…:25: Connection timed out`        | Hetzner still blocks outbound port 25 (step 2). Wait for the unblock, or use a relay (below).                                                |
| `status=bounced … 550-5.7.1 … PTR record`                   | Reverse DNS is missing or wrong. Redo the reverse DNS part of step 2.                                                                        |
| `status=bounced … 550-5.7.26 … unauthenticated`             | SPF and DKIM both failed. Check the `_spf` and `@` TXT records (step 4) and that the domain is verified (step 12).                           |
| `status=deferred … 421-4.7.0 … rate limited` / `suspicious` | Gmail is slowing down a new IP. It retries automatically. Send less and warm up gradually.                                                   |
| Nothing at all                                              | The worker never handed it over. Check the `worker` logs, that the sender is approved (step 12), and that the recipient isn't suppressed.   |

**A service won't start.** Run `docker compose -f docker-compose.prod.yml logs <service>`.
The production checks name the setting to fix.

**The site loads but sign-in or every API call fails.** Vercel can't reach the
API. Check `curl https://api.mailvender.com/health/ready` from your Mac, that
`API_INTERNAL_URL` is set on Vercel, and that you redeployed after setting it
(step 10).

**Login works but you're logged out immediately.** You opened the site over
`http://`. Use `https://www.mailvender.com`; the session cookie is HTTPS-only.

**Sending through a relay (while port 25 is blocked).** Postfix can hand all
mail to an email provider's SMTP server instead of delivering it itself (e.g.
Amazon SES, Brevo, Mailgun). Add to `.env`:

```ini
POSTFIX_RELAYHOST=[email-smtp.us-east-1.amazonaws.com]:587
POSTFIX_RELAYHOST_USERNAME=<SMTP username from the provider>
POSTFIX_RELAYHOST_PASSWORD=<SMTP password from the provider>
```

then `docker compose -f docker-compose.prod.yml up -d postfix`. Also add the
provider's own DNS records (they give you SPF/DKIM values) and verify
`mailvender.com` with them. The catch: bounces and spam complaints then go to
the provider, not to Mailvender, so they aren't suppressed automatically yet.
Remove the three lines once Hetzner opens port 25.
