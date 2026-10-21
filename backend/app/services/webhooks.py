"""Webhooks: tell a customer's own server what happened to their email, as it happens.

An account owner adds an endpoint (an https URL and the events it wants). When one of those events
happens, `emit` records a delivery per listening endpoint and queues a `webhook` outbox event; the
worker POSTs the JSON event, signed, and retries with backoff until the endpoint answers 2xx.

Events (EVENTS): message.sent · message.failed · message.bounced · message.complained ·
recipient.unsubscribed, plus webhook.test from the "Send test" button.

Signing: `Mailvender-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>" with the
endpoint's secret>`. The secret (`whsec_…`) is shown once when the endpoint is created or its
secret is rotated; it's stored encrypted (it has to be used, not just compared). Receivers check
the signature and that `t` is recent (docs/WEBHOOKS.md has code). `Mailvender-Event-Id` stays the
same across retries, so receivers can ignore duplicates – delivery is at-least-once.

Safety: only https URLs whose host resolves to public addresses (no localhost, private networks or
cloud metadata – checked when saved and again before every request, which also doesn't follow
redirects), a 10 s timeout, and only the first 1 KB of the response is kept. An endpoint that keeps
failing for 3 days is switched off and the owners are emailed.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app import models as m
from app.config import get_settings
from app.errors import ApiError, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import decrypt_secret, encrypt_secret, new_token, utcnow
from app.services import emails
from app.services.common import AccountContext, Actor, app_url, audit, enqueue_system_email

log = get_logger("webhooks")

EVENTS = {
    "message.sent": "Our mail server accepted the email for a recipient and is delivering it.",
    "message.failed": "We couldn't send to a recipient (rejected, suppressed, or gave up retrying).",
    "message.bounced": "The recipient's server bounced the email (hard: the address doesn't exist; soft: try "
                       "later).",
    "message.complained": "The recipient marked the email as spam.",
    "recipient.unsubscribed": "The recipient clicked unsubscribe; they won't get email from this account again.",
}
TEST_EVENT = "webhook.test"
# Waits before each retry: about 1.5 days of trying in all.
BACKOFF = [timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(minutes=30),
           timedelta(hours=1), timedelta(hours=3), timedelta(hours=6), timedelta(hours=12), timedelta(hours=12)]
MAX_ATTEMPTS = len(BACKOFF) + 1
DISABLE_AFTER = timedelta(days=3)
KEEP_RESPONSE_BYTES = 1024


@dataclass
class Response:
    status: int
    body: str


Post = Callable[[str, bytes, dict[str, str], float], Response]


def resolve_host(host: str) -> list[str]:
    """Addresses the host resolves to (replaced in tests)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def url_problem(url: str) -> str | None:
    """Why this URL can't be a webhook endpoint, or None."""
    settings = get_settings()
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "Use a full URL, like https://example.com/webhooks/mailvender."
    if parts.scheme == "http" and not settings.webhook_allow_http:
        return "Use https:// – events contain your recipients' addresses."
    if parts.username or parts.password:
        return "Don't put a username or password in the URL; check our signature instead."
    if settings.webhook_allow_private_addresses:
        return None
    try:
        addresses = resolve_host(parts.hostname)
    except (OSError, UnicodeError):
        return "We couldn't find that host. Check the URL."
    if not addresses or any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        return "The URL must point to a server on the public internet."
    return None


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> Response:
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("POST", url, content=body, headers=headers) as response:
            chunk = b""
            for part in response.iter_bytes():
                chunk += part
                if len(chunk) >= KEEP_RESPONSE_BYTES:
                    break
            return Response(response.status_code, chunk[:KEEP_RESPONSE_BYTES].decode(errors="replace"))


