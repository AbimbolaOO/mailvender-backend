"""Accepting send requests and delivering them through the MTA.

Lifecycle of each recipient::

    queued ──► accepted_by_mta ──► bounced | complained
       │
       ├──► failed       (permanent SMTP error, retry limit reached, account/domain no longer allowed)
       └──► suppressed   (address suppressed after the message was queued)

A send request is validated as a whole – nothing is queued unless every
recipient is acceptable. The message, its recipients and one outbox event per
recipient are committed in a single transaction; the worker consumes the outbox.
"""

import hashlib
import html as html_lib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from email import policy as email_policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from urllib.parse import quote

from app import models as m
from app.config import get_settings
from app.errors import Conflict, FieldError, Forbidden, NotFound, RateLimited, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import DuplicateError, UnitOfWork
from app.security import sign_recipient_token, utcnow
from app.services.common import AccountContext, Actor, audit, try_normalize_email
from app.services.mta import MailTransport, PermanentDeliveryError, TemporaryDeliveryError

log = get_logger("sending")

UNSUBSCRIBE_PLACEHOLDER = "{{unsubscribe_url}}"
VIEW_PLACEHOLDER = "{{view_in_browser_url}}"
ADDRESS_PLACEHOLDER = "{{physical_address}}"
CATEGORIES = ("transactional", "bulk")
MIN_TEXT_CHARS = 20
MAX_METADATA_KEYS = 20


@dataclass
class SendRequest:
    from_email: str
    to: list[str]
    subject: str
    html: str
    text: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    category: str = "transactional"
    metadata: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "from": self.from_email.strip().lower(),
                "from_name": self.from_name,
                "reply_to": self.reply_to,
                "to": [address.strip().lower() for address in self.to],
                "subject": self.subject,
                "html": self.html,
                "text": self.text,
                "category": self.category,
                "metadata": self.metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class SendResult:
    message: m.Message
    recipients: list[m.MessageRecipient]
    replayed: bool


def html_to_text(html: str) -> str:
    """A readable plain-text alternative: links kept as "text (url)", block elements as line breaks."""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", html)
    text = re.sub(
        r'(?is)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda match: f"{re.sub(r'<[^>]+>', '', match.group(2)).strip()} ({match.group(1)})",
        text,
    )
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table)>", "\n", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _usable_text(text: str | None) -> bool:
    return bool(text) and len(re.sub(r"\s+", "", text or "")) >= MIN_TEXT_CHARS


