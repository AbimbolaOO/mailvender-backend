# Production setup for mailvender.com

A step-by-step guide to deploying the Mailvender backend to production and
sending real email. For the reference behind each step, see
[RUNBOOK.md](RUNBOOK.md).

The production layout:

| Address                                 | Where it runs        | What it is                                               |
| --------------------------------------- | -------------------- | -------------------------------------------------------- |
| `https://www.mailvender.com`            | Vercel               | The Next.js app; it forwards `/api/*` to the API         |
| `mailvender.com`                        | Vercel               | Redirects to `www`                                       |
| `https://backoffice.mailvender.com`          | Vercel (2nd project) | The staff dashboard (step 19); talks to `/v1/admin/*` only |
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

Where the mailvender.com deployment stands (last checked 2026-10-22). Tick
items off as you go. Work that isn't about this deployment (features, ideas,
recommendations) is in [backlog.md](../../backlog.md).

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
- [ ] Redeploy the backend with the platform DKIM change (step 15).
- [ ] Step 12: publish the platform DKIM record for `mailvender.com` (`app.cli platform-dkim`).
- [ ] Step 13: a system email shows SPF, DKIM and DMARC PASS.
- [x] Step 14a: backup key created (in Passwords), log and database backup timers installed; first
      backups uploaded on 2026-10-18.
- [ ] Step 13a: Cloudflare Email Routing (`postmaster@`, `abuse@`, `dmarc@`), Google
      Postmaster Tools, Microsoft SNDS and JMRP. Doesn't need port 25.
- [ ] Step 19: the admin dashboard (secrets, Vercel project, first sign-in, Email Worker for the role inboxes).
- [ ] Step 14b: turn on bucket versioning and the two lifecycle rules in the AWS Console.
- [ ] IAM: allow `s3:ListBucket` on the `prod/` prefix (the admin's backup checks need it; RUNBOOK
      "Asset storage").
- [ ] Settings for monitoring: `KNOWN_SENDING_IPS`, `ALERT_WEBHOOK_URL` in
      `.env.production` (step 19).
- [ ] Step 20: outside uptime monitors (API, studio, admin, mail port) and the worker
      heartbeat (`HEARTBEAT_URL`).
