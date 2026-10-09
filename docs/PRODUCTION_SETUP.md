# Production setup for mailvender.com

A step-by-step guide to running Mailvender in production at
**https://www.mailvender.com** and sending real email (for example to your
Gmail). For the reference behind each step, see [RUNBOOK.md](RUNBOOK.md).

At the end you will have:

| Address                       | What it is                                                     |
| ----------------------------- | -------------------------------------------------------------- |
| `https://www.mailvender.com`  | The Mailvender app (Next.js), with the API behind it at `/api` |
| `mailvender.com`              | Redirects to `www`                                             |
| `mail.mailvender.com`         | The outbound mail server (Postfix + OpenDKIM)                  |
| `feedback.mailvender.com`     | Where bounces come back (same server)                          |
| `_spf.mailvender.com`         | The SPF list of IPs allowed to send for Mailvender             |

Everything runs on **one server**. Below, replace `YOUR_SERVER_IP` with that
server's public IP address.

> **Why not from your Mac?** Home internet providers block port 25 (the port mail
> servers use to talk to each other), and Gmail treats home IP addresses as spam
> sources. Real sending needs a server with a fixed IP.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Prepare the server](#2-prepare-the-server)
3. [DNS records in Cloudflare](#3-dns-records-in-cloudflare)
4. [Reverse DNS](#4-reverse-dns)
5. [Copy the project to the server](#5-copy-the-project-to-the-server)
6. [TLS certificate for the mail server](#6-tls-certificate-for-the-mail-server)
7. [Create the `.env` file](#7-create-the-env-file)
8. [Start the backend](#8-start-the-backend)
9. [Start the app with HTTPS](#9-start-the-app-with-https)
10. [First email to your Gmail](#10-first-email-to-your-gmail)
11. [Verify mailvender.com as a sending domain](#11-verify-mailvendercom-as-a-sending-domain)
12. [Send a test and check it](#12-send-a-test-and-check-it)
13. [Updating after code changes](#13-updating-after-code-changes)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What you need

- **The domain `mailvender.com`**, with DNS in Cloudflare.
- **A Linux server (VPS)** with Ubuntu 24.04, at least 2 GB RAM, a **static
  IPv4 address**, and **port 25 allowed**. Providers such as Hetzner, OVH,
  Vultr and Linode work, but most block port 25 on new accounts until you open
  a support ticket asking them to unblock it ("I run a transactional email
  service; please allow outbound SMTP on port 25"). Do this first, because it
  can take a few days. AWS, Google Cloud, Azure and DigitalOcean rarely agree.
- **A PostgreSQL database.** A free [Neon](https://neon.tech) or
  [Supabase](https://supabase.com) database is fine to start. Copy its
  connection string.
- **Optional: an S3 bucket** (AWS S3, Cloudflare R2, …) for image uploads. You
  can skip it for now; everything except image uploads works without it.

## 2. Prepare the server

Log in and install Docker, Node.js 22, certbot and Caddy:

```sh
ssh root@YOUR_SERVER_IP

curl -fsSL https://get.docker.com | sh
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs certbot caddy dnsutils
systemctl stop caddy     # it needs port 80 free for step 6; started again in step 9

ufw allow 22,25,80,443/tcp
ufw --force enable
```

Check that the server is allowed to send on port 25:

```sh
timeout 5 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' && echo "port 25 OPEN" || echo "port 25 BLOCKED"
```

If it says **BLOCKED**, your provider hasn't unblocked port 25 yet. Wait for
your support ticket, or see "Sending through a relay" in
[Troubleshooting](#14-troubleshooting).

## 3. DNS records in Cloudflare

In Cloudflare → **mailvender.com → DNS → Records**:

**First, delete the records you added while testing locally.** Anything
pointing to `feedback.mailvender.localhost` or `_spf.mailvender.localhost`,
and the old `…._domainkey.mailvender.com` DKIM record. The production
database generates new values in step 11.

Then add these records. Set **Proxy status to "DNS only" (grey cloud)** on
every one. Mail can't pass through Cloudflare's proxy, and Caddy needs a
direct connection to get its HTTPS certificate.

| Type | Name       | Content                                | Proxy    |
| ---- | ---------- | -------------------------------------- | -------- |
| A    | `www`      | `YOUR_SERVER_IP`                       | DNS only |
| A    | `@`        | `YOUR_SERVER_IP`                       | DNS only |
| A    | `mail`     | `YOUR_SERVER_IP`                       | DNS only |
| A    | `feedback` | `YOUR_SERVER_IP`                       | DNS only |
| TXT  | `_spf`     | `v=spf1 ip4:YOUR_SERVER_IP -all`       | –        |
| TXT  | `@`        | `v=spf1 include:_spf.mailvender.com -all` | –     |
| TXT  | `_dmarc`   | `v=DMARC1; p=none`                     | –        |

> **Already receive email at mailvender.com** (Google Workspace, Zoho, …)?
> A domain may have only **one** SPF record. Don't add a second `@` TXT
> record. Add `include:_spf.mailvender.com` to the existing one instead, e.g.
> `v=spf1 include:_spf.google.com include:_spf.mailvender.com -all`. Leave
> your existing MX records alone.

> **Is www.mailvender.com already hosted elsewhere** (Vercel, Netlify, …)?
> Then skip the `www` and `@` records and step 9. Run Next.js where it already
> is, and give the API its own name instead: an A record `api` →
> `YOUR_SERVER_IP`, `api.mailvender.com { reverse_proxy localhost:8000 }` in
> the Caddyfile, and `API_INTERNAL_URL=https://api.mailvender.com` on the
> frontend host.

Check from your Mac (it can take a few minutes):

```sh
dig +short mail.mailvender.com          # → YOUR_SERVER_IP
dig +short TXT _spf.mailvender.com      # → "v=spf1 ip4:YOUR_SERVER_IP -all"
```

## 4. Reverse DNS

Reverse DNS (PTR) maps the IP back to a name. Gmail checks that
`YOUR_SERVER_IP → mail.mailvender.com` and `mail.mailvender.com →
YOUR_SERVER_IP` match. Mail from IPs without it usually goes to spam or is
rejected.

You set it **in your VPS provider's control panel, not in Cloudflare**.
Look for "Reverse DNS" or "PTR" on the server's IP, and set it to:

```
mail.mailvender.com
```

Check it:

```sh
dig +short -x YOUR_SERVER_IP            # → mail.mailvender.com.
```

## 5. Copy the project to the server

From your Mac:

```sh
rsync -a --exclude node_modules --exclude .venv --exclude .next --exclude .DS_Store \
  ~/Desktop/mailvender-repo/ root@YOUR_SERVER_IP:/opt/mailvender/
```

(Or `git clone` your repositories into `/opt/mailvender/mailvender` and
`/opt/mailvender/mailvender-backend`.)

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

## 7. Create the `.env` file

The production stack already uses mailvender.com for every domain setting
(see the top of `docker-compose.prod.yml`). You only fill in the database and
the secrets.

```sh
cd /opt/mailvender/mailvender-backend
cp .env.example .env

# Generate the three secrets. Copy each output.
openssl rand -hex 32                       # → SECRET_KEY
openssl rand -base64 32 | tr '+/' '-_'     # → DKIM_ENCRYPTION_KEY
openssl rand -hex 24                       # → INTERNAL_API_TOKEN

nano .env
```

Fill in these lines and leave the rest empty:

```ini
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require
SECRET_KEY=<from above>
DKIM_ENCRYPTION_KEY=<from above>
INTERNAL_API_TOKEN=<from above>
```

- **`DATABASE_URL`:** take Neon's/Supabase's connection string and change the
  start from `postgres://` or `postgresql://` to `postgresql+psycopg://`.
- **Keep these secrets safe** (e.g. in a password manager). If you lose
  `DKIM_ENCRYPTION_KEY`, the stored DKIM keys can't be decrypted. If you change
  `SECRET_KEY`, everyone is signed out and old unsubscribe links stop working.
- **Optional, for image uploads:** fill in `AWS_REGION`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_S3_BUCKET`, `AWS_S3_PUBLIC_BASE_URL` (and
  `AWS_S3_ENDPOINT_URL` for R2 or another S3-compatible service).

```sh
chmod 600 .env
```

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

## 9. Start the app with HTTPS

**Build the frontend.** `API_INTERNAL_URL` is needed at build time as well as
at run time:

```sh
cd /opt/mailvender/mailvender
npm ci
API_INTERNAL_URL=http://localhost:8000 npm run build
```

**Run it as a service** so it restarts after crashes and reboots:

```sh
cat > /etc/systemd/system/mailvender-web.service <<'EOF'
[Unit]
Description=Mailvender web app (Next.js)
After=network.target docker.service

[Service]
WorkingDirectory=/opt/mailvender/mailvender
Environment=NODE_ENV=production
Environment=API_INTERNAL_URL=http://localhost:8000
ExecStart=/usr/bin/npm start
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now mailvender-web
```

**Put HTTPS in front with Caddy.** It gets and renews certificates for
`www.mailvender.com` automatically:

```sh
cat > /etc/caddy/Caddyfile <<'EOF'
www.mailvender.com {
	reverse_proxy localhost:3000
}

mailvender.com {
	redir https://www.mailvender.com{uri} permanent
}
EOF
systemctl enable --now caddy
systemctl reload caddy
```

Open **https://www.mailvender.com**. You should see the login page.

## 10. First email to your Gmail

1. On https://www.mailvender.com, **sign up with your Gmail address**.
2. Mailvender sends you a verification email from `no-reply@mailvender.com`.
   **This is your first real email.** Look in your Gmail inbox, and if it isn't
   there, in **Spam**. New servers have no reputation yet, so spam is normal at
   first. Click "Not spam".
3. Click the link in the email to verify your account and sign in.

Didn't arrive? See [Troubleshooting](#14-troubleshooting).

## 11. Verify mailvender.com as a sending domain

This sets up DKIM signing, which Gmail needs to trust your mail.

1. In the app, open **Settings → Sending domains** and add `mailvender.com`.
2. The app lists DNS records to add. Add each one in Cloudflare exactly as
   shown, with **DNS only** where Cloudflare offers the proxy option:
   - **TXT** `<selector>._domainkey` → `v=DKIM1; k=rsa; p=…` (DKIM)
   - **MX** `bounce` → `feedback.mailvender.com`, priority 10 (bounces come back here)
   - **TXT** `bounce` → `v=spf1 include:_spf.mailvender.com -all`
   - **TXT** `_dmarc`: you already added this in step 3; the app checks it.
3. Wait a few minutes and click **Verify**. All records should turn green.
4. In **Settings → Senders**, approve the address you'll send from, e.g.
   `hello@mailvender.com`.

From now on, all mail from `@mailvender.com` is DKIM-signed, including the
verification and password-reset emails.

## 12. Send a test and check it

1. Open an email in the studio, use **Send test**, and send it to your Gmail.
2. In Gmail, open the message → **⋮ (More) → Show original**. You want:

   ```
   SPF:   PASS with IP YOUR_SERVER_IP
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
  on day one gets it blocked.
- Register `mailvender.com` in [Google Postmaster Tools](https://postmaster.google.com/)
  to see your Gmail reputation, and your IP in
  [Microsoft SNDS](https://sendersupport.olc.protection.outlook.com/snds/).
- Once deliverability looks good, tighten DMARC from `p=none` to
  `p=quarantine`.

## 13. Updating after code changes

From your Mac, copy the changes (step 5), then on the server:

```sh
cd /opt/mailvender/mailvender-backend
docker compose -f docker-compose.prod.yml up -d --build    # migrations run automatically

cd /opt/mailvender/mailvender
npm ci && API_INTERNAL_URL=http://localhost:8000 npm run build
systemctl restart mailvender-web
```

Queued mail is kept across restarts (the `postfix-queue` volume).

## 14. Troubleshooting

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
| `connect to gmail-smtp-in…:25: Connection timed out`        | Outbound port 25 is blocked by your provider. Get it unblocked, or use a relay (below).                                                      |
| `status=bounced … 550-5.7.1 … PTR record`                   | Reverse DNS is missing or wrong. Redo step 4.                                                                                                |
| `status=bounced … 550-5.7.26 … unauthenticated`             | SPF and DKIM both failed. Check the `_spf` and `@` TXT records (step 3) and that the domain is verified (step 11).                            |
| `status=deferred … 421-4.7.0 … rate limited` / `suspicious` | Gmail is slowing down a new IP. It retries automatically. Send less and warm up gradually.                                                   |
| Nothing at all                                              | The worker never handed it over. Check the `worker` logs, that the sender is approved (step 11), and that the recipient isn't suppressed.   |

**A service won't start.** Run `docker compose -f docker-compose.prod.yml logs <service>`.
The production checks name the setting to fix.

**Login works but you're logged out immediately.** You opened the site over
`http://`. Use `https://www.mailvender.com`; the session cookie is HTTPS-only.

**Sending through a relay (if port 25 stays blocked).** Postfix can hand all
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