class SendingService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    # ---- Accepting ----

    def send(self, ctx: AccountContext, request: SendRequest, idempotency_key: str | None) -> SendResult:
        key = (idempotency_key or "").strip()
        if not key or len(key) > 255 or not key.isprintable():
            raise ValidationFailed(
                [field_error("Idempotency-Key", "Send a unique Idempotency-Key header (1–255 characters).",
                             "idempotency_key_required")]
            )
        fingerprint = request.fingerprint()
        existing = self.uow.messages.get_by_idempotency_key(ctx.account_id, key)
        if existing:
            return self._replay(existing, fingerprint)

        # Lock the account row: sends for one account are serialized, so the
        # hourly/daily limits hold under concurrency.
        account = self.uow.accounts.get(ctx.account_id, for_update=True)
        assert account is not None
        if account.status != "active":
            raise Forbidden("This account can no longer send email.", code="account_inactive")
        if account.sending_suspended_at:
            raise Forbidden(
                "Sending is suspended for this account. You can still sign in and export your data; "
                "contact support to request a review.",
                code="account_suspended",
                details={"reason": account.suspension_reason},
            )

        recipients, identity, domain = self._validate(account, request)
        self._enforce_limits(account, len(recipients))

        text = request.text if request.text and request.text.strip() else html_to_text(request.html)
        message = m.Message(
            account_id=account.id,
            domain_id=domain.id,
            sender_identity_id=identity.id,
            from_email=identity.email,
            from_name=(request.from_name or identity.display_name or "").strip() or None,
            reply_to=try_normalize_email(request.reply_to) if request.reply_to else None,
            subject=request.subject,
            html=request.html,
            text_body=text,
            category=request.category,
            metadata_=request.metadata,
            idempotency_key=key,
            request_hash=fingerprint,
            created_by_user_id=ctx.user.id if ctx.user else None,
            created_by_api_key_id=ctx.api_key.id if ctx.api_key else None,
        )
        try:
            self.uow.messages.add(message)
        except DuplicateError:
            # A concurrent request with the same key won the race.
            existing = self.uow.messages.get_by_idempotency_key(ctx.account_id, key)
            if existing is None:
                raise
            return self._replay(existing, fingerprint)

        now = utcnow()
        rows = []
        for address in recipients:
            recipient = self.uow.messages.add_recipient(
                m.MessageRecipient(
                    message_id=message.id,
                    account_id=account.id,
                    domain_id=domain.id,
                    email=address,
                    status="queued",
                    attempts=0,
                )
            )
            self.uow.outbox.add(
                m.OutboxEvent(
                    kind="deliver_recipient",
                    account_id=account.id,
                    payload={"recipient_id": str(recipient.id)},
                    available_at=now,
                )
            )
            rows.append(recipient)
        self.uow.commit()
        log_event(log, "message.queued", message_id=str(message.id), recipients=len(rows), category=message.category)
        return SendResult(message=message, recipients=rows, replayed=False)

    def _replay(self, existing: m.Message, fingerprint: str) -> SendResult:
        if existing.request_hash != fingerprint:
            raise Conflict(
                "This Idempotency-Key was already used for a different request. Use a new key.",
                code="idempotency_key_reused",
            )
        log_event(log, "message.idempotent_replay", message_id=str(existing.id))
        return SendResult(existing, self.uow.messages.list_recipients(existing.id), replayed=True)

    def _validate(self, account: m.Account, request: SendRequest) -> tuple[list[str], m.SenderIdentity, m.Domain]:
        errors: list[FieldError] = []
        settings = self.settings

        # Recipients
        recipients: list[str] = []
        if not request.to:
            errors.append(field_error("to", "Add at least one recipient.", "required"))
        if len(request.to) > settings.max_recipients_per_message:
            errors.append(field_error("to", f"Send to at most {settings.max_recipients_per_message} recipients "
                                            "per request.", "too_many_recipients"))
        seen: set[str] = set()
        for index, raw in enumerate(request.to[: settings.max_recipients_per_message]):
            address = try_normalize_email(raw)
            if address is None:
                errors.append(field_error(f"to[{index}]", "This isn't a valid email address.", "invalid_email"))
            elif address in seen:
                errors.append(field_error(f"to[{index}]", "This recipient is listed twice.", "duplicate"))
            else:
                seen.add(address)
                recipients.append(address)

        # Content
        if not request.subject.strip():
            errors.append(field_error("subject", "Add a subject.", "required"))
        elif len(request.subject) > 998 or "\r" in request.subject or "\n" in request.subject:
            errors.append(field_error("subject", "The subject must be one line of at most 998 characters.", "invalid"))
        if not request.html.strip():
            errors.append(field_error("html", "The email has no content.", "required"))
        if len(request.html.encode()) > settings.max_html_bytes:
            errors.append(field_error("html", f"The HTML is larger than {settings.max_html_bytes // 1000} KB.",
                                      "too_large"))
        if request.text and len(request.text.encode()) > settings.max_text_bytes:
            errors.append(field_error("text", "The plain-text version is too large.", "too_large"))
        if request.category not in CATEGORIES:
            errors.append(field_error("category", "Use 'transactional' or 'bulk'.", "invalid"))
        if request.reply_to and try_normalize_email(request.reply_to) is None:
            errors.append(field_error("reply_to", "This isn't a valid email address.", "invalid_email"))
        if request.from_name and ("\r" in request.from_name or "\n" in request.from_name or len(request.from_name) > 200):
            errors.append(field_error("from_name", "Use one line of at most 200 characters.", "invalid"))
        if len(request.metadata) > MAX_METADATA_KEYS or any(
            len(k) > 64 or len(v) > 512 for k, v in request.metadata.items()
        ):
            errors.append(field_error("metadata", f"At most {MAX_METADATA_KEYS} keys; keys up to 64 and values up "
                                                  "to 512 characters.", "invalid"))

        # Sender: an approved identity on a verified domain of this account.
        identity: m.SenderIdentity | None = None
        domain: m.Domain | None = None
        from_email = try_normalize_email(request.from_email)
        if from_email:
            identity = self.uow.sender_identities.get_by_email(account.id, from_email)
            domain = self.uow.domains.get(identity.domain_id, account.id) if identity else None
        if identity is None or identity.status != "verified":
            errors.append(field_error("from", "Send from an approved sender of this account.", "sender_not_approved"))
        elif domain is None or domain.status != "verified":
            errors.append(field_error("from", "The sender's domain isn't verified. Check its DNS records.",
                                      "sender_domain_not_verified"))

        # Bulk-mail requirements
        if request.category == "bulk":
            if domain is not None and domain.status == "verified" and not domain.bulk_eligible:
                errors.append(field_error("from", "Publish a DMARC record with SPF and DKIM alignment before "
                                                  "sending bulk email from this domain.", "bulk_not_eligible"))
            if not _usable_text(request.text):
                errors.append(field_error("text", "Bulk email needs a plain-text version.", "text_required"))
            if UNSUBSCRIBE_PLACEHOLDER not in request.html:
                errors.append(field_error("html", f"Bulk email must contain an unsubscribe link "
                                                  f"({UNSUBSCRIBE_PLACEHOLDER}).", "unsubscribe_required"))
            if not account.physical_address:
                errors.append(field_error("physical_address", "Add your postal address in the account's sending "
                                                              "settings before sending bulk email.",
                                          "physical_address_missing"))
            elif ADDRESS_PLACEHOLDER not in request.html and account.physical_address not in request.html:
                errors.append(field_error("html", f"Bulk email must show your postal address "
                                                  f"({ADDRESS_PLACEHOLDER}).", "physical_address_required"))

        # Suppressions – per recipient, without saying why.
        suppressed = self.uow.suppressions.suppressed_among(account.id, recipients)
        for index, raw in enumerate(request.to[: settings.max_recipients_per_message]):
            if try_normalize_email(raw) in suppressed:
                errors.append(field_error(f"to[{index}]", "This recipient can't receive email from this account.",
                                          "suppressed"))

        if errors:
            raise ValidationFailed(errors, "Nothing was sent – fix the listed fields and try again.")
        assert identity is not None and domain is not None
        return recipients, identity, domain

    def _enforce_limits(self, account: m.Account, count: int) -> None:
        now = utcnow()
        hourly = self.uow.messages.count_recipients_since(account.id, now - timedelta(hours=1))
        daily = self.uow.messages.count_recipients_since(account.id, now - timedelta(days=1))
        exceeded = None
        if hourly + count > account.hourly_recipient_limit:
            exceeded = ("hourly", account.hourly_recipient_limit, 3600)
        elif daily + count > account.daily_recipient_limit:
            exceeded = ("daily", account.daily_recipient_limit, 86400)
        if exceeded is None:
            return
        period, limit, retry = exceeded
        window = now.replace(minute=0, second=0, microsecond=0)
        violations = self.uow.rate_limits.hit(f"limit_violation:{account.id}", window)
        if violations >= self.settings.limit_violations_before_suspension and not account.sending_suspended_at:
            suspend(self.uow, account, Actor.system(), "limit_violations",
                    f"{violations} sends over the {period} limit within an hour")
        self.uow.commit()
        log_event(log, "message.limit_exceeded", logging.WARNING, period=period, limit=limit)
        raise RateLimited(
            f"This would exceed your {period} sending limit of {limit} recipients.",
            code="sending_limit_exceeded",
            headers={"Retry-After": str(retry)},
            details={"period": period, "limit": limit, "used": hourly if period == "hourly" else daily},
        )

    # ---- Reading ----

    def get(self, ctx: AccountContext, message_id: uuid.UUID) -> tuple[m.Message, list[m.MessageRecipient]]:
        message = self.uow.messages.get(message_id, ctx.account_id)
        if message is None:
            raise NotFound("Message not found.")
        return message, self.uow.messages.list_recipients(message.id)

    def list_page(self, ctx: AccountContext, page: PageRequest) -> Page[m.Message]:
        return self.uow.messages.list_page(ctx.account_id, page)


