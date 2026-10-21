"""Request and response models of the admin API (/v1/admin/*, not in the public contract)."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.api.schemas import (
    AccountOut,
    ApiKeyOut,
    AuditEventOut,
    DnsRecordOut,
    HealthMetricsOut,
    Model,
    PageOut,
    SenderIdentityOut,
)

__all__ = ["PageOut"]

NextStep = Literal["change_password", "setup_two_factor"]


# ---- Sign-in ----


class AdminLoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    code: str | None = Field(None, max_length=64, description="6-digit authenticator code, or a recovery code")


class AdminOut(Model):
    id: uuid.UUID
    email: str
    two_factor_enabled: bool
    recovery_codes_left: int
    next_step: NextStep | None = Field(description="A first sign-in must change the password, then set up 2FA.")
    last_login_at: datetime | None
    limits: "LimitsOut" = Field(description="The rates above which accounts are suspended, and Gmail's spam limits")


class AdminSessionOut(Model):
    admin: AdminOut
    expires_at: datetime
    idle_minutes: int


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class TwoFactorSetupOut(Model):
    secret: str = Field(description="For typing into the app by hand")
    otpauth_uri: str = Field(description="Shown as a QR code")


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=64)


class RecoveryCodesOut(Model):
    recovery_codes: list[str] = Field(description="Shown once. Each works one time if the phone is lost.")


# ---- Overview, deliverability, system ----


class StatCardOut(Model):
    key: str
    label: str
    explain: str
    today: int
    yesterday: int


class DayOut(Model):
    day: date
    sent: int
    delivered: int
    bounced: int
    hard_bounces: int
    complaints: int
    failed: int
    signups: int
    active_users: int
    transactional: int
    bulk: int
    bounce_rate: float
    complaint_rate: float


class CheckOut(Model):
    status: Literal["ok", "warning", "problem"]
    title: str
    detail: str


class LimitsOut(Model):
    bounce_rate: float
    complaint_rate: float
    gmail_spam_warning: float
    gmail_spam_limit: float


class OverviewOut(Model):
    cards: list[StatCardOut]
    series: list[DayOut]
    checks: list[CheckOut]
    limits: LimitsOut
    generated_at: datetime


SpamLevel = Literal["good", "warning", "bad"]


class RateWindowOut(Model):
    label: str
    delivered: int
    complaints: int
    hard_bounces: int
    complaint_rate: float
    bounce_rate: float
    spam_level: SpamLevel


class ProviderOut(Model):
    provider: str
    delivered: int
    hard_bounces: int
    soft_bounces: int
    complaints: int
    bounce_rate: float
    complaint_rate: float
    spam_level: SpamLevel


class WorstAccountOut(Model):
    account_id: uuid.UUID
    account_name: str
    owner_email: str | None
    suspended: bool
    delivered: int
    hard_bounces: int
    complaints: int
    bounce_rate: float
    complaint_rate: float


class BounceReasonOut(Model):
    status_code: str | None
    count: int
    meaning: str
    example: str | None


class DmarcReporterOut(Model):
    reporter: str
    messages: int
    dmarc_pass: int
    dkim_pass: int
    spf_pass: int
    pass_rate: float
    last_seen: datetime | None


class DmarcSourceOut(Model):
    source_ip: str
    messages: int
    dmarc_pass: int
    dkim_pass: int
    spf_pass: int
    pass_rate: float
    last_seen: datetime | None
    ours: bool
    suspicious: bool


class RateDayOut(Model):
    day: date
    delivered: int
    bounce_rate: float
    complaint_rate: float


class DeliverabilityOut(Model):
    windows: list[RateWindowOut]
    by_provider: list[ProviderOut]
    worst_accounts: list[WorstAccountOut]
    bounce_reasons: list[BounceReasonOut]
    dmarc_reporters: list[DmarcReporterOut]
    dmarc_sources: list[DmarcSourceOut]
    known_sending_ips: list[str]
    series: list[RateDayOut]
    limits: LimitsOut
    generated_at: datetime


class JobOut(Model):
    name: str
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_error: str | None
    every_seconds: int | None
    ok: bool


class QueueOut(Model):
    kind: str
    status: str
    count: int


class PlatformKeyOut(Model):
    selector: str
    status: str
    created_at: datetime
    activated_at: datetime | None


class BackupOut(Model):
    label: str
    key: str | None
    size: int | None
    at: datetime | None


class SystemOut(Model):
    jobs: list[JobOut]
    queue: list[QueueOut]
    oldest_pending_at: datetime | None
    platform_domain: str
    platform_dkim: list[PlatformKeyOut]
    backups: list[BackupOut]
    port25_open: bool | None
    checks: list[CheckOut]
    generated_at: datetime


# ---- Users and accounts ----

UserStatus = Literal["active", "blocked", "unverified", "deleted"]
AccountStatus = Literal["active", "suspended", "deleted"]


class AdminUserRowOut(Model):
    id: uuid.UUID
    email: str
    status: UserStatus
    created_at: datetime
    email_verified_at: datetime | None
    disabled_at: datetime | None
    accounts: int
    last_seen_at: datetime | None
    sent_30d: int


class UserAccountOut(Model):
    account: AccountOut
    role: str
    status: AccountStatus


class AdminUserDetailOut(Model):
    user: AdminUserRowOut
    accounts: list[UserAccountOut]
    active_sessions: int


class BlockIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    suspend_accounts: bool = Field(True, description="Also suspend sending and revoke API keys of accounts they own")


class AdminAccountRowOut(Model):
    account: AccountOut
    status: AccountStatus
    owner_email: str | None
    members: int
    verified_domains: int
    sent_today: int
    metrics: HealthMetricsOut


class MemberRowOut(Model):
    user_id: uuid.UUID
    email: str
    role: str
    status: UserStatus


class AdminDomainOut(Model):
    id: uuid.UUID
    name: str
    status: str
    bulk_eligible: bool
    verified_at: datetime | None
    last_checked_at: datetime | None
    records: list[DnsRecordOut]


class DeliveryEventOut(Model):
    type: str
    classification: str | None
    status_code: str | None
    diagnostic: str | None
    email: str = Field(description="Masked, e.g. jo***@gmail.com")
    created_at: datetime


class AdminAccountDetailOut(Model):
    account: AccountOut
    status: AccountStatus
    owner_email: str | None
    metrics: HealthMetricsOut
    latest_decision: AuditEventOut | None
    members: list[MemberRowOut]
    domains: list[AdminDomainOut]
    senders: list[SenderIdentityOut]
    api_keys: list[ApiKeyOut]
    recent_events: list[DeliveryEventOut]
    suppressions: int
    audit: list[AuditEventOut]


class DecisionIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class AdminLimitsIn(BaseModel):
    hourly_recipient_limit: int = Field(ge=0, le=1_000_000)
    daily_recipient_limit: int = Field(ge=0, le=10_000_000)
    reason: str = Field(min_length=3, max_length=1000)


# ---- Needs help ----


class IssueOut(Model):
    key: str
    type: str
    label: str
    severity: Literal["high", "medium", "low"]
    who: str
    problem: str
    how_to_fix: str
    details: list[str]
    since: datetime | None
    user_id: uuid.UUID | None
    account_id: uuid.UUID | None
    contact_email: str | None
    action: Literal["resend_verification", "reinstate", "change_limits"] | None
    state: Literal["open", "contacted", "resolved"]
    note: str | None
    state_updated_at: datetime | None
    reopened: bool


class IssueTypeOut(Model):
    type: str
    label: str
    severity: str
    count: int


class IssuesOut(Model):
    items: list[IssueOut]
    types: list[IssueTypeOut]


class IssueStateIn(BaseModel):
    key: str = Field(max_length=200)
    status: Literal["open", "contacted", "resolved"]
    note: str | None = Field(None, max_length=2000)


# ---- Inbox ----

Mailbox = Literal["support", "postmaster", "abuse", "dmarc"]


class InboxMessageOut(Model):
    id: uuid.UUID
    mailbox: Mailbox
    from_address: str
    from_name: str | None
    subject: str
    kind: Literal["plain", "dmarc_report", "complaint_report", "bounce"]
    size: int
    has_attachments: bool
    summary: dict[str, Any]
    created_at: datetime
    read_at: datetime | None
    archived_at: datetime | None


class InboxCountsOut(Model):
    unread: dict[str, int]


class HeaderOut(Model):
    name: str
    value: str


class AttachmentOut(Model):
    index: int
    filename: str
    content_type: str
    size: int


class DmarcRowOut(Model):
    reporter: str
    domain: str
    begin_at: datetime
    end_at: datetime
    source_ip: str
    count: int
    disposition: str
    dkim: str
    spf: str


class InboxMessageDetailOut(Model):
    message: InboxMessageOut
    headers: list[HeaderOut]
    text: str
    has_html: bool
    attachments: list[AttachmentOut]
    dmarc_rows: list[DmarcRowOut]


class InboxMarkIn(BaseModel):
    read: bool | None = None
    archived: bool | None = None


class InboundResultOut(Model):
    id: uuid.UUID
    created: bool
    kind: str


AdminOut.model_rebuild()
AdminSessionOut.model_rebuild()


# ---- Writing email ----


class MailOptionsOut(Model):
    from_addresses: list[str] = Field(description="Addresses an admin can send from; the first is the default")
    from_name: str
    max_recipients: int
    max_attachments: int
    max_attachment_bytes: int
    max_total_bytes: int
    blocked_extensions: list[str]


class MailAttachmentOut(Model):
    id: uuid.UUID
    filename: str
    content_type: str
    size: int


class MailSendIn(BaseModel):
    from_address: str = Field(max_length=320)
    to: list[str] = Field(min_length=1, max_length=100)
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=100_000, description="Plain text; links become clickable in the branded format")
    format: Literal["branded", "plain"] = "branded"
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    reply_to_message_id: uuid.UUID | None = Field(None, description="An inbox message this answers (threads it)")


class MailPreviewIn(BaseModel):
    subject: str = Field("", max_length=300)
    body: str = Field("", max_length=100_000)
    format: Literal["branded", "plain"] = "branded"


class SentMailOut(Model):
    id: uuid.UUID
    from_address: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    status: Literal["queued", "sent", "failed"]
    error: str | None
    created_at: datetime
    sent_at: datetime | None


class SentMailDetailOut(Model):
    mail: SentMailOut
    body: str
    format: Literal["branded", "plain"]
    sent_by: str | None
    in_reply_to_id: uuid.UUID | None
    attachments: list[MailAttachmentOut]


# ---- The admin's own login ----


class AdminListItemOut(Model):
    id: uuid.UUID
    email: str
    disabled: bool
    last_login_at: datetime | None


class PasswordIn(BaseModel):
    password: str = Field(max_length=256)


class SessionsEndedOut(Model):
    sessions: int
