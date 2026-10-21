"""Database tables. Every account-owned table has an `account_id` column.

Identifiers are UUIDs and every timestamp is stored as `timestamptz` (UTC).
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _now() -> datetime:
    return datetime.now(UTC)


def _created() -> Mapped[datetime]:
    return mapped_column(default=_now, server_default=func.now(), nullable=False)


def _account_fk(nullable: bool = False) -> Mapped[Any]:
    return mapped_column(ForeignKey("accounts.id"), nullable=nullable, index=True)


# ---- Identity ----


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None]
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    default_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id", use_alter=True))
    created_at: Mapped[datetime] = _created()
    disabled_at: Mapped[datetime | None]
    # Set when the person deletes their login: email and password are replaced, the row stays for history.
    deleted_at: Mapped[datetime | None]
    # Optional two-factor sign-in (authenticator app), like admins'.
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None]
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # a code is accepted once
    recovery_code_hashes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))

    @property
    def two_factor_enabled(self) -> bool:
        return self.totp_enabled_at is not None

    @property
    def recovery_codes_left(self) -> int:
        return len(self.recovery_code_hashes or [])


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(64))


class EmailToken(Base):
    """Single-use, hashed, expiring tokens for email verification and password reset."""

    __tablename__ = "email_tokens"
    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


# ---- Accounts ----


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))  # personal | team
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    physical_address: Mapped[str | None] = mapped_column(Text)
    hourly_recipient_limit: Mapped[int] = mapped_column(Integer)
    daily_recipient_limit: Mapped[int] = mapped_column(Integer)
    sending_suspended_at: Mapped[datetime | None]
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    deletion_requested_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("account_id", "user_id"),)
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # owner | member
    created_at: Mapped[datetime] = _created()


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None]


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(32))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


# ---- Sending domains ----


class Domain(Base):
    __tablename__ = "domains"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    name: Mapped[str] = mapped_column(String(253))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | verified | disabled
    return_path_host: Mapped[str] = mapped_column(String(253))
    record_status: Mapped[dict[str, Any]] = mapped_column(default=dict)
    bulk_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[datetime | None]
    verified_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


# A domain name can be registered by one live (non-disabled-by-deletion) account at a time.
Index(
    "uq_domains_live_name",
    Domain.name,
    unique=True,
    postgresql_where=text("disabled_at IS NULL"),
)


class DkimKey(Base):
    __tablename__ = "dkim_keys"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"), index=True)
    selector: Mapped[str] = mapped_column(String(63), unique=True)
    private_key_encrypted: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # pending | active | retired
    created_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]


class PlatformDkimKey(Base):
    """DKIM keys for Mailvender's own domain (system emails). Not account-owned: `python -m app.cli platform-dkim`."""

    __tablename__ = "platform_dkim_keys"
    id: Mapped[uuid.UUID] = _id()
    domain: Mapped[str] = mapped_column(String(253), index=True)
    selector: Mapped[str] = mapped_column(String(63), unique=True)
    private_key_encrypted: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # pending | active | retired
    created_at: Mapped[datetime] = _created()
    activated_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]


class SenderIdentity(Base):
    __tablename__ = "sender_identities"
    __table_args__ = (UniqueConstraint("account_id", "email"),)
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | verified | disabled
    verified_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


# ---- Assets ----


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    status: Mapped[str] = mapped_column(String(16))  # pending | ready | deleted
    upload_key: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()
    upload_expires_at: Mapped[datetime]
    finalized_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]


# ---- Messages ----


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key"),)
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"))
    sender_identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sender_identities.id"))
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text)
    text_body: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(16))  # transactional | bulk
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    created_at: Mapped[datetime] = _created()
    content_purged_at: Mapped[datetime | None]


class MessageRecipient(Base):
    __tablename__ = "message_recipients"
    __table_args__ = (
        Index("ix_message_recipients_account_created", "account_id", "created_at"),
        Index("ix_message_recipients_created", "created_at"),  # platform-wide admin charts
        Index("ix_message_recipients_accepted", "accepted_at"),
    )
    id: Mapped[uuid.UUID] = _id()
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), index=True)
    account_id: Mapped[uuid.UUID] = _account_fk()
    domain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("domains.id"))
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    accepted_at: Mapped[datetime | None]
    failed_at: Mapped[datetime | None]
    bounced_at: Mapped[datetime | None]
    complained_at: Mapped[datetime | None]


class OutboxEvent(Base):
    """Transactional outbox: written with the change, consumed by the worker."""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_pending", "status", "available_at"),)
    id: Mapped[uuid.UUID] = _id()
    kind: Mapped[str] = mapped_column(String(32))  # deliver_recipient | system_email | account_export | ...
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done | dead | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    processed_at: Mapped[datetime | None]


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message_recipients.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))  # accepted | temporary_failure | permanent_failure
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class DeliveryEvent(Base):
    """Recipient-level outcomes reported after the MTA accepted a message."""

    __tablename__ = "delivery_events"
    __table_args__ = (
        Index("ix_delivery_events_account_created", "account_id", "created_at"),
        Index("ix_delivery_events_created", "created_at"),
    )
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message_recipients.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    type: Mapped[str] = mapped_column(String(16))  # bounce | complaint
    classification: Mapped[str | None] = mapped_column(String(16))  # hard | soft
    status_code: Mapped[str | None] = mapped_column(String(16))
    diagnostic: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("account_id", "email"),)
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    email: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(32))  # unsubscribe | hard_bounce | soft_bounce_threshold | complaint | manual
    source: Mapped[str] = mapped_column(String(64))
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message_recipients.id"))
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Immutable (a database trigger rejects UPDATE and DELETE)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_account_created", "account_id", "created_at"),
        Index("ix_audit_actor_created", "actor_type", "created_at"),
    )
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    actor_type: Mapped[str] = mapped_column(String(16))  # user | api_key | system | operator | public
    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