def signature(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def emit(uow: UnitOfWork, account_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> int:
    """Queues `event_type` for every enabled endpoint of the account that listens to it. The caller commits."""
    endpoints = uow.webhooks.listening(account_id, event_type)
    if not endpoints:
        return 0
    account = uow.accounts.get(account_id)
    if account is None or account.status != "active":
        return 0  # a deleted account's endpoints stay quiet (they're erased with it)
    event_id = uuid.uuid4()
    payload = {"id": str(event_id), "type": event_type, "created_at": utcnow().isoformat(),
               "account_id": str(account_id), "data": data}
    for endpoint in endpoints:
        delivery = uow.webhooks.add(m.WebhookDelivery(
            account_id=account_id, endpoint_id=endpoint.id, event_id=event_id, event_type=event_type,
            payload=payload, status="pending", attempts=0, next_attempt_at=utcnow()))
        uow.outbox.add(m.OutboxEvent(kind="webhook", account_id=account_id,
                                     payload={"delivery_id": str(delivery.id)}, available_at=utcnow()))
    return len(endpoints)


def recipient_event(uow: UnitOfWork, recipient: m.MessageRecipient, event_type: str, **extra: Any) -> int:
    """emit() for an event about one recipient of a message, with the message's details."""
    message = uow.messages.get_message_by_id(recipient.message_id)
    data: dict[str, Any] = {
        "message_id": str(recipient.message_id),
        "recipient_id": str(recipient.id),
        "email": recipient.email,
        "status": recipient.status,
    }
    if message is not None:
        data.update({"from": message.from_email, "subject": message.subject, "category": message.category,
                     "metadata": message.metadata_ or {}})
    data.update(extra)
    return emit(uow, recipient.account_id, event_type, data)


class WebhookService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    # ---- Endpoints (account owners) ----

    def _validate(self, url: str | None, events: list[str] | None) -> list[str] | None:
        errors = []
        if url is not None and (problem := url_problem(url)):
            errors.append(field_error("url", problem, "invalid_url"))
        if events is not None:
            unknown = [e for e in events if e not in EVENTS]
            if unknown:
                errors.append(field_error("events", f"Unknown events: {', '.join(unknown)}.", "invalid"))
            elif not events:
                errors.append(field_error("events", "Choose at least one event.", "required"))
        if errors:
            raise ValidationFailed(errors)
        return sorted(set(events)) if events is not None else None

    def create(self, ctx: AccountContext, url: str, events: list[str], description: str | None
               ) -> tuple[m.WebhookEndpoint, str]:
        ctx.require_owner()
        if len(self.uow.webhooks.list_for_account(ctx.account_id)) >= self.settings.webhook_max_endpoints:
            raise ApiError(f"An account can have up to {self.settings.webhook_max_endpoints} webhook endpoints.",
                           code="webhook_limit", status_code=409)
        chosen = self._validate(url, events)
        secret = "whsec_" + new_token()
        endpoint = self.uow.webhooks.add(m.WebhookEndpoint(
            account_id=ctx.account_id, url=url.strip(), description=(description or "").strip() or None,
            events=chosen, secret_encrypted=encrypt_secret(secret), enabled=True))
        audit(self.uow, ctx.account_id, ctx.actor, "webhook.created", target_type="webhook", target_id=endpoint.id,
              url=endpoint.url, events=chosen)
        self.uow.commit()
        return endpoint, secret

    def list_endpoints(self, ctx: AccountContext) -> list[m.WebhookEndpoint]:
        ctx.require_owner()
        return self.uow.webhooks.list_for_account(ctx.account_id)

    def get(self, ctx: AccountContext, endpoint_id: uuid.UUID) -> m.WebhookEndpoint:
        ctx.require_owner()
        endpoint = self.uow.webhooks.get(endpoint_id, ctx.account_id)
        if endpoint is None:
            raise NotFound("Webhook not found.")
        return endpoint

    def update(self, ctx: AccountContext, endpoint_id: uuid.UUID, *, url: str | None, events: list[str] | None,
               description: str | None, enabled: bool | None) -> m.WebhookEndpoint:
        endpoint = self.get(ctx, endpoint_id)
        chosen = self._validate(url, events)
        if url is not None:
            endpoint.url = url.strip()
        if chosen is not None:
            endpoint.events = chosen
        if description is not None:
            endpoint.description = description.strip() or None
        if enabled is not None and enabled != endpoint.enabled:
            endpoint.enabled = enabled
            endpoint.disabled_reason = None if enabled else "Turned off by the account."
            if enabled:
                endpoint.consecutive_failures = 0
        audit(self.uow, ctx.account_id, ctx.actor, "webhook.updated", target_type="webhook", target_id=endpoint.id,
              url=endpoint.url, events=endpoint.events, enabled=endpoint.enabled)
        self.uow.commit()
        return endpoint

    def rotate_secret(self, ctx: AccountContext, endpoint_id: uuid.UUID) -> tuple[m.WebhookEndpoint, str]:
        endpoint = self.get(ctx, endpoint_id)
        secret = "whsec_" + new_token()
        endpoint.secret_encrypted = encrypt_secret(secret)
        audit(self.uow, ctx.account_id, ctx.actor, "webhook.secret_rotated", target_type="webhook",
              target_id=endpoint.id)
        self.uow.commit()
        return endpoint, secret

    def delete(self, ctx: AccountContext, endpoint_id: uuid.UUID) -> None:
        endpoint = self.get(ctx, endpoint_id)
        audit(self.uow, ctx.account_id, ctx.actor, "webhook.deleted", target_type="webhook", target_id=endpoint.id,
              url=endpoint.url)
        self.uow.webhooks.delete(endpoint)
        self.uow.commit()

    def send_test(self, ctx: AccountContext, endpoint_id: uuid.UUID) -> m.WebhookDelivery:
        endpoint = self.get(ctx, endpoint_id)
        event_id = uuid.uuid4()
        payload = {"id": str(event_id), "type": TEST_EVENT, "created_at": utcnow().isoformat(),
                   "account_id": str(ctx.account_id),
                   "data": {"message": "A test from Mailvender. If you can read this, your endpoint works."}}
        delivery = self.uow.webhooks.add(m.WebhookDelivery(
            account_id=ctx.account_id, endpoint_id=endpoint.id, event_id=event_id, event_type=TEST_EVENT,
            payload=payload, status="pending", attempts=0, next_attempt_at=utcnow()))
        self.uow.outbox.add(m.OutboxEvent(kind="webhook", account_id=ctx.account_id,
                                          payload={"delivery_id": str(delivery.id)}, available_at=utcnow()))
        self.uow.commit()
        return delivery

    def deliveries(self, ctx: AccountContext, endpoint_id: uuid.UUID, page: PageRequest) -> Page[m.WebhookDelivery]:
        return self.uow.webhooks.deliveries_page(self.get(ctx, endpoint_id).id, page)

    def retry(self, ctx: AccountContext, endpoint_id: uuid.UUID, delivery_id: uuid.UUID) -> m.WebhookDelivery:
        endpoint = self.get(ctx, endpoint_id)
        delivery = self.uow.webhooks.get_delivery(delivery_id)
        if delivery is None or delivery.endpoint_id != endpoint.id:
            raise NotFound("Delivery not found.")
        if delivery.status == "pending":
            raise ApiError("This one is still being tried.", code="delivery_pending", status_code=409)
        delivery.status, delivery.attempts, delivery.next_attempt_at = "pending", 0, utcnow()
        self.uow.outbox.add(m.OutboxEvent(kind="webhook", account_id=ctx.account_id,
                                          payload={"delivery_id": str(delivery.id)}, available_at=utcnow()))
        self.uow.commit()
        return delivery

    # ---- Sending (worker) ----

    def deliver(self, event: m.OutboxEvent, post: Post = http_post) -> None:
        """One attempt. Leaves `event` done, or pending with a later `available_at` for a retry."""
        event.status = "done"
        delivery = self.uow.webhooks.get_delivery(uuid.UUID(event.payload["delivery_id"]))
        if delivery is None or delivery.status != "pending":
            return
        endpoint = self.uow.webhooks.get_by_id(delivery.endpoint_id)
        if endpoint is None:
            return
        if not endpoint.enabled and delivery.event_type != TEST_EVENT:
            delivery.status, delivery.error = "failed", "The endpoint is turned off."
            return
        now = utcnow()
        delivery.attempts += 1
        body = json.dumps(delivery.payload, separators=(",", ":"), default=str).encode()
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mailvender-Webhooks/1.0",
            "Mailvender-Event": delivery.event_type,
            "Mailvender-Event-Id": str(delivery.event_id),
            "Mailvender-Delivery-Id": str(delivery.id),
            "Mailvender-Signature": signature(decrypt_secret(endpoint.secret_encrypted), timestamp, body),
        }
        problem = url_problem(endpoint.url)
        response: Response | None = None
        if problem is None:
            try:
                response = post(endpoint.url, body, headers, self.settings.webhook_timeout_seconds)
            except httpx.HTTPError as error:
                problem = f"Couldn't connect: {type(error).__name__}: {error}"[:500]
        delivery.response_status = response.status if response else None
        delivery.response_body = response.body if response else None
        if response is not None and 200 <= response.status < 300:
            delivery.status, delivery.delivered_at, delivery.error, delivery.next_attempt_at = "delivered", now, None, None
            endpoint.consecutive_failures, endpoint.last_success_at = 0, now
            log_event(log, "webhook.delivered", delivery_id=str(delivery.id), attempts=delivery.attempts)
            return
        delivery.error = problem or f"Your server answered {response.status if response else '?'}."
        endpoint.last_failure_at = now
        if delivery.attempts < MAX_ATTEMPTS and delivery.event_type != TEST_EVENT:
            delivery.next_attempt_at = now + BACKOFF[delivery.attempts - 1]
            event.status, event.available_at = "pending", delivery.next_attempt_at
            log_event(log, "webhook.retry_scheduled", logging.INFO, delivery_id=str(delivery.id),
                      attempt=delivery.attempts, status=delivery.response_status)
            return
        delivery.status, delivery.next_attempt_at = "failed", None
        if delivery.event_type != TEST_EVENT:
            endpoint.consecutive_failures += 1
            self._maybe_disable(endpoint, now)
        log_event(log, "webhook.failed", logging.WARNING, delivery_id=str(delivery.id), error=delivery.error)

    def _maybe_disable(self, endpoint: m.WebhookEndpoint, now: datetime) -> None:
        healthy_since = endpoint.last_success_at or endpoint.created_at
        if not endpoint.enabled or now - healthy_since < DISABLE_AFTER:
            return
        endpoint.enabled = False
        endpoint.disabled_reason = (f"Turned off automatically on {now:%d %b %Y}: every event failed for "
                                    f"{DISABLE_AFTER.days} days.")
        audit(self.uow, endpoint.account_id, Actor.system(), "webhook.disabled", target_type="webhook",
              target_id=endpoint.id, url=endpoint.url)
        for user_id in self.uow.memberships.list_user_ids(endpoint.account_id):
            membership = self.uow.memberships.get(endpoint.account_id, user_id)
            user = self.uow.users.get(user_id)
            if membership and membership.role == "owner" and user:
                enqueue_system_email(self.uow, user.email, emails.webhook_disabled(
                    endpoint.url, DISABLE_AFTER.days, app_url("/settings#webhooks")), kind="webhook_disabled")
        log_event(log, "webhook.disabled", logging.WARNING, endpoint_id=str(endpoint.id))
