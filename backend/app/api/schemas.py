"""Request and response models (the published API contract)."""

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageOut(Model, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(None, description="Pass as `cursor` to get the next page; null on the last page.")


class Accepted(Model):
    message: str


# ---- Auth ----


class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class EmailIn(BaseModel):
    email: str = Field(max_length=320)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    code: str | None = Field(None, max_length=32, description="Authenticator or recovery code, once two-factor "
                                                               "sign-in is on (401 `totp_required` without it)")


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(max_length=256)


class UserOut(Model):
    id: uuid.UUID
    email: str
    email_verified_at: datetime | None
    is_operator: bool
    default_account_id: uuid.UUID | None
    created_at: datetime
    two_factor_enabled: bool
    recovery_codes_left: int


class TwoFactorSetupOut(Model):
    secret: str
    otpauth_uri: str


class TwoFactorCodeIn(BaseModel):
    code: str = Field(max_length=32)


class TwoFactorDisableIn(BaseModel):
    password: str = Field(max_length=256)
    code: str = Field(max_length=32, description="A current authenticator code or a recovery code")


class PasswordConfirmIn(BaseModel):
    password: str = Field(max_length=256)


class RecoveryCodesOut(Model):
    recovery_codes: list[str]


class AccountOut(Model):
    id: uuid.UUID
    name: str
    kind: Literal["personal", "team"]
    physical_address: str | None
    hourly_recipient_limit: int
    daily_recipient_limit: int
    sending_suspended_at: datetime | None
    suspension_reason: str | None
    created_at: datetime


class MembershipAccountOut(AccountOut):
    role: Literal["owner", "member"]


class DeletedAccountOut(Model):
    id: uuid.UUID
    name: str
    kind: Literal["personal", "team"]
    deletion_requested_at: datetime
    purge_on: datetime = Field(description="When it's erased for good; it can be restored until then")


class MeOut(Model):
    user: UserOut
    accounts: list[MembershipAccountOut]
    deleted_accounts: list[DeletedAccountOut] = Field(
        default_factory=list, description="Accounts you own that are deleted but can still be restored")


class SessionOut(Model):
    user: UserOut
    accounts: list[MembershipAccountOut]
    deleted_accounts: list[DeletedAccountOut] = Field(default_factory=list)
    expires_at: datetime


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class SessionInfoOut(Model):
    """A device where the user is signed in."""

    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    user_agent: str | None
    ip_address: str | None
    current: bool = Field(False, description="The session making this request")


class RevokedOut(BaseModel):
    revoked: int


class UserDeletionIn(BaseModel):
    password: str = Field(max_length=256, description="Your current password, to confirm")


# ---- Accounts ----


class AccountCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AccountUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    physical_address: str | None = Field(None, max_length=500, description="Postal address shown in bulk email.")


class MemberOut(Model):
    user_id: uuid.UUID
    email: str
    role: Literal["owner", "member"]
    created_at: datetime


class TransferIn(BaseModel):
    user_id: uuid.UUID


class InvitationIn(BaseModel):
    email: str = Field(max_length=320)


class InvitationOut(Model):
    id: uuid.UUID
    email: str
    role: str
    created_at: datetime
    expires_at: datetime


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ApiKeyOut(Model):
    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = Field(description="The secret. Shown only in this response – store it now.")


class AuditEventOut(Model):
    id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    reason: str | None
    data: dict[str, Any]
    created_at: datetime


# ---- Domains ----


class DomainIn(BaseModel):
    name: str = Field(max_length=253)


class DnsRecordOut(Model):
    key: str = Field(description="dkim | dkim_next | return_path_mx | spf | dmarc")
    purpose: str
    type: Literal["TXT", "MX"]
    name: str
    value: str
    required: bool
    status: Literal["verified", "missing", "mismatched", "unchecked"]
    found: list[str]
    note: str | None


class DomainOut(Model):
    id: uuid.UUID
    name: str
    status: Literal["pending", "verified", "disabled"]
    bulk_eligible: bool
    return_path_host: str
    dkim_selector: str | None
    records: list[DnsRecordOut]
    last_checked_at: datetime | None
    verified_at: datetime | None
    created_at: datetime


class SenderIdentityIn(BaseModel):
    domain_id: uuid.UUID
    email: str = Field(max_length=320)
    display_name: str | None = Field(None, max_length=200)


class SenderIdentityOut(Model):
    id: uuid.UUID
    domain_id: uuid.UUID
    email: str
    display_name: str | None
    status: Literal["pending", "verified", "disabled"]
    verified_at: datetime | None
    disabled_at: datetime | None
    created_at: datetime


# ---- Assets ----


class UploadIn(BaseModel):
    filename: str = Field(max_length=255)
    content_type: str = Field(max_length=64)
    size: int = Field(gt=0, description="Bytes. The upload is limited to this size.")


class PresignedUploadOut(Model):
    url: str
    fields: dict[str, str] = Field(description="Send as multipart form fields, then the file as `file` (last).")
    expires_in: int


class AssetOut(Model):
    id: uuid.UUID
    status: Literal["pending", "ready", "deleted"]
    original_filename: str
    mime_type: str
    byte_size: int | None
    width: int | None
    height: int | None
    public_url: str | None
    created_at: datetime


class UploadOut(Model):
    asset: AssetOut
    upload: PresignedUploadOut


# ---- Messages ----


class SendIn(BaseModel):
    from_: str = Field(alias="from", max_length=320, description="An approved sender identity of the account.")
    from_name: str | None = Field(None, max_length=200)
    reply_to: str | None = Field(None, max_length=320)
    to: list[str] = Field(description="Recipients; each gets an individual copy.")
    subject: str = Field(max_length=998)
    html: str
    text: str | None = Field(None, description="Plain-text alternative. Required for bulk; derived if omitted.")
    category: Literal["transactional", "bulk"] = Field(
        "transactional",
        description="Bulk (marketing, newsletters, mail merge) needs a text part, {{unsubscribe_url}}, a "
        "postal address and a DMARC-aligned domain; it gets List-Unsubscribe headers.",
    )
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


RecipientStatus = Literal["queued", "accepted_by_mta", "failed", "bounced", "complained", "suppressed"]


class RecipientOut(Model):
    id: uuid.UUID
    email: str
    status: RecipientStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None
    failed_at: datetime | None
    bounced_at: datetime | None
    complained_at: datetime | None


class MessageOut(Model):
    id: uuid.UUID
    from_email: str
    from_name: str | None
    subject: str
    category: Literal["transactional", "bulk"]
    metadata: dict[str, str]
    created_at: datetime
    recipients: list[RecipientOut]


class MessageSummaryOut(Model):
    id: uuid.UUID
    from_email: str
    subject: str
    category: str
    created_at: datetime


# ---- Suppressions ----


class SuppressionIn(BaseModel):
    email: str = Field(max_length=320)


class SuppressionOut(Model):
    id: uuid.UUID
    email: str
    reason: Literal["unsubscribe", "hard_bounce", "soft_bounce_threshold", "complaint", "manual"]
    source: str
    created_at: datetime


class FeedbackResultOut(Model):
    correlated: bool
    classification: str | None
    suppressed: bool


# ---- Operator ----


class HealthMetricsOut(Model):
    window_days: int
    accepted: int
    hard_bounces: int
    soft_bounces: int
    complaints: int
    bounce_rate: float
    complaint_rate: float
    sent_last_hour: int
    sent_last_day: int


class AccountHealthOut(Model):
    account: AccountOut
    status: str
    metrics: HealthMetricsOut
    latest_decision: AuditEventOut | None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class LimitsIn(BaseModel):
    hourly_recipient_limit: int = Field(ge=0)
    daily_recipient_limit: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)


