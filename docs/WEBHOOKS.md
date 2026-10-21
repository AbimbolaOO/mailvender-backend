# Webhooks

Webhooks let your own server know what happens to the email you send through
Mailvender, as it happens: so you can mark an address as bad in your CRM when
it bounces, stop emailing someone who unsubscribed, or show "sent" in your app.

You give Mailvender a URL. When an event you chose happens, Mailvender sends
an HTTPS `POST` to it with a small JSON body, signed so you can check it really
came from us.

- [Set one up](#set-one-up)
- [Events](#events)
- [What a request looks like](#what-a-request-looks-like)
- [Check the signature](#check-the-signature)
- [Answering, retries and duplicates](#answering-retries-and-duplicates)
- [Testing](#testing)
- [The API](#the-api)

---

## Set one up

Account owners: **Settings → Webhooks → Add a webhook**.

1. Paste your endpoint's URL. It must be `https://` and reachable from the
   internet (not `localhost` or a private network address).
2. Tick the events you want.
3. **Copy the signing secret** shown next (`whsec_…`). It's shown only once;
   store it with your server's other secrets. Lost it? Press **New secret**
   (the old one stops working at once).
4. Press **Send test**. A `webhook.test` event arrives at your URL, and
   **Deliveries** shows what your server answered.

An account can have up to 10 endpoints, each with its own events.

## Events

| Event | When |
| --- | --- |
| `message.sent` | Our mail server accepted the email for one recipient and is delivering it to their mail server. |
| `message.failed` | We couldn't send to a recipient: `data.reason` is `rejected` (refused, or gave up retrying – see `data.error`) or `suppressed` (the address is on your do-not-send list). |
| `message.bounced` | The recipient's server bounced it. `data.bounce.classification` is `hard` (the address doesn't exist – we won't send to it again) or `soft` (a temporary problem such as a full mailbox; repeated soft bounces also stop sending). |
| `message.complained` | The recipient marked it as spam. We won't send to them again. |
| `recipient.unsubscribed` | The recipient clicked your unsubscribe link (or their mail app's unsubscribe button). We won't send to them again. |
| `webhook.test` | You pressed **Send test**. |

There's one event per recipient: an email to 3 people gives 3 `message.sent`
events.

## What a request looks like

```http
POST /webhooks/mailvender HTTP/1.1
Content-Type: application/json
User-Agent: Mailvender-Webhooks/1.0
Mailvender-Event: message.bounced
Mailvender-Event-Id: 6f1c0e0a-4d0f-4a43-9f5e-2f1a7c1b8d21
Mailvender-Delivery-Id: 0d9b…
Mailvender-Signature: t=1792663200,v1=5b2c…e81f

{
  "id": "6f1c0e0a-4d0f-4a43-9f5e-2f1a7c1b8d21",
  "type": "message.bounced",
  "created_at": "2026-10-22T10:00:00.123456+00:00",
  "account_id": "ff585b5a-…",
  "data": {
    "message_id": "9a8b…",
    "recipient_id": "1c2d…",
    "email": "someone@example.org",
    "status": "bounced",
    "from": "news@yourbrand.com",
    "subject": "Our autumn sale",
    "category": "bulk",
    "metadata": {"order_id": "1234"},
    "bounce": {"classification": "hard", "status_code": "5.1.1", "diagnostic": "550 5.1.1 User unknown"}
  }
}
```

- `message_id` is the id `POST /v1/messages` returned; `metadata` is what you
  sent with it – put your own ids there to match events to your records.
- `message.failed` adds `reason` and `error`; `message.complained` and
  `recipient.unsubscribed` add `source`.
- Timestamps are UTC (ISO 8601), ids are UUIDs.

## Check the signature

Anyone can send a request to your URL, so check every request:

1. Read the **raw body** exactly as received (before parsing JSON).
2. From `Mailvender-Signature`, take `t` (Unix seconds) and `v1`.
3. Compute HMAC-SHA256 with your secret over `"<t>.<raw body>"`, as lowercase hex.
4. It must equal `v1` (compare in constant time), and `t` must be within
   5 minutes of now (stops someone replaying an old request).

**Python** (Flask):

```python
import hashlib, hmac, os, time
from flask import Flask, abort, request

SECRET = os.environ["MAILVENDER_WEBHOOK_SECRET"]  # whsec_…
app = Flask(__name__)

@app.post("/webhooks/mailvender")
def mailvender():
    parts = dict(p.split("=", 1) for p in request.headers["Mailvender-Signature"].split(","))
    signed = f"{parts['t']}.".encode() + request.get_data()
    expected = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts["v1"]) or abs(time.time() - int(parts["t"])) > 300:
        abort(400)
    event = request.get_json()
    # … handle event["type"]; skip it if you've seen event["id"] before
    return "", 204
```

**Node.js** (Express):

```js
import crypto from 'node:crypto';
import express from 'express';

const SECRET = process.env.MAILVENDER_WEBHOOK_SECRET; // whsec_…
const app = express();

app.post('/webhooks/mailvender', express.raw({ type: 'application/json' }), (req, res) => {
  const parts = Object.fromEntries(req.get('Mailvender-Signature').split(',').map((p) => p.split('=')));
  const expected = crypto.createHmac('sha256', SECRET).update(`${parts.t}.`).update(req.body).digest('hex');
  const ok = parts.v1?.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
  if (!ok || Math.abs(Date.now() / 1000 - Number(parts.t)) > 300) return res.sendStatus(400);
  const event = JSON.parse(req.body);
  // … handle event.type; skip it if you've seen event.id before
  res.sendStatus(204);
});
```

## Answering, retries and duplicates

- **Answer `2xx` within 10 seconds.** Do slow work afterwards (a queue), not
  before answering.
- Anything else (`4xx`, `5xx`, a timeout, no connection) is **retried**: after
  30 seconds, 2 and 10 minutes, then ½, 1, 3, 6 and 12 hours – 10 tries over
  about a day and a half. Redirects aren't followed.
- The same event can arrive **more than once** (for example if your server
  saved it but the answer got lost). `Mailvender-Event-Id` and the body's `id`
  stay the same on every retry: store the ids you've handled and skip repeats.
- Events can arrive **out of order** (a retry can land after a newer event).
  Use `created_at` if order matters.
- **Turned off automatically:** if every event to an endpoint fails for 3 days,
  we turn it off and email the account's owners. Fix it, then **Turn on** and
  **Send test**. Events from while it was off aren't resent; the API
  (`GET /v1/messages/{id}`) has every message's status.
- **Deliveries** (Settings → Webhooks) lists the last 30 days: each event, the
  HTTP status and the first part of your server's answer. **Send again**
  resends one.

## Testing

- **Send test** sends a `webhook.test` event once (no retries), even while the
  endpoint is turned off.
- To look at real requests before writing code, point a webhook at a request
  inspector such as <https://webhook.site>, or expose your laptop with a
  tunnel (`ngrok http 3000`, Cloudflare Tunnel) and use its https URL.
- Running Mailvender itself locally (developers): the local stack allows
  `http://` and private addresses, so `http://host.docker.internal:<port>/…`
  reaches a server on your Mac.

## The API

Signed-in account owners (the same calls the Settings page makes); full
details at `/docs`:

| Call | What it does |
| --- | --- |
| `GET /v1/webhooks/events` | The event types and what they mean |
| `GET /v1/webhooks` | Your endpoints |
| `POST /v1/webhooks` `{url, events, description?}` | Add one (answer includes the secret, once) |
| `PATCH /v1/webhooks/{id}` `{url?, events?, description?, enabled?}` | Change it, or turn it off/on |
| `DELETE /v1/webhooks/{id}` | Delete it and its history |
| `POST /v1/webhooks/{id}/rotate-secret` | New secret (shown once) |
| `POST /v1/webhooks/{id}/test` | Send a `webhook.test` event |
| `GET /v1/webhooks/{id}/deliveries` | Recent deliveries, newest first |
| `POST /v1/webhooks/{id}/deliveries/{delivery_id}/retry` | Send one again |