# ---- Cloud sync ----


class SyncRecord(Base):
    __tablename__ = "sync_records"
    __table_args__ = (
        Index("ix_sync_records_account_seq", "account_id", "seq"),
        Index("ix_sync_records_parent", "account_id", "collection", "parent_id"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    collection: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    parent_id: Mapped[uuid.UUID | None]
    revision: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Collections with large content (datasets) keep it in object storage; `data` is then a small summary.
    object_key: Mapped[str | None] = mapped_column(Text)
    object_size: Mapped[int | None] = mapped_column(BigInteger)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    seq: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime]
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class SyncUpload(Base):
    """A presigned content upload for a sync record, until a write commits it (or it expires)."""

    __tablename__ = "sync_uploads"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    collection: Mapped[str] = mapped_column(String(32))
    record_id: Mapped[uuid.UUID]
    object_key: Mapped[str] = mapped_column(Text)
    max_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(index=True)


# ---- Data lifecycle ----


class AccountExport(Base):
    __tablename__ = "account_exports"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16))  # pending | ready | failed | expired
    object_key: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]


class JobRun(Base):
    """Last run of each scheduled job (for monitoring)."""

    __tablename__ = "job_runs"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_started_at: Mapped[datetime | None]
    last_finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)



class UserActivityDay(Base):
    """One row per user per UTC day they used the app (for the admin's "active users" chart)."""

    __tablename__ = "user_activity_days"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True, index=True)


# ---- Admin (Mailvender staff – separate from customer users; see docs/ADMIN_SETUP.md) ----


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None]
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # a code is accepted once
    recovery_code_hashes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[uuid.UUID] = _id()
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class AdminIssueState(Base):
    """An admin's note on a "Needs help" problem (contacted / resolved). Problems themselves are computed."""

    __tablename__ = "admin_issue_states"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)  # <type>:<subject id>
    status: Mapped[str] = mapped_column(String(16))  # contacted | resolved
    note: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id"))
    updated_at: Mapped[datetime] = _created()


class InboundMessage(Base):
    """Mail received at postmaster@ / abuse@ / dmarc@ (raw message in object storage)."""

    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("mailbox", "message_id_header"),
        Index("ix_inbound_messages_mailbox_created", "mailbox", "created_at"),
    )
    id: Mapped[uuid.UUID] = _id()
    mailbox: Mapped[str] = mapped_column(String(16))  # postmaster | abuse | dmarc | support
    message_id_header: Mapped[str | None] = mapped_column(String(500))
    from_address: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(24))  # plain | dmarc_report | complaint_report | bounce
    size: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()  # received
    read_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]


class DmarcReportRow(Base):
    """One <record> of a DMARC aggregate report: what a receiver saw from one source IP."""

    __tablename__ = "dmarc_report_rows"
    id: Mapped[uuid.UUID] = _id()
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id"), index=True)
    reporter: Mapped[str] = mapped_column(String(200))
    report_id: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(253))
    begin_at: Mapped[datetime]
    end_at: Mapped[datetime]
    source_ip: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    disposition: Mapped[str] = mapped_column(String(16))  # none | quarantine | reject
    dkim: Mapped[str] = mapped_column(String(16))  # DMARC-aligned result: pass | fail
    spf: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = _created()


class AdminMail(Base):
    """An email an admin wrote and sent from one of the platform's addresses (the Sent folder)."""

    __tablename__ = "admin_mails"
    __table_args__ = (Index("ix_admin_mails_created", "created_at"),)
    id: Mapped[uuid.UUID] = _id()
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id"))
    from_address: Mapped[str] = mapped_column(String(320))
    to: Mapped[list[str]] = mapped_column(JSONB, default=list)
    cc: Mapped[list[str]] = mapped_column(JSONB, default=list)
    bcc: Mapped[list[str]] = mapped_column(JSONB, default=list)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)  # what the admin typed (plain text)
    format: Mapped[str] = mapped_column(String(16))  # branded | plain
    message_id_header: Mapped[str] = mapped_column(String(500))
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    in_reply_to_header: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | sent | failed
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None]


class AdminMailAttachment(Base):
    """A file uploaded for an admin email; unattached ones are removed by the retention job after a day."""

    __tablename__ = "admin_mail_attachments"
    id: Mapped[uuid.UUID] = _id()
    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admin_users.id"))
    mail_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_mails.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class SystemState(Base):
    """Small bits of platform state the worker keeps between runs (blocklist results, alerts sent)."""

    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime]



class WebhookEndpoint(Base):
    """A customer's URL that receives signed event notifications (services/webhooks.py)."""

    __tablename__ = "webhook_endpoints"
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(200))
    events: Mapped[list[str]] = mapped_column(JSONB, default=list)
    secret_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_success_at: Mapped[datetime | None]
    last_failure_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class WebhookDelivery(Base):
    """One event sent (or being sent) to one endpoint, with the last attempt's outcome."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_endpoint_created", "endpoint_id", "created_at"),)
    id: Mapped[uuid.UUID] = _id()
    account_id: Mapped[uuid.UUID] = _account_fk()
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhook_endpoints.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID]
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


# ---- Starter templates ----


class StarterTemplate(Base):
    """A ready-made email offered in the studio's New page gallery (global, no account).

    Written in the studio (`mailvender/lib/editor/templates/`), exported as JSON to
    `backend/starter_templates/` and synced into this table when the API starts (services/templates.py).
    """

    __tablename__ = "starter_templates"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    doc: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sort_order: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())