- [ ] Step 14c: test-restore one database backup.

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
12. [DKIM for mailvender.com](#12-dkim-for-mailvendercom)
13. [Check a real email](#13-check-a-real-email)
14. [Backups to S3 (logs and database)](#14-backups-to-s3-logs-and-database)
15. [Redeploying after code changes](#15-redeploying-after-code-changes)
16. [Troubleshooting](#16-troubleshooting)
17. [Checking the server's status](#17-checking-the-servers-status)
18. [Reading the logs](#18-reading-the-logs)
19. [The admin dashboard](#19-the-admin-dashboard)
20. [Uptime monitoring from outside](#20-uptime-monitoring-from-outside)

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
- **The AWS S3 bucket** (images, dataset content, exports, admin mail
  attachments, backups). Required: the production stack stores files under
  `prod/` in the bucket. Bucket setup and the IAM permissions:
  [RUNBOOK.md](RUNBOOK.md), "Asset storage (S3)".

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

Step 13a later adds Cloudflare's MX records and changes the `@` SPF and
`_dmarc` values so mailvender.com can receive mail.

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

## 12. DKIM for mailvender.com

System emails (verification, welcome, password reset, invitations) go out
automatically from `no-reply@mailvender.com`. There's nothing to set up in the
app: `mailvender.com` is reserved, so no account (yours included) can add it
under **Settings → Sending domains**. Mailvender creates its DKIM key itself;
you only copy the DNS record into Cloudflare.

> Needs the backend version with `platform-dkim`. If the command below only
> prints the list of commands (without `platform-dkim` in it), redeploy first
> (step 15).

**1. Get the DKIM record.** SSH into the server and run:

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml exec api python -m app.cli platform-dkim
```

It creates the key the first time and prints the record:

```text
Platform DKIM for mailvender.com

pending  mvsys202610abc123   NOT FOUND in DNS
  TXT  mvsys202610abc123._domainkey.mailvender.com
       v=DKIM1; k=rsa; p=MIIBIjANBgkq…
```

**2. Add it in Cloudflare.** **DNS → Records → Add record**:

| Field   | Value                                                                       |
| ------- | --------------------------------------------------------------------------- |
| Type    | `TXT`                                                                       |
| Name    | `mvsys202610abc123._domainkey` (your selector; Cloudflare adds `.mailvender.com`) |
| Content | the whole `v=DKIM1; k=rsa; p=…` line, copied in one piece                   |

Copy the `p=` value completely: it's about 400 characters on one line. If your
terminal wraps it, select the full line; a missing character makes the record
`WRONG value in DNS`.

**3. Check it's active.** Within 5 minutes the worker finds the record and
activates the key. Run the command from step 1 again; you want:

```text
active   mvsys202610abc123   published
```

From then on, all mail from `@mailvender.com` is DKIM-signed.

**Rotating the key** (e.g. yearly): run `python -m app.cli platform-dkim rotate`
the same way, publish the new record it shows, and keep the old one. Once the
new record is found, the new key takes over; then delete the old record.

**Customer domains** are different: customers add them in the app under
**Settings → Sending domains**, publish the records it shows (DKIM, plus
`bounce` MX and TXT pointing at `feedback.mailvender.com` and
`_spf.mailvender.com`), click **Verify**, and approve their senders.

## 13. Check a real email

1. On www.mailvender.com, use **Forgot password** with your Gmail address (or
   sign up another address) so a fresh system email is sent.
2. In Gmail, open the message → **⋮ (More) → Show original**. You want:

   ```text
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

To test the customer path too (the studio's **Send test**), use another domain
you own: add it under **Settings → Sending domains** like a customer would,
verify it, approve a sender, and send a test. "Show original" then shows DKIM
`PASS` with that domain.

**Before sending to real customers:**

- **Warm up the IP.** Start with a few dozen emails a day to people who expect
  them, and increase gradually over 2–4 weeks. Sending a big list from a new IP
  on day one gets it blocked. All customers share this IP, so one bad sender
  hurts everyone: keep new accounts on low limits and watch bounce and
  complaint rates.
- Set up the postmaster addresses and register with Google and Microsoft
  (13a below).
- Once deliverability looks good, tighten DMARC from `p=none` to
  `p=quarantine`.

### 13a. Postmaster addresses, Google and Microsoft

These registrations don't whitelist you. Nothing does. What they give you is
your reputation at Gmail and Outlook, and copies of spam complaints, so you
notice a problem before mail starts bouncing. What actually keeps mail out of
spam: SPF, DKIM, DMARC and reverse DNS (done), port 25 open (step 2), a slow
warm-up and few complaints.

**None of this needs port 25 or the server.** Cloudflare receives the mail and
Google checks a DNS record, so do it now. The reputation pages only fill up
once you send real volume.

#### 1. Receive mail at mailvender.com (Cloudflare Email Routing, free)

Providers expect `postmaster@` and `abuse@` to work, and Microsoft sends its
confirmation link to `postmaster@`. mailvender.com has no MX record yet, so it
can't receive anything; Email Routing adds one and forwards to your Gmail.

1. Cloudflare → **mailvender.com → Email → Email Routing → Get started**.
2. Add these custom addresses, each with action **Send to an email** → your
   Gmail. Confirm the verification email Cloudflare sends to the Gmail once.

   | Address                    | For                               |
   | -------------------------- | --------------------------------- |
   | `support@mailvender.com`    | replies to emails written in the admin |
   | `postmaster@mailvender.com` | Microsoft confirmation, providers |
   | `abuse@mailvender.com`      | complaint reports (JMRP), abuse   |
   | `dmarc@mailvender.com`      | daily DMARC reports               |

3. Cloudflare offers to add its DNS records. **Accept the MX records.** For
   SPF, **don't add a second `@` TXT record** (a domain may have only one).
   Edit the existing `@` SPF record to:

   ```text
   v=spf1 include:_spf.mailvender.com include:_spf.mx.cloudflare.net -all
   ```

4. Change the `_dmarc` TXT record so receivers send you reports:

   ```text
   v=DMARC1; p=none; rua=mailto:dmarc@mailvender.com
   ```

5. Test: send an email from another account to `postmaster@mailvender.com`;
   it arrives in your Gmail.

This doesn't touch the server: system emails still leave from `2.29.100.188`,
and bounces for customer domains still go to `feedback.mailvender.com`.

Check:

```sh
dig +short MX mailvender.com          # → route1/2/3.mx.cloudflare.net
dig +short TXT mailvender.com         # → "v=spf1 include:_spf.mailvender.com include:_spf.mx.cloudflare.net -all"
dig +short TXT _dmarc.mailvender.com  # → "v=DMARC1; p=none; rua=mailto:dmarc@mailvender.com"
```

#### 2. Google Postmaster Tools (Gmail reputation)

1. Open <https://postmaster.google.com> and sign in with your Gmail.
2. Click **+** and add `mailvender.com`.
3. Google shows a TXT value like `google-site-verification=…`. In Cloudflare
   add a **TXT** record, name `@`, with that value. It's a separate record
   from SPF, which is fine. Click **Verify**.
4. Later it shows domain and IP reputation, spam rate and SPF/DKIM/DMARC
   results. It stays empty until you send a meaningful daily volume to Gmail.

Keep the spam rate under **0.1%**. Above 0.3% Gmail starts rejecting mail.

#### 3. Microsoft SNDS (Outlook and Hotmail reputation of the IP)

1. Open <https://sendersupport.olc.protection.outlook.com/snds/> and sign in
   with a Microsoft account (a free one made with your Gmail works).
2. **Request Access** → enter `2.29.100.188`.
3. Pick **`postmaster@mailvender.com`** from the offered addresses and click
   the link in the email (Cloudflare forwards it to your Gmail). If it only
   offers addresses at `mail.mailvender.com`, enable that subdomain under
   **Email Routing → Settings → Subdomains**, route its `postmaster@` to your
   Gmail as well, and request again.
4. SNDS then shows green, yellow or red for the IP, plus complaint rates.

#### 4. Microsoft JMRP (Outlook complaint copies)

On the same site, open **JMRP** and register `2.29.100.188` with
`abuse@mailvender.com`. Each time an Outlook user clicks **Junk** on one of
your emails, you get a copy, so you can spot a bad customer early.

#### Optional

- **Yahoo / AOL:** <https://senders.yahooinc.com> → **Complaint Feedback
  Loop**, register DKIM domain `mailvender.com`.
- **If Outlook blocks the IP anyway**, the bounce message has a link; request
  delisting at <https://sender.office.com>.

## 14. Backups to S3 (logs and database)

Two systemd timers on the server copy everything you'd need after an incident
into the S3 bucket, under `prod/`:

| What               | When          | Where in the bucket                                          | Format                      |
| ------------------ | ------------- | ------------------------------------------------------------ | --------------------------- |
| Container logs     | every hour    | `prod/logs/<service>/<YYYY>/<MM>/<DD>/<service>-<time>.log.gz` | gzip                        |
| Database (Supabase) | every 6 hours | `prod/backups/db/<YYYY>/<MM>/<DD>/mailvender-<time>.dump.age` | `pg_dump`, encrypted with age |

Both upload through the API image (`python -m app.cli upload-log` /
`upload-backup`, which only accept these exact paths), using the AWS
credentials already in `.env`.

How the database backup stays safe and cheap:

- **Encrypted before it leaves the server**, with [age](https://age-encryption.org)
  to a public key. The matching private key lives only in your password
  manager, so a stolen server, leaked AWS key or public bucket can't read the
  backups.
- **Never half-written:** the dump goes to a temporary file first and is only
  uploaded if `pg_dump` finished; the upload refuses anything not encrypted.
- **Small:** only Mailvender's `public` schema (not Supabase's internal
  schemas), in PostgreSQL's compressed custom format. `pg_dump` runs from the
  `postgres:17-alpine` image, so the server needs no PostgreSQL install.

### 14a. One-time setup

**1. Create the backup key, on your Mac:**

```sh
brew install age
mkdir -p ~/mailvender-secrets && chmod 700 ~/mailvender-secrets
age-keygen -o ~/mailvender-secrets/backup.key
```

It prints `Public key: age1…`. Copy that line's `age1…` value.

**Save `~/mailvender-secrets/backup.key` in your password manager now.** It's
the only way to read the backups: lose it and they're useless.

**2. Copy the code to the server** (the rsync in step 15) so it has
`infra/db-backup/` and `infra/status.sh`.

**3. Install the log backup, on the server.** It doesn't need the key:

```sh
cd /opt/mailvender/mailvender-backend
cp infra/log-backup/mailvender-log-backup.service infra/log-backup/mailvender-log-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mailvender-log-backup.timer
systemctl start mailvender-log-backup.service
journalctl -u mailvender-log-backup.service -n 20 --no-pager
```

The journal shows one `prod/logs/…` line per container. The first run uploads
everything Docker still has.

**4. Store the public key on the server.** On your Mac, show it:

```sh
grep "public key" ~/mailvender-secrets/backup.key     # → # public key: age1…
```

On the server, with your `age1…` value (it's public, safe to paste anywhere):

```sh
apt install -y age
mkdir -p /etc/mailvender
echo 'age1…' > /etc/mailvender/backup-recipient.txt
cat /etc/mailvender/backup-recipient.txt               # check it's there
```

**5. Install the database backup, on the server:**

```sh
cd /opt/mailvender/mailvender-backend
cp infra/db-backup/mailvender-db-backup.service infra/db-backup/mailvender-db-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mailvender-db-backup.timer
systemctl start mailvender-db-backup.service
journalctl -u mailvender-db-backup.service -n 20 --no-pager
```

The journal ends with a `prod/backups/db/…dump.age` line. The first run takes
a little longer: Docker downloads the `postgres:17-alpine` image once.

**6. Check:** `infra/status.sh` (step 17). The four backup lines should be ✔,
and `systemctl list-timers 'mailvender-*' --no-pager` shows when each runs next.

Common mistakes:

| Error                                                                 | Cause and fix                                                                                         |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `Unit mailvender-log-backup.service not found`                        | The unit files aren't in `/etc/systemd/system/` yet: run the `cp` and `systemctl daemon-reload` above. |
| `Invalid unit name "infra/log-backup/…" escaped as …`                 | `systemctl` takes the unit's **name**, not a path: `systemctl start mailvender-log-backup.service`.    |
| `-bash: /etc/mailvender/backup-recipient.txt: No such file or directory` | The folder doesn't exist: run `mkdir -p /etc/mailvender` first.                                        |
| `Job for mailvender-db-backup.service failed because the control process exited with error code` | The backup stopped with an error. Read why: `journalctl -u mailvender-db-backup.service -n 30 --no-pager` (or `mailvender-log-backup`), then fix it using the rows below and run `systemctl start …` again. |
| `missing /etc/mailvender/backup-recipient.txt` (in the journal)       | Step 4 wasn't done, or the file is empty.                                                             |
| `age isn't installed: apt install -y age` (in the journal)            | The `apt install -y age` line in step 4 was skipped: run it, then start the backup again.              |

### 14b. Protect the bucket (AWS Console, once)

In **S3 → `mailvender` → Properties → Bucket Versioning → Enable**. A deleted
or overwritten backup can then still be recovered.

Then **Management → Create lifecycle rule**, twice:

| Rule name   | Prefix              | Actions                                                                          |
| ----------- | ------------------- | -------------------------------------------------------------------------------- |
| `logs`      | `prod/logs/`        | Move current versions to Standard-IA after 30 days; expire them after 365 days  |
| `db-backups` | `prod/backups/db/` | Expire current versions after 35 days; permanently delete noncurrent versions after 30 days |

35 days of 6-hourly backups is about 140 files; at today's size (well under
1 MB each) that's a few cents a month.

Both folders are private: the bucket policy only makes `prod/images/*/public/*`
public. Logs contain email and IP addresses, and that's why they expire.

### 14c. Restoring the database

**Get and decrypt a backup, on your Mac** (needs the AWS CLI with read access
to the bucket, and `backup.key` from your password manager):

```sh
aws s3 ls s3://mailvender/prod/backups/db/ --recursive | tail -5      # newest last
aws s3 cp s3://mailvender/prod/backups/db/2026/10/18/mailvender-20261018T061500Z.dump.age .
age --decrypt -i ~/mailvender-secrets/backup.key -o mailvender.dump mailvender-20261018T061500Z.dump.age
```

**Test it in a throwaway database** (do this every few months, so you know
restoring works):

```sh
docker run -d --rm --name mv-restore -e POSTGRES_PASSWORD=x postgres:17-alpine
sleep 5
docker exec -i mv-restore sh -c 'cat > /tmp/db.dump
  pg_restore -l /tmp/db.dump | grep -v " SCHEMA - public " > /tmp/list
  pg_restore --no-owner --no-privileges --exit-on-error -U postgres -d postgres -L /tmp/list /tmp/db.dump' < mailvender.dump
docker exec mv-restore psql -U postgres -c 'select count(*) from users' -c 'select version_num from alembic_version'
docker stop mv-restore
```

(The `grep -v " SCHEMA - public "` skips re-creating the `public` schema, which
every database already has.)

**Restoring production** replaces the live data with the backup: everything
since that backup is lost. Only do it after a real data loss. On the server,
stop the API and worker first, restore, then start them:

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml stop api worker
# copy mailvender.dump to the server first (scp), then:
DBURL=$(sed -n 's/^DATABASE_URL=//p' .env | sed 's#^postgresql+psycopg://#postgresql://#')
docker run --rm -i -e DBURL postgres:17-alpine sh -c 'cat > /tmp/db.dump
  pg_restore -l /tmp/db.dump | grep -v " SCHEMA - public " > /tmp/list
  pg_restore --clean --if-exists --no-owner --no-privileges --single-transaction -d "$DBURL" -L /tmp/list /tmp/db.dump' < mailvender.dump
docker compose -f docker-compose.prod.yml start api worker
rm mailvender.dump
```

## 15. Redeploying after code changes

Three things deploy separately. When a change touches more than one, deploy in
this order, so a newer web app never talks to an older API:

1. **Backend** (server): API, worker, Postfix, OpenDKIM. Database migrations run
   by themselves when the API starts.
2. **Studio** (`mailvender/`, Vercel project for www.mailvender.com).
3. **Admin** (`mailvender-admin/`, Vercel project for backoffice.mailvender.com).

The Cloudflare Email Worker (`infra/email-worker/`) only needs redeploying when
its own code changes.

### Backend

**0. Keep a copy of what's running** (so you can roll back), from your **Mac**:

```sh
ssh root@2.29.100.188 'cd /opt/mailvender && tar czf backend-previous.tgz --exclude=.env --exclude=certs mailvender-backend'
```

**1. Copy the code to the server.** In a terminal on your **Mac** (not the SSH
session):

```sh
rsync -a --exclude .venv --exclude .env --exclude .env.production --exclude certs --exclude .git --exclude .vscode --exclude .DS_Store --exclude '__pycache__' \
  ~/Desktop/mailvender-repo/mailvender-backend/ root@2.29.100.188:/opt/mailvender/mailvender-backend/
```

The server's `.env` and `certs/` are never touched.

**2. Rebuild and restart.** On the **server**:

```sh
ssh root@2.29.100.188
cd /opt/mailvender/mailvender-backend
systemctl start mailvender-log-backup.service   # save the current logs to S3 first (step 14)
docker compose -f docker-compose.prod.yml up -d --build
```

Rebuilding replaces the `api` and `worker` containers, and **Docker deletes a
container's logs with it**. The first line uploads them to S3 before that
(skip it until step 14 is set up).

Database migrations run automatically when the API starts. Queued mail is kept
across restarts (the `postfix-queue` volume). Check which migration is live:
`docker compose -f docker-compose.prod.yml exec api alembic current` (the
newest is the highest number in `backend/alembic/versions/`).

**3. Check it's healthy.** Run the status check (step 17), or by hand:

```sh
docker compose -f docker-compose.prod.yml ps                # api/postfix healthy, worker/opendkim running
docker compose -f docker-compose.prod.yml logs --tail 30 api worker
curl -s http://localhost:8000/health/ready                  # {"status":"ok","database":"ok"}
```

From your Mac, `curl -s https://api.mailvender.com/health/ready` should give
the same. The admin's **System** page shows every background job; new ones
appear there after their first run.

**Changed a setting only?** Copy `.env.production` again (step 7), then on the
server run `docker compose -f docker-compose.prod.yml up -d` (no `--build`).
Every setting is listed in `.env.example` and RUNBOOK "Environment variables".

**Changed `infra/log-backup/` or `infra/db-backup/`?** Re-run the `cp` and
`systemctl daemon-reload` from step 14a.

**Rolling back.** If the new version misbehaves, put the copy from step 0 back
and rebuild, on the server:

```sh
cd /opt/mailvender && mv mailvender-backend mailvender-backend.bad && tar xzf backend-previous.tgz
cp mailvender-backend.bad/.env mailvender-backend/ && cp -a mailvender-backend.bad/certs mailvender-backend/
cd mailvender-backend && docker compose -f docker-compose.prod.yml up -d --build
```

Migrations so far only *add* tables and columns, so the previous code runs fine
on the newer database; don't downgrade migrations unless a release note says so.

### Studio (www.mailvender.com)

Push to the branch Vercel deploys (`mailvender/` is a git repository), or
redeploy in the Vercel dashboard. Nothing changes on the server. After
changing Vercel environment variables, redeploy too (step 10): the `/api`
rewrite is fixed at build time.

### Admin (backoffice.mailvender.com)

`mailvender-admin/` isn't in git yet, so deploy it from your Mac with the
Vercel CLI (ADMIN_SETUP B5):

```sh
cd ~/Desktop/mailvender-repo/mailvender-admin
npx vercel deploy --prod
```

### Rolling back a web app

Vercel keeps every deployment: **Vercel → the project → Deployments → a
previous one → ⋯ → Promote to Production** (or `npx vercel rollback`). It takes
seconds and doesn't touch the API.

### Email Worker (role inboxes)

Only after changing `infra/email-worker/`: `cd infra/email-worker && npx
wrangler deploy` (ADMIN_SETUP C1).

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
| `status=bounced … 550-5.7.26 … unauthenticated`             | SPF and DKIM both failed. Check the `_spf` and `@` TXT records (step 4) and that `platform-dkim` shows the key `active` (step 12).          |
| `status=deferred … 421-4.7.0 … rate limited` / `suspicious` | Gmail is slowing down a new IP. It retries automatically. Send less and warm up gradually.                                                   |
| Nothing at all                                              | The worker never handed it over. Check the `worker` logs, (for app sends) that the sender is approved, and that the recipient isn't suppressed.|

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

## 17. Checking the server's status

One command shows everything: containers, API and HTTPS, migrations, port 25,
DKIM, the mail queue, reverse DNS, TLS certificates, the last log and database
backups, errors in the last 24 hours, and disk/memory. From your **Mac**:

```sh
ssh root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/status.sh
```

(On the server itself: `/opt/mailvender/mailvender-backend/infra/status.sh`.)
It only reads; it changes nothing. Each line is `✔` or `✘`, and it ends with
either `All good.` or a list of what needs attention:

```text
Mail
  ✘ outbound port 25: BLOCKED (Hetzner unblock, PRODUCTION_SETUP step 2)
  ✔ platform DKIM: active mvsys202610z2jrkq published
…
1 thing(s) need attention:
  - outbound port 25: BLOCKED (Hetzner unblock, PRODUCTION_SETUP step 2)
```

To make it shorter to type, add an alias on your Mac:

```sh
echo "alias mv-status='ssh root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/status.sh'" >> ~/.zshrc
source ~/.zshrc
mv-status
```

Run it after every redeploy (step 15) and whenever something seems off. For
details on a ✘, see [Troubleshooting](#16-troubleshooting) and the service's
logs (`docker compose -f docker-compose.prod.yml logs --tail 100 <service>`).

## 18. Reading the logs

`infra/logs.sh` prints the containers' logs; name the ones you want. From your
**Mac**:

```sh
ssh -t root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/logs.sh api
```

(On the server itself: `/opt/mailvender/mailvender-backend/infra/logs.sh api`.
The `-t` lets Ctrl+C stop `--follow`.)

| Container  | What it logs                                         | Also accepts      |
| ---------- | ---------------------------------------------------- | ----------------- |
| `api`      | requests, sign-ups, logins, errors                   | `apis`            |
| `worker`   | sending, system emails, scheduled jobs, DKIM sync    | `workers`         |
| `postfix`  | the mail server: each delivery, bounce and deferral  | `mail`, `smtp`    |
| `opendkim` | DKIM signing and key reloads                         | `dkim`            |
| `all`      | all four (also the default when you name none)       |                   |

Options:

| Option            | Meaning                                                                    |
| ----------------- | -------------------------------------------------------------------------- |
| `-n 50`           | last 50 lines per container (default 100)                                  |
| `-s 2h`           | only the last 2 hours (`30m`, `2h`, `1d`, or `2026-10-18T09:00:00`)        |
| `-f`              | keep printing new lines live; Ctrl+C stops                                 |
| `-e`              | only errors and warnings (and bounced/deferred mail); last 24 h by default |
| `-g TEXT`         | only lines containing TEXT, e.g. an email address or a `request_id`        |
| `-h`              | help                                                                       |

Examples:

```sh
logs.sh api                       # last 100 lines of the API
logs.sh api worker -n 50          # last 50 lines of each
logs.sh api -f                    # watch the API live
logs.sh -e                        # errors and warnings from everything, last 24 h
logs.sh worker -e -s 1d           # worker errors in the last day
logs.sh postfix -g gmail.com      # deliveries to Gmail
logs.sh all -g someone@example.com -s 1d   # everything about one address
```

To type less, add an alias on your Mac:

```sh
echo "alias mv-logs='ssh -t root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/logs.sh'" >> ~/.zshrc
source ~/.zshrc
mv-logs api -f
```

Notes:

- Docker only has logs since each container was last created: a redeploy
  (step 15) starts `api` and `worker` fresh. Older logs are in S3 under
  `prod/logs/` (step 14).
- Postfix warnings like `non-SMTP command from …shadowserver.io` or
  `hostname … does not resolve` are internet scanners probing port 25. They're
  normal.

## 19. The admin dashboard

Mailvender Admin (`mailvender-admin/`, a second Vercel project) is the private
dashboard for staff: the support@/postmaster@/abuse@/dmarc@ inboxes, writing
email (with attachments) to customers or anyone, usage and spam rates,
customers who need help, blocking or suspending, and the platform's health. Its setup is in
its own guide, **[ADMIN_SETUP.md](ADMIN_SETUP.md)**. In short:

1. Make `ADMIN_PROXY_SECRET` and `INBOUND_MAIL_SECRET` (`openssl rand -hex 32`),
   add them, `KNOWN_SENDING_IPS=2.29.100.188` (also used for the hourly
   blocklist check) and, ideally, `ALERT_WEBHOOK_URL=<a Slack or Discord
   webhook>` (RUNBOOK "Monitoring and
   alerting") to `.env.production`, copy it to the server (step 7) and redeploy
   (step 15). Migrations run by themselves. Alert emails link to
   `https://backoffice.mailvender.com` (`ADMIN_APP_URL`, set in
   `docker-compose.prod.yml`).
2. On the server: `docker compose -f docker-compose.prod.yml exec api python -m app.cli create-admin you@example.com`.
3. Deploy `mailvender-admin/` to Vercel with `API_INTERNAL_URL` and
   `ADMIN_PROXY_SECRET`, add the domain `backoffice.mailvender.com` in Vercel,
   and a `CNAME` `backoffice` → Vercel in Cloudflare (DNS only).
4. Sign in: you'll change the temporary password and set up an authenticator
   app.
5. Deploy the Cloudflare Email Worker (`infra/email-worker/`) and point the
   four role addresses (`support@`, `postmaster@`, `abuse@`, `dmarc@`) at it, so
   their mail shows up in the admin's Inbox (13a must be done first).

---

## 20. Uptime monitoring from outside

Mailvender's own alerts (step 19) run *inside* the server. If the server, the
worker or the database is down, they can't run, so nothing tells you. A free
outside service fixes that: it checks Mailvender from the internet, and the
worker reports in to it regularly. Takes about 10 minutes.

**Which service:** [Better Stack](https://betterstack.com/uptime) (free plan:
website checks, a port check and heartbeats) is used below. UptimeRobot works
the same way for the website checks; check its current plan for heartbeats, or
use [Healthchecks.io](https://healthchecks.io) (free) for the heartbeat only.

### 20a. Website checks

Sign up, then **Monitors → Create monitor** four times:

| Monitor | URL / host | Alert when |
| --- | --- | --- |
| API | `https://api.mailvender.com/health/ready` | It doesn't return 200 (it returns 503 when the database is unreachable). |
| Studio | `https://www.mailvender.com/login` | It doesn't return 200. |
| Admin | `https://backoffice.mailvender.com/login` | It doesn't return 200. |
| Mail server | `mail.mailvender.com`, port **25** (type "TCP port") | The port doesn't answer – other mail servers can't hand us bounces or reports. |

Check every 3 minutes (the free minimum). Under **On-call / Notifications**, add
your email **and** the phone app (push), or Slack: email alone isn't enough if
your mail is what's broken.

The `/login` pages are used because they're public. The home pages redirect
signed-out visitors to them.

### 20b. Worker heartbeat

The website checks can't see the **worker** (it sends all email and runs the
jobs and the internal alerts). It checks in by itself instead:

1. In Better Stack: **Heartbeats → Create heartbeat**. Name `Mailvender worker`,
   expect a heartbeat **every 5 minutes**, grace period **5 minutes**. Copy
   its URL (`https://uptime.betterstack.com/api/v1/heartbeat/…`).
2. On your Mac, add it to `mailvender-backend/.env.production`:

   ```
   HEARTBEAT_URL=https://uptime.betterstack.com/api/v1/heartbeat/…
   ```

3. Copy the file to the server (step 7) and restart the worker:

   ```bash
   ssh -i ~/.ssh/hetzner-key root@2.29.100.188 \
     'cd /opt/mailvender/mailvender-backend && docker compose -f docker-compose.prod.yml up -d worker'
   ```

4. Within 5 minutes the heartbeat shows **Up**. To confirm in the logs:
   `infra/logs.sh worker -g heartbeat` shows `job.heartbeat.completed` with `result=sent`.

The worker only reports in while it can reach the database. If the server, the
worker or Supabase stops, the reports stop, and Better Stack alerts you after
the grace period. `HEARTBEAT_MINUTES` changes the interval (default 5); keep it
the same as in Better Stack.

### Test it once

- **Heartbeat:** on the server, `docker compose -f docker-compose.prod.yml stop worker`.
  Within about 10 minutes you get an alert. Then `... start worker`, and a
  "recovered" message follows.
- **Website:** pause and resume a monitor in Better Stack. Its "Send test
  alert" button checks that the notifications reach you.