def suspend(uow: UnitOfWork, account: m.Account, actor: Actor, code: str, reason: str) -> None:
    account.sending_suspended_at = utcnow()
    account.suspension_reason = reason
    audit(uow, account.id, actor, "account.suspended", target_type="account", target_id=account.id,
          reason=reason, code=code)
    log_event(log, "account.suspended", logging.WARNING, suspended_account_id=str(account.id), code=code)


# ---- Delivery (worker) ----


def public_url(path: str) -> str:
    return get_settings().public_api_base_url.rstrip("/") + path


def unsubscribe_url(recipient: m.MessageRecipient) -> str:
    return public_url(f"/u/{sign_recipient_token('unsubscribe', recipient.id, recipient.account_id)}")


def view_url(recipient: m.MessageRecipient) -> str:
    return public_url(f"/v/{sign_recipient_token('view', recipient.id, recipient.account_id)}")


def recipient_links(recipient: m.MessageRecipient, account: m.Account) -> dict[str, str]:
    """Placeholder values for one recipient (tokens are made once, so header and body links match)."""
    return {
        UNSUBSCRIBE_PLACEHOLDER: unsubscribe_url(recipient),
        VIEW_PLACEHOLDER: view_url(recipient),
        ADDRESS_PLACEHOLDER: account.physical_address or "",
    }


