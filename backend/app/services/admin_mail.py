"""Email an admin writes in the admin app – to customers or anyone else – with attachments.

* It's sent from one of the platform's own addresses (ADMIN_MAIL_FROM_MAILBOXES at the platform
  domain, e.g. support@mailvender.com), so it's signed with the platform DKIM key, and replies (and
  bounces – the From address is also the envelope sender) come back to that address.
* Attachments are uploaded one file per request (each under ADMIN_MAIL_MAX_ATTACHMENT_BYTES, all
  together under ADMIN_MAIL_MAX_TOTAL_BYTES) into private storage, then attached when the email is
  sent. Program files mail providers refuse (.exe, .js, …) are rejected up front.
* Sending queues an `admin_mail` outbox event; the worker builds the MIME message and hands it to
  Postfix like any other email. Every send is in the admin audit log; the email itself (body,
  recipients, attachments) is kept as the Sent folder until the retention job removes it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from html import escape
from typing import Literal

from app import models as m
from app.config import get_settings
from app.errors import ApiError, FieldError, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services import emails
from app.services.admin_auth import AdminAuth, admin_audit
from app.services.common import try_normalize_email
from app.services.inbound import InboundService, safe_filename
from app.services.mta import MailTransport, PermanentDeliveryError
from app.services.sending import MIME_POLICY
from app.services.storage import ObjectStorage

log = get_logger("admin_mail")

MAX_ATTACHMENTS = 10
MAX_BODY_CHARS = 100_000
# Mail providers (Gmail, Outlook) refuse these outright, so the email would bounce.
BLOCKED_EXTENSIONS = {
    "ade", "adp", "apk", "appx", "bat", "cab", "chm", "cmd", "com", "cpl", "dll", "dmg", "exe", "hta", "ins",
    "isp", "iso", "jar", "js", "jse", "lib", "lnk", "mde", "msc", "msi", "msix", "msp", "mst", "nsh", "pif",
    "ps1", "scr", "sct", "shb", "sys", "vb", "vbe", "vbs", "vxd", "wsc", "wsf", "wsh",
}

Format = Literal["branded", "plain"]


@dataclass
class Draft:
    from_address: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    format: Format
    attachment_ids: list[uuid.UUID]
    reply_to_message_id: uuid.UUID | None = None


@dataclass
class MailDetail:
    mail: m.AdminMail
    attachments: list[m.AdminMailAttachment]
    sent_by: str | None


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB".replace(".0 MB", " MB")


class AdminMailService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage):
        self.uow = uow
        self.storage = storage
        self.settings = get_settings()

    # ---- Attachments ----

    def upload(self, auth: AdminAuth, filename: str, content_type: str, data: bytes) -> m.AdminMailAttachment:
        name = safe_filename(filename or "attachment")
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension in BLOCKED_EXTENSIONS:
            raise ApiError(f"Mail providers refuse .{extension} files, so the email would bounce. Zip it, or "
                           "share a link instead.", code="attachment_blocked", status_code=422)
        if not data:
            raise ApiError("That file is empty.", code="attachment_empty", status_code=422)
        limit = self.settings.admin_mail_max_attachment_bytes
        if len(data) > limit:
            raise ApiError(f"Each file can be up to {_mb(limit)}. For bigger files, share a link instead.",
                           code="attachment_too_large", status_code=413)
        content_type = content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"
        if "/" not in content_type or len(content_type) > 100:
            content_type = "application/octet-stream"
        key = f"{self.settings.aws_s3_prefix}admin-mail/{utcnow():%Y/%m}/{uuid.uuid4()}/{name}"
        self.storage.put(key, data, content_type)
        attachment = self.uow.admin_mail.add(m.AdminMailAttachment(
            admin_id=auth.admin.id, filename=name, content_type=content_type, size=len(data), object_key=key))
        self.uow.commit()
        return attachment

    def copy_from_inbox(self, auth: AdminAuth, message_id: uuid.UUID) -> list[m.AdminMailAttachment]:
        """Forwarding: the received message's attachments, ready to attach to a new email."""
        inbound = InboundService(self.uow, self.storage)
        copies = []
        for item in inbound.attachments(message_id):
            data = item.part.get_payload(decode=True) or b""
            if not data or len(data) > self.settings.admin_mail_max_attachment_bytes:
                continue  # too big to send again; the admin can still download it
            copies.append(self.upload(auth, item.filename, item.content_type, data))
        return copies

    def discard(self, auth: AdminAuth, attachment_id: uuid.UUID) -> None:
        attachment = self.uow.admin_mail.get_attachment(attachment_id)
        if attachment is None or attachment.mail_id is not None:
            raise NotFound("Attachment not found.")
        self.storage.delete(attachment.object_key)
        self.uow.admin_mail.delete(attachment)
        self.uow.commit()

    # ---- Sending ----

    def _recipients(self, field: str, values: list[str], errors: list[FieldError]) -> list[str]:
        found = []
        for index, raw in enumerate(values):
            address = try_normalize_email(raw)
            if address is None:
                errors.append(field_error(f"{field}[{index}]", f"“{raw[:80]}” isn't an email address.",
                                          "invalid_email"))
            elif address not in found:
                found.append(address)
        return found

    def send(self, auth: AdminAuth, draft: Draft) -> m.AdminMail:
        errors: list[FieldError] = []
        from_address = draft.from_address.strip().lower()
        if from_address not in self.settings.admin_mail_from_list:
            errors.append(field_error("from_address", "Choose one of our sending addresses.", "invalid"))
        to = self._recipients("to", draft.to, errors)
        cc = [x for x in self._recipients("cc", draft.cc, errors) if x not in to]
        bcc = [x for x in self._recipients("bcc", draft.bcc, errors) if x not in to and x not in cc]
        if not to:
            errors.append(field_error("to", "Add at least one recipient.", "required"))
        total = len(to) + len(cc) + len(bcc)
        if total > self.settings.admin_mail_max_recipients:
            errors.append(field_error("to", f"Up to {self.settings.admin_mail_max_recipients} recipients per "
                                            "email.", "too_many"))
        subject = " ".join(draft.subject.split())
        if not subject:
            errors.append(field_error("subject", "Add a subject.", "required"))
        if not draft.body.strip():
            errors.append(field_error("body", "Write a message.", "required"))
        if len(draft.body) > MAX_BODY_CHARS:
            errors.append(field_error("body", "The message is too long.", "too_long"))
        attachments = []
        for attachment_id in dict.fromkeys(draft.attachment_ids):
            attachment = self.uow.admin_mail.get_attachment(attachment_id)
            if attachment is None or attachment.mail_id is not None:
                errors.append(field_error("attachment_ids", "An attachment is missing; upload it again.",
                                          "not_found"))
            else:
                attachments.append(attachment)
        if len(attachments) > MAX_ATTACHMENTS:
            errors.append(field_error("attachment_ids", f"Up to {MAX_ATTACHMENTS} files per email.", "too_many"))
        if sum(a.size for a in attachments) > self.settings.admin_mail_max_total_bytes:
            errors.append(field_error("attachment_ids", "All files together can be up to "
                                      f"{_mb(self.settings.admin_mail_max_total_bytes)}.", "too_large"))
        reply_to = None
        if draft.reply_to_message_id:
            reply_to = self.uow.inbound.get(draft.reply_to_message_id)
            if reply_to is None:
                errors.append(field_error("reply_to_message_id", "That message no longer exists.", "not_found"))
        if errors:
            raise ValidationFailed(errors, "Nothing was sent – fix the listed fields and try again.")

        mail = self.uow.admin_mail.add(m.AdminMail(
            admin_id=auth.admin.id, from_address=from_address, to=to, cc=cc, bcc=bcc, subject=subject[:998],
            body=draft.body, format=draft.format,
            message_id_header=f"<{uuid.uuid4()}@{self.settings.platform_domain}>",
            in_reply_to_id=reply_to.id if reply_to else None,
            in_reply_to_header=reply_to.message_id_header if reply_to else None, status="queued",
        ))
        for attachment in attachments:
            attachment.mail_id = mail.id
        self.uow.outbox.add(m.OutboxEvent(kind="admin_mail", payload={"mail_id": str(mail.id)},
                                          available_at=utcnow()))
        admin_audit(self.uow, auth, "admin.mail_sent", target_type="admin_mail", target_id=mail.id,
                    subject=subject[:200], recipients=total, attachments=len(attachments),
                    reply_to=str(reply_to.id) if reply_to else None)
        self.uow.commit()
        log_event(log, "admin_mail.queued", recipients=total, attachments=len(attachments))
        return mail

    def mime(self, mail: m.AdminMail) -> EmailMessage:
        message = EmailMessage(policy=MIME_POLICY)
        message["From"] = formataddr((self.settings.admin_mail_from_name, mail.from_address))
        message["To"] = ", ".join(mail.to)
        if mail.cc:
            message["Cc"] = ", ".join(mail.cc)
        message["Reply-To"] = mail.from_address
        message["Subject"] = mail.subject
        message["Date"] = format_datetime(mail.created_at)
        message["Message-ID"] = mail.message_id_header
        if mail.in_reply_to_header:
            message["In-Reply-To"] = mail.in_reply_to_header
            message["References"] = mail.in_reply_to_header
        rendered = emails.admin_message(mail.subject, mail.body)
        message.set_content(rendered.text)
        if mail.format == "branded":
            message.add_alternative(rendered.html, subtype="html")
        for attachment in self.uow.admin_mail.attachments_for(mail.id):
            data = self.storage.read(attachment.object_key, attachment.size + 1)
            maintype, _, subtype = attachment.content_type.partition("/")
            message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                                   filename=attachment.filename)
        return message

    def deliver(self, mail_id: uuid.UUID, transport: MailTransport) -> None:
        """Worker. Temporary failures raise (the outbox retries); permanent ones mark the email failed."""
        mail = self.uow.admin_mail.get(mail_id)
        if mail is None or mail.status != "queued":
            return
        try:
            transport.send(mail.from_address, [*mail.to, *mail.cc, *mail.bcc], self.mime(mail).as_bytes())
        except PermanentDeliveryError as error:
            mail.status, mail.error = "failed", str(error)[:1000]
            log_event(log, "admin_mail.failed", mail_id=str(mail.id), error=str(error))
            return
        mail.status, mail.sent_at, mail.error = "sent", utcnow(), None
        log_event(log, "admin_mail.sent", mail_id=str(mail.id))

    def mark_failed(self, mail_id: uuid.UUID, error: str) -> None:
        mail = self.uow.admin_mail.get(mail_id)
        if mail is not None and mail.status == "queued":
            mail.status, mail.error = "failed", error[:1000]
            log_event(log, "admin_mail.failed", mail_id=str(mail.id), error=error)

    def preview(self, subject: str, body: str, format: Format) -> str:
        """The HTML the recipient will see (for a sandboxed iframe)."""
        if format == "branded":
            return emails.admin_message(" ".join(subject.split()) or "(no subject)", body).html
        return (f"<pre style=\"white-space:pre-wrap;font:14px/1.5 system-ui,sans-serif\">"
                f"{escape(body[:MAX_BODY_CHARS])}</pre>")

    # ---- The Sent folder ----

    def list_page(self, page: PageRequest, query: str | None) -> Page[m.AdminMail]:
        return self.uow.admin_mail.list_page(page, query=query)

    def detail(self, mail_id: uuid.UUID) -> MailDetail:
        mail = self.uow.admin_mail.get(mail_id)
        if mail is None:
            raise NotFound("Email not found.")
        admin = self.uow.admins.get(mail.admin_id)
        return MailDetail(mail=mail, attachments=self.uow.admin_mail.attachments_for(mail.id),
                          sent_by=admin.email if admin else None)

    def attachment(self, attachment_id: uuid.UUID) -> tuple[str, bytes]:
        attachment = self.uow.admin_mail.get_attachment(attachment_id)
        if attachment is None:
            raise NotFound("Attachment not found.")
        return attachment.filename, self.storage.read(attachment.object_key, attachment.size + 1)

    # ---- Retention ----

    def delete_old(self, sent_before: datetime, uploads_before: datetime, limit: int = 500) -> int:
        removed = 0
        for attachment in self.uow.admin_mail.unattached_before(uploads_before, limit):
            self.storage.delete(attachment.object_key)
            self.uow.admin_mail.delete(attachment)
            removed += 1
        for mail in self.uow.admin_mail.sent_before(sent_before, limit):
            for attachment in self.uow.admin_mail.attachments_for(mail.id):
                self.storage.delete(attachment.object_key)
            self.uow.admin_mail.delete(mail)
            removed += 1
        return removed