# ---- Sync ----

Collection = Literal["pages", "workspace", "saved_pages", "components", "brand", "datasets", "versions"]


class SyncRecordOut(Model):
    collection: Collection
    id: uuid.UUID
    parent_id: uuid.UUID | None
    revision: int
    deleted: bool
    data: dict[str, Any] | None
    content_url: str | None = Field(None, description="Datasets: short-lived URL of the full content (JSON).")
    content_size: int | None = Field(None, validation_alias="object_size", description="Bytes of the content.")
    seq: int
    created_at: datetime
    updated_at: datetime
    updated_by: uuid.UUID | None


class SyncPutIn(BaseModel):
    expected_revision: int = Field(ge=0, description="The revision you last saw; 0 to create.")
    parent_id: uuid.UUID | None = Field(None, description="For versions: the page's id.")
    data: dict[str, Any] = Field(description="The record; for datasets only a small summary (max 64 KB).")
    content_upload_id: uuid.UUID | None = Field(
        None, description="Datasets (required): the upload holding the full content, from `.../content-uploads`.")


class ContentUploadIn(BaseModel):
    size: int = Field(gt=0, description="Bytes of the JSON content. The upload is limited to this size.")


class ContentUploadOut(Model):
    upload_id: uuid.UUID
    upload: PresignedUploadOut


