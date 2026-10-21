"""Mail to support@, postmaster@, abuse@ and dmarc@ (the platform's role addresses) and the admin inbox.

Cloudflare Email Routing receives the mail; a Cloudflare Email Worker
(infra/email-worker/) forwards a backup copy to Gmail and POSTs the raw message to
`/internal/inbound-mail`, signed with INBOUND_MAIL_SECRET. Here the raw message is stored in
object storage (<prefix>inbox/<mailbox>/<YYYY>/<MM>/<id>.eml) with a row for the list, and:

* complaint reports (ARF, e.g. Microsoft JMRP) at abuse@ go through the same processing as
  /internal/feedback-loop – suppression and the account's complaint rate;
* DMARC aggregate reports at dmarc@ are parsed into `dmarc_report_rows`.

The admin reads messages as headers + text; HTML is only ever served by `body_document` with a
Content-Security-Policy that blocks scripts (and remote images until asked), for a sandboxed iframe.
"""

import hashlib
import hmac
import html as html_lib
import logging
import re
import uuid
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from app import models as m
from app.config import get_settings
from app.errors import ApiError, NotFound, Unauthenticated
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import DuplicateError, UnitOfWork
from app.security import utcnow
from app.services import dmarc
from app.services.admin_auth import AdminAuth, admin_audit
from app.services.feedback import FeedbackService
from app.services.storage import ObjectStorage

log = get_logger("inbound")

MAILBOXES = ("support", "postmaster", "abuse", "dmarc")
MAX_TEXT_CHARS = 200_000
SHOWN_HEADERS = ("From", "To", "Cc", "Reply-To", "Date", "Subject", "Message-ID", "Authentication-Results")


@dataclass
class Attachment:
    index: int
    filename: str
    content_type: str
    size: int
    part: EmailMessage = field(repr=False)


@dataclass
class InboundDetail:
    message: m.InboundMessage
    headers: list[tuple[str, str]]
    text: str
    has_html: bool
    attachments: list[Attachment]
    dmarc_rows: list[m.DmarcReportRow]


def _parse(raw: bytes) -> EmailMessage:
    return message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


