"""Suppressions, one-click unsubscribe, bounce (DSN) and complaint (ARF) processing."""

import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from email import message_from_bytes, policy
from email.message import Message as EmailMessageType

from app import models as m
from app.config import get_settings
from app.errors import Conflict, NotFound, Unauthenticated
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import read_recipient_token, utcnow
from app.services.common import AccountContext, Actor, audit, normalize_email
from app.services.sending import personalize, recipient_from_envelope, recipient_links

log = get_logger("feedback")

# DSN status codes that look permanent but are usually temporary.
SOFT_PERMANENT_CODES = {"5.2.2", "5.4.7", "5.7.0"}


@dataclass
class DsnRecipient:
    action: str
    status: str
    final_recipient: str | None
    diagnostic: str | None


@dataclass
class ProcessingResult:
    correlated: bool
    classification: str | None = None
    suppressed: bool = False


def _parts(message: EmailMessageType) -> list[EmailMessageType]:
    return list(message.walk())


def _header_block_value(message: EmailMessageType, name: str) -> str | None:
    """Finds a header in an attached original message (message/rfc822 or text/rfc822-headers)."""
    for part in _parts(message):
        content_type = part.get_content_type()
        if content_type == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
            if inner is not None and inner.get(name):
                return str(inner.get(name)).strip()
        elif content_type == "text/rfc822-headers":
            text = part.get_payload(decode=True) or b""
            match = re.search(rf"(?im)^{re.escape(name)}:\s*(\S+)", text.decode(errors="replace"))
            if match:
                return match.group(1)
    return None


def parse_dsn(raw: bytes) -> tuple[list[DsnRecipient], uuid.UUID | None]:
    message = message_from_bytes(raw, policy=policy.compat32)
    recipients: list[DsnRecipient] = []
    for part in _parts(message):
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        blocks = payload if isinstance(payload, list) else []
        if not blocks:  # some MTAs send it as a single text block
            text = (part.get_payload(decode=True) or b"").decode(errors="replace")
            blocks = [message_from_bytes(chunk.encode()) for chunk in re.split(r"\r?\n\r?\n", text) if chunk.strip()]
        for block in blocks[1:] if len(blocks) > 1 else blocks:
            status = str(block.get("Status", "")).strip()
            action = str(block.get("Action", "")).strip().lower()
            if not status and not action:
                continue
            final = str(block.get("Final-Recipient", "") or block.get("Original-Recipient", ""))
            recipients.append(
                DsnRecipient(
                    action=action,
                    status=status,
                    final_recipient=final.split(";", 1)[-1].strip().lower() or None,
                    diagnostic=str(block.get("Diagnostic-Code", "")).split(";", 1)[-1].strip() or None,
                )
            )
    header_id = _header_block_value(message, "X-Mailvender-Recipient-Id")
    try:
        recipient_id = uuid.UUID(header_id) if header_id else None
    except ValueError:
        recipient_id = None
    return recipients, recipient_id


def classify(dsn: DsnRecipient) -> str | None:
    if dsn.action in ("delivered", "relayed", "expanded"):
        return None
    if dsn.status.startswith("5.") and dsn.status not in SOFT_PERMANENT_CODES:
        return "hard"
    if dsn.status.startswith(("4.", "5.")) or dsn.action in ("failed", "delayed"):
        return "soft"
    return None