class ClientEventIn(BaseModel):
    kind: Literal["sync_stuck", "sync_error"]
    status: str = Field(max_length=32)
    pending: int = Field(ge=0, le=1_000_000)
    seconds: int = Field(ge=0, le=10_000_000, description="How long the problem has lasted.")
    detail: str | None = Field(None, max_length=500)


class SnapshotOut(Model):
    records: list[SyncRecordOut]
    next_cursor: str | None
    watermark: int = Field(description="Fetch /v1/sync/changes?since=<watermark> after the last snapshot page.")


class ChangesOut(Model):
    records: list[SyncRecordOut]
    next_since: int
    has_more: bool


# ---- Lifecycle ----


class ExportOut(Model):
    id: uuid.UUID
    status: Literal["pending", "ready", "failed", "expired"]
    byte_size: int | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None


class ExportLinkOut(Model):
    url: str = Field(description="Downloads the ZIP directly from storage")
    expires_in: int = Field(description="Seconds the link works for")


class DeletionIn(BaseModel):
    confirm_name: str = Field(description="The account's current name.")


# ---- Webhooks ----


class WebhookEventTypeOut(Model):
    type: str
    description: str


class WebhookEndpointOut(Model):
    id: uuid.UUID
    url: str
    description: str | None
    events: list[str]
    enabled: bool
    disabled_reason: str | None
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    created_at: datetime


class WebhookSecretOut(Model):
    endpoint: WebhookEndpointOut
    secret: str = Field(description="Signs every request (`Mailvender-Signature`). Shown only now – store it safely.")


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000, description="https URL on the public internet")
    events: list[str] = Field(min_length=1, max_length=20, description="See GET /v1/webhooks/events")
    description: str | None = Field(None, max_length=200)


class WebhookUpdateIn(BaseModel):
    url: str | None = Field(None, max_length=2000)
    events: list[str] | None = Field(None, min_length=1, max_length=20)
    description: str | None = Field(None, max_length=200)
    enabled: bool | None = None


class WebhookDeliveryOut(Model):
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: Literal["pending", "delivered", "failed"]
    attempts: int
    response_status: int | None
    response_body: str | None = Field(description="The first 1 KB of your server's answer")
    error: str | None
    next_attempt_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    payload: dict[str, Any]


# ---- Starter templates ----


class StarterTemplateSummaryOut(Model):
    id: str
    name: str
    category: str
    description: str
    tags: list[str]
    version: str = Field(description="Changes whenever the template does: cache its document by it.")


class StarterTemplateOut(StarterTemplateSummaryOut):
    doc: dict[str, Any] = Field(description="The email document (the studio's EmailDoc, version 3).")


class StarterTemplatePageOut(Model):
    items: list[StarterTemplateSummaryOut]
    next_cursor: str | None = Field(None, description="Pass as `cursor` to get the next page; null on the last page.")
    catalog_version: str = Field(description="Changes whenever any template does.")


class TemplateCategoryOut(Model):
    name: str
    count: int


class TemplateCategoriesOut(Model):
    catalog_version: str = Field(description="Changes whenever any template does: cached pages are stale.")
    total: int
    categories: list[TemplateCategoryOut]