def _attachments(message: EmailMessage) -> list[Attachment]:
    found: list[Attachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment" and part.get_content_maintype() in ("text", "message"):
            continue
        payload = part.get_payload(decode=True) or b""
        found.append(Attachment(index=len(found), filename=(filename or f"attachment-{len(found) + 1}")[:200],
                                content_type=part.get_content_type(), size=len(payload), part=part))
    return found


def _body_part(message: EmailMessage, kind: str) -> str | None:
    part = message.get_body(preferencelist=(kind,))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    return content if isinstance(content, str) else None


def _kind(message: EmailMessage, mailbox: str, attachments: list[Attachment]) -> str:
    if message.get_content_type() == "multipart/report":
        report_type = (message.get_param("report-type") or "").lower()
        if report_type == "feedback-report":
            return "complaint_report"
        if report_type == "delivery-status":
            return "bounce"
    if mailbox == "dmarc" and any(dmarc.looks_like_report(a.filename, a.content_type) for a in attachments):
        return "dmarc_report"
    return "plain"


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "attachment"
    return cleaned[:120]


class InboundService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage):
        self.uow = uow
        self.storage = storage
        self.settings = get_settings()

    # ---- Receiving (Cloudflare Email Worker → /internal/inbound-mail) ----

    def verify_signature(self, body: bytes, signature: str | None) -> None:
        secret = self.settings.inbound_mail_secret
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else None
        if not expected or not signature or not hmac.compare_digest(expected, signature.strip()):
            log_event(log, "inbound.unauthenticated", logging.WARNING)
            raise Unauthenticated("Bad signature.", code="invalid_signature")

    def mailbox_for(self, address: str) -> str:
        local, _, domain = parseaddr(address)[1].lower().partition("@")
        if domain != self.settings.platform_domain or local not in MAILBOXES:
            raise ApiError(f"Only {', '.join(MAILBOXES)} at {self.settings.platform_domain} are accepted.",
                           code="unknown_mailbox", status_code=422)
        return local

    def receive(self, raw: bytes, to: str) -> tuple[m.InboundMessage, bool]:
        """Stores one message. Returns (message, created); a repeated delivery returns the first copy."""
        mailbox = self.mailbox_for(to)
        if len(raw) > self.settings.inbound_mail_max_bytes:
            raise ApiError("The message is too large.", code="payload_too_large", status_code=413)
        message = _parse(raw)
        message_id = str(message.get("Message-ID", "") or "").strip()[:500] or None
        if message_id:
            existing = self.uow.inbound.get_by_message_id(mailbox, message_id)
            if existing:
                return existing, False
        name, address = parseaddr(str(message.get("From", "") or ""))
        attachments = _attachments(message)
        kind = _kind(message, mailbox, attachments)
        now = utcnow()
        object_key = f"{self.settings.aws_s3_prefix}inbox/{mailbox}/{now:%Y/%m}/{uuid.uuid4()}.eml"
        self.storage.put(object_key, raw, "message/rfc822")
        try:
            inbound = self.uow.inbound.add(m.InboundMessage(
                mailbox=mailbox, message_id_header=message_id, from_address=(address or "unknown")[:320],
                from_name=name[:200] or None, subject=str(message.get("Subject", "") or "")[:1000],
                kind=kind, size=len(raw), object_key=object_key, has_attachments=bool(attachments), summary={},
            ))
        except DuplicateError:
            self.storage.delete(object_key)  # delivered twice at the same moment
            existing = self.uow.inbound.get_by_message_id(mailbox, message_id or "")
            if existing is None:
                raise
            return existing, False
        self.uow.commit()

        if kind == "complaint_report" and mailbox == "abuse":
            # Same as /internal/feedback-loop: suppression and the account's complaint rate.
            result = FeedbackService(self.uow).process_complaint("abuse-mailbox", raw)
            inbound.summary = {"correlated": result.correlated, "suppressed": result.suppressed}
        elif kind == "dmarc_report":
            inbound.summary = self._store_dmarc(inbound, attachments)
        self.uow.commit()
        log_event(log, "inbound.received", mailbox=mailbox, kind=kind, size=len(raw))
        return inbound, True

    def _store_dmarc(self, inbound: m.InboundMessage, attachments: list[Attachment]) -> dict[str, Any]:
        reports: list[dmarc.DmarcReport] = []
        try:
            for attachment in attachments:
                if not dmarc.looks_like_report(attachment.filename, attachment.content_type):
                    continue
                data = attachment.part.get_payload(decode=True) or b""
                for document in dmarc.unpack(attachment.filename, attachment.content_type, data):
                    reports.append(dmarc.parse(document))
        except dmarc.DmarcError as error:
            log_event(log, "inbound.dmarc_unreadable", logging.WARNING, error=str(error))
            return {"error": str(error)}
        rows = [
            m.DmarcReportRow(inbound_message_id=inbound.id, reporter=report.reporter, report_id=report.report_id,
                             domain=report.domain, begin_at=report.begin_at, end_at=report.end_at,
                             source_ip=record.source_ip, count=record.count, disposition=record.disposition,
                             dkim=record.dkim, spf=record.spf)
            for report in reports for record in report.records
        ]
        self.uow.inbound.add_dmarc_rows(rows)
        messages = sum(r.messages for r in reports)
        passed = sum(r.passed for r in reports)
        return {
            "reporters": sorted({r.reporter for r in reports}),
            "domains": sorted({r.domain for r in reports}),
            "messages": messages,
            "passed": passed,
            "sources": len({row.source_ip for row in rows}),
        }

    # ---- The admin inbox ----

    def list_page(self, mailbox: str, page: PageRequest, query: str | None, state: str) -> Page[m.InboundMessage]:
        return self.uow.inbound.list_page(mailbox, page, query=query, state=state)

    def unread_counts(self) -> dict[str, int]:
        counts = self.uow.inbound.unread_counts()
        return {mailbox: counts.get(mailbox, 0) for mailbox in MAILBOXES}

    def _get(self, message_id: uuid.UUID) -> m.InboundMessage:
        inbound = self.uow.inbound.get(message_id)
        if inbound is None:
            raise NotFound("Message not found.")
        return inbound

    def _raw(self, inbound: m.InboundMessage) -> bytes:
        return self.storage.read(inbound.object_key, self.settings.inbound_mail_max_bytes)

    def detail(self, auth: AdminAuth, message_id: uuid.UUID) -> InboundDetail:
        inbound = self._get(message_id)
        message = _parse(self._raw(inbound))
        headers: list[tuple[str, str]] = []
        for name in SHOWN_HEADERS:
            for value in message.get_all(name, [])[:3]:
                headers.append((name, str(value)[:2000]))
        text = _body_part(message, "plain")
        html = _body_part(message, "html")
        if text is None and html is not None:
            text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html))
            text = html_lib.unescape(re.sub(r"[ \t]+", " ", text))
        if inbound.read_at is None:
            inbound.read_at = utcnow()
        admin_audit(self.uow, auth, "admin.viewed_inbox_message", target_type="inbound_message",
                    target_id=inbound.id, mailbox=inbound.mailbox)
        self.uow.commit()
        return InboundDetail(message=inbound, headers=headers, text=(text or "")[:MAX_TEXT_CHARS],
                             has_html=html is not None, attachments=_attachments(message),
                             dmarc_rows=self.uow.inbound.dmarc_rows_for_message(inbound.id)[:500])

    def body_document(self, message_id: uuid.UUID, images: bool) -> tuple[str, str]:
        """(HTML document, Content-Security-Policy) for the sandboxed reading-pane iframe."""
        inbound = self._get(message_id)
        message = _parse(self._raw(inbound))
        html = _body_part(message, "html")
        if html is None:
            text = _body_part(message, "plain") or ""
            html = (f"<pre style=\"white-space:pre-wrap;font:14px/1.5 system-ui,sans-serif\">"
                    f"{html_lib.escape(text[:MAX_TEXT_CHARS])}</pre>")
        else:
            html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)  # the CSP blocks them anyway
        document = f"<!doctype html><meta charset=\"utf-8\"><base target=\"_blank\">{html}"
        image_sources = "data: https: http:" if images else "data:"
        csp = (f"default-src 'none'; style-src 'unsafe-inline'; img-src {image_sources}; font-src data:; "
               "frame-ancestors 'self'; form-action 'none'; sandbox allow-popups allow-popups-to-escape-sandbox")
        return document, csp

    def attachments(self, message_id: uuid.UUID) -> list[Attachment]:
        return _attachments(_parse(self._raw(self._get(message_id))))

    def attachment(self, message_id: uuid.UUID, index: int) -> tuple[str, bytes]:
        attachments = _attachments(_parse(self._raw(self._get(message_id))))
        if not 0 <= index < len(attachments):
            raise NotFound("Attachment not found.")
        item = attachments[index]
        return safe_filename(item.filename), item.part.get_payload(decode=True) or b""

    def raw(self, auth: AdminAuth, message_id: uuid.UUID) -> bytes:
        inbound = self._get(message_id)
        admin_audit(self.uow, auth, "admin.downloaded_inbox_message", target_type="inbound_message",
                    target_id=inbound.id)
        self.uow.commit()
        return self._raw(inbound)

    def mark(self, auth: AdminAuth, message_id: uuid.UUID, *, read: bool | None, archived: bool | None
             ) -> m.InboundMessage:
        inbound = self._get(message_id)
        now = utcnow()
        if read is not None:
            inbound.read_at = now if read else None
        if archived is not None:
            inbound.archived_at = now if archived else None
        self.uow.commit()
        return inbound

    # ---- Retention ----

    def delete_old(self, before: Any, limit: int = 500) -> int:
        old = self.uow.inbound.list_received_before(before, limit)
        for inbound in old:
            self.storage.delete(inbound.object_key)
            self.uow.inbound.delete(inbound)
        return len(old)