def personalize(content: str, links: dict[str, str], *, html: bool) -> str:
    for placeholder, value in links.items():
        content = content.replace(placeholder, html_lib.escape(value) if html else value)
        # Also inside URLs, where editors may have percent-encoded the braces.
        encoded = placeholder.replace("{", "%7B").replace("}", "%7D")
        content = content.replace(encoded, quote(value, safe=""))
    return content


# CRLF line endings and RFC 5322's 998-character limit: long headers such as
# List-Unsubscribe stay plain instead of being RFC 2047-encoded (which breaks them).
MIME_POLICY = email_policy.SMTP.clone(max_line_length=998)


def build_mime(message: m.Message, recipient: m.MessageRecipient, account: m.Account, domain: m.Domain) -> EmailMessage:
    links = recipient_links(recipient, account)
    mime = EmailMessage(policy=MIME_POLICY)
    local, _, host = message.from_email.partition("@")
    mime["From"] = Address(display_name=message.from_name or "", username=local, domain=host)
    mime["To"] = recipient.email
    mime["Subject"] = message.subject
    mime["Date"] = format_datetime(utcnow())
    mime["Message-ID"] = f"<{recipient.id}@{domain.name}>"
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    mime["X-Mailvender-Message-Id"] = str(message.id)
    mime["X-Mailvender-Recipient-Id"] = str(recipient.id)
    if message.category == "bulk":
        # RFC 8058 one-click unsubscribe.
        mime["List-Unsubscribe"] = f"<{links[UNSUBSCRIBE_PLACEHOLDER]}>"
        mime["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        mime["Precedence"] = "bulk"
    mime.set_content(personalize(message.text_body or "", links, html=False))
    mime.add_alternative(personalize(message.html or "", links, html=True), subtype="html")
    return mime


def envelope_sender(recipient: m.MessageRecipient, domain: m.Domain) -> str:
    """VERP: bounces come back to an address that identifies the recipient."""
    return f"b-{recipient.id.hex}@{domain.return_path_host}"


def recipient_from_envelope(address: str) -> uuid.UUID | None:
    match = re.match(r"^b-([0-9a-f]{32})@", address.strip().lower().strip("<>"))
    return uuid.UUID(match.group(1)) if match else None


class DeliveryService:
    def __init__(self, uow: UnitOfWork, transport: MailTransport):
        self.uow = uow
        self.transport = transport
        self.settings = get_settings()

    def backoff(self, attempt: int) -> timedelta:
        seconds = self.settings.delivery_backoff_base_seconds * 2 ** max(attempt - 1, 0)
        return timedelta(seconds=min(seconds, self.settings.delivery_backoff_max_seconds))

    def deliver(self, event: m.OutboxEvent) -> None:
        """Delivers one recipient. Leaves `event` done, or pending with a later `available_at`."""
        recipient = self.uow.messages.get_recipient(uuid.UUID(event.payload["recipient_id"]), for_update=True)
        event.status = "done"
        if recipient is None or recipient.status != "queued":
            return
        message = self.uow.messages.get_message_by_id(recipient.message_id)
        account = self.uow.accounts.get(recipient.account_id)
        domain = self.uow.domains.get(recipient.domain_id, recipient.account_id)
        assert message is not None and account is not None
        now = utcnow()
        fields = {"message_id": str(message.id), "recipient_id": str(recipient.id)}

        def finish(status: str, error: str | None = None) -> None:
            recipient.status = status
            recipient.updated_at = now
            recipient.last_error = error
            if status == "failed":
                recipient.failed_at = now
            log_event(log, f"message.{status}", logging.WARNING if status == "failed" else logging.INFO,
                      error=error, **fields)

        if account.status != "active" or account.sending_suspended_at:
            return finish("failed", "Account can no longer send")
        if domain is None or domain.status != "verified":
            return finish("failed", "Sender domain is no longer verified")
        if self.uow.suppressions.get(account.id, recipient.email):
            return finish("suppressed")

        mime = build_mime(message, recipient, account, domain)
        recipient.attempts += 1
        try:
            response = self.transport.send(envelope_sender(recipient, domain), [recipient.email], mime.as_bytes())
        except PermanentDeliveryError as error:
            self._attempt(recipient, "permanent_failure", error.code, str(error))
            return finish("failed", f"Rejected by the MTA: {error}")
        except TemporaryDeliveryError as error:
            self._attempt(recipient, "temporary_failure", error.code, str(error))
            if recipient.attempts >= self.settings.delivery_max_attempts:
                return finish("failed", f"Gave up after {recipient.attempts} attempts: {error}")
            event.status = "pending"
            event.attempts = recipient.attempts
            event.available_at = now + self.backoff(recipient.attempts)
            event.last_error = str(error)[:1000]
            recipient.last_error = str(error)[:1000]
            recipient.updated_at = now
            log_event(log, "message.retry_scheduled", logging.WARNING, attempt=recipient.attempts,
                      retry_at=event.available_at.isoformat(), **fields)
            return
        self._attempt(recipient, "accepted", 250, response)
        recipient.status = "accepted_by_mta"
        recipient.accepted_at = now
        recipient.updated_at = now
        recipient.last_error = None
        log_event(log, "message.accepted_by_mta", **fields)

    def _attempt(self, recipient: m.MessageRecipient, outcome: str, code: int | None, detail: str) -> None:
        self.uow.delivery.add_attempt(
            m.DeliveryAttempt(
                account_id=recipient.account_id,
                recipient_id=recipient.id,
                attempt=recipient.attempts,
                outcome=outcome,
                smtp_code=code,
                detail=detail[:1000],
            )
        )


def system_email(payload: dict[str, str]) -> EmailMessage:
    settings = get_settings()
    mime = EmailMessage(policy=MIME_POLICY)
    mime["From"] = settings.system_from_email
    mime["To"] = payload["to"]
    mime["Subject"] = payload["subject"]
    mime["Date"] = format_datetime(utcnow())
    mime["Message-ID"] = f"<{uuid.uuid4()}@{settings.system_from_email.rsplit('@', 1)[-1].strip('>')}>"
    mime["Auto-Submitted"] = "auto-generated"
    mime.set_content(payload["text"])
    return mime