class FeedbackService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    # ---- Suppressions ----

    def suppress(self, account_id: uuid.UUID, email: str, reason: str, source: str, actor: Actor,
                 recipient_id: uuid.UUID | None = None) -> tuple[m.Suppression, bool]:
        suppression, created = self.uow.suppressions.add_if_absent(
            m.Suppression(account_id=account_id, email=email, reason=reason, source=source, recipient_id=recipient_id)
        )
        if created:
            audit(self.uow, account_id, actor, "suppression.created", target_type="suppression",
                  target_id=suppression.id, reason=reason, source=source, email=email)
            log_event(log, "suppression.created", reason=reason, source=source)
        return suppression, created

    def list_page(self, ctx: AccountContext, page: PageRequest) -> Page[m.Suppression]:
        return self.uow.suppressions.list_page(ctx.account_id, page)

    def add_manual(self, ctx: AccountContext, email: str) -> m.Suppression:
        suppression, _ = self.suppress(ctx.account_id, normalize_email(email), "manual", "account_user", ctx.actor)
        self.uow.commit()
        return suppression

    def remove(self, ctx: AccountContext, email: str) -> None:
        suppression = self.uow.suppressions.get(ctx.account_id, normalize_email(email))
        if suppression is None:
            raise NotFound("This address isn't suppressed.")
        if suppression.reason != "manual":
            # Unsubscribes, complaints and hard bounces are kept for compliance.
            raise Conflict("Only manually added suppressions can be removed.", code="suppression_protected")
        self.uow.suppressions.delete(suppression)
        audit(self.uow, ctx.account_id, ctx.actor, "suppression.removed", target_type="suppression",
              target_id=suppression.id, email=suppression.email)
        self.uow.commit()

    # ---- Unsubscribe / view in browser (public, token-authenticated) ----

    def _recipient_from_token(self, token: str, purpose: str) -> m.MessageRecipient | None:
        parsed = read_recipient_token(token, purpose, timedelta(days=self.settings.unsubscribe_token_ttl_days))
        if parsed is None:
            return None
        recipient = self.uow.messages.get_recipient(parsed.recipient_id)
        if recipient is None or recipient.account_id != parsed.account_id:
            return None
        return recipient

    def unsubscribe(self, token: str, source: str) -> m.MessageRecipient | None:
        """Idempotent: unsubscribing twice leaves one suppression."""
        recipient = self._recipient_from_token(token, "unsubscribe")
        if recipient is None:
            return None
        self.suppress(recipient.account_id, recipient.email, "unsubscribe", source, Actor("public"), recipient.id)
        self.uow.commit()
        return recipient

    def view_in_browser(self, token: str) -> str | None:
        recipient = self._recipient_from_token(token, "view")
        if recipient is None:
            return None
        message = self.uow.messages.get_message_by_id(recipient.message_id)
        account = self.uow.accounts.get(recipient.account_id)
        if message is None or account is None or not message.html:
            return None
        return personalize(message.html, recipient_links(recipient, account), html=True)

    # ---- Bounces ----

    def process_dsn(self, raw: bytes, envelope_recipient: str | None) -> ProcessingResult:
        dsn_recipients, header_id = parse_dsn(raw)
        recipient_id = (recipient_from_envelope(envelope_recipient) if envelope_recipient else None) or header_id
        recipient = self.uow.messages.get_recipient(recipient_id, for_update=True) if recipient_id else None
        if recipient is None:
            log_event(log, "dsn.uncorrelated", logging.WARNING)
            return ProcessingResult(correlated=False)
        report = next((d for d in dsn_recipients if d.final_recipient == recipient.email), None) or (
            dsn_recipients[0] if dsn_recipients else None
        )
        classification = classify(report) if report else None
        if classification is None:
            return ProcessingResult(correlated=True)
        now = utcnow()
        event = self.uow.delivery.add_event(
            m.DeliveryEvent(
                account_id=recipient.account_id,
                recipient_id=recipient.id,
                email=recipient.email,
                type="bounce",
                classification=classification,
                status_code=report.status if report else None,
                diagnostic=(report.diagnostic or "")[:1000] if report else None,
                source="dsn",
            )
        )
        audit(self.uow, recipient.account_id, Actor.system(), "delivery.bounce_classified",
              target_type="message_recipient", target_id=recipient.id, classification=classification,
              status=report.status if report else None, event_id=str(event.id))
        if report and report.action != "delayed":
            recipient.status, recipient.bounced_at, recipient.updated_at = "bounced", now, now
        suppressed = False
        if classification == "hard":
            _, suppressed = self.suppress(recipient.account_id, recipient.email, "hard_bounce", "dsn",
                                          Actor.system(), recipient.id)
        else:
            since = now - timedelta(days=self.settings.soft_bounce_window_days)
            if self.uow.delivery.count_soft_bounces(recipient.account_id, recipient.email,
                                                    since) >= self.settings.soft_bounce_threshold:
                _, suppressed = self.suppress(recipient.account_id, recipient.email, "soft_bounce_threshold", "dsn",
                                              Actor.system(), recipient.id)
        self.uow.commit()
        log_event(log, "message.bounced", message_id=str(recipient.message_id), recipient_id=str(recipient.id),
                  classification=classification)
        return ProcessingResult(correlated=True, classification=classification, suppressed=suppressed)

    # ---- Complaints ----

    def verify_feedback_source(self, source: str, body: bytes, signature: str | None) -> None:
        secret = self.settings.feedback_loop_sources.get(source)
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else None
        if not expected or not signature or not hmac.compare_digest(expected, signature.strip()):
            log_event(log, "complaint.unauthenticated", logging.WARNING, source=source)
            raise Unauthenticated("Unknown feedback source or bad signature.", code="invalid_signature")

    def process_complaint(self, source: str, raw: bytes) -> ProcessingResult:
        message = message_from_bytes(raw, policy=policy.compat32)
        header_id = _header_block_value(message, "X-Mailvender-Recipient-Id")
        recipient = None
        try:
            if header_id:
                recipient = self.uow.messages.get_recipient(uuid.UUID(header_id), for_update=True)
        except ValueError:
            recipient = None
        if recipient is None:
            log_event(log, "complaint.uncorrelated", logging.WARNING, source=source)
            return ProcessingResult(correlated=False)
        now = utcnow()
        self.uow.delivery.add_event(
            m.DeliveryEvent(
                account_id=recipient.account_id,
                recipient_id=recipient.id,
                email=recipient.email,
                type="complaint",
                classification=None,
                source=f"fbl:{source}",
            )
        )
        recipient.status, recipient.complained_at, recipient.updated_at = "complained", now, now
        audit(self.uow, recipient.account_id, Actor.system(), "delivery.complaint_received",
              target_type="message_recipient", target_id=recipient.id, source=source)
        _, suppressed = self.suppress(recipient.account_id, recipient.email, "complaint", f"fbl:{source}",
                                      Actor.system(), recipient.id)
        self.uow.commit()
        log_event(log, "message.complained", message_id=str(recipient.message_id), recipient_id=str(recipient.id))
        return ProcessingResult(correlated=True, classification="complaint", suppressed=suppressed)
