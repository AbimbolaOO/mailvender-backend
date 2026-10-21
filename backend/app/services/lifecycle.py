"""Account export, deletion and restore; personal data export and login deletion; retention.

See docs/RETENTION.md.

* Deleting an account (workspace) stops it at once – keys and sessions revoked, domains and senders
  disabled, pending sends failed – but keeps its data for RETENTION_DELETED_ACCOUNT_DAYS, during which
  an owner can restore it. The retention job then erases images, workspace data and memberships.
* Deleting a login erases the person: accounts they alone own are deleted (as above), they leave every
  other account, and their email and password are replaced. Refused while they own an account that
  has other members – they transfer it or delete it first.
"""

import io
import json
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Conflict, Forbidden, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import hash_password, new_token, utcnow, verify_password
from app.services import emails
from app.services.admin_mail import AdminMailService
from app.services.assets import AssetService
from app.services.common import AccountContext, Actor, app_url, audit, enqueue_system_email
from app.services.inbound import InboundService, safe_filename
from app.services.storage import ObjectStorage
from app.services.sync import SyncService

log = get_logger("lifecycle")

EXPORT_MESSAGE_LIMIT = 50_000
EXPORT_LINK_SECONDS = 300
PERSONAL_ACTIVITY_LIMIT = 10_000


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row(entity: Any, fields: list[str]) -> dict[str, Any]:
    return {name: _iso(getattr(entity, name)) if not isinstance(getattr(entity, name), uuid.UUID)
            else str(getattr(entity, name)) for name in fields}


def purge_date(account: m.Account) -> datetime:
    """When a deleted account is erased for good (it can be restored until then)."""
    assert account.deletion_requested_at
    return account.deletion_requested_at + timedelta(days=get_settings().retention_deleted_account_days)


def deleted_accounts(uow: UnitOfWork, user: m.User) -> list[m.Account]:
    """Accounts this person owns that are deleted but can still be restored."""
    return [account for account, membership in uow.accounts.list_memberships_for_user(user.id)
            if account.status == "deleted" and membership.role == "owner"]


@dataclass
class RetentionReport:
    message_content_purged: int = 0
    messages_deleted: int = 0
    delivery_events_deleted: int = 0
    outbox_deleted: int = 0
    sessions_deleted: int = 0
    tokens_deleted: int = 0
    rate_counters_deleted: int = 0
    dkim_keys_deleted: int = 0
    exports_expired: int = 0
    accounts_purged: int = 0
    uploads_cleaned: int = 0
    inbound_deleted: int = 0
    dmarc_rows_deleted: int = 0
    admin_sessions_deleted: int = 0
    admin_mail_deleted: int = 0


class LifecycleService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage):
        self.uow = uow
        self.storage = storage
        self.settings = get_settings()

    # ---- Export ----

    def request_export(self, ctx: AccountContext) -> m.AccountExport:
        ctx.require_owner()
        export = self.uow.exports.add(
            m.AccountExport(account_id=ctx.account_id, requested_by=ctx.require_user().id, status="pending")
        )
        self.uow.outbox.add(
            m.OutboxEvent(kind="account_export", account_id=ctx.account_id, payload={"export_id": str(export.id)},
                          available_at=utcnow())
        )
        audit(self.uow, ctx.account_id, ctx.actor, "export.requested", target_type="export", target_id=export.id)
        self.uow.commit()
        return export

    def list_exports(self, ctx: AccountContext, page: PageRequest) -> Page[m.AccountExport]:
        ctx.require_owner()
        return self.uow.exports.list_page(ctx.account_id, page)

    def get_export(self, ctx: AccountContext, export_id: uuid.UUID) -> m.AccountExport:
        ctx.require_owner()
        export = self.uow.exports.get(export_id, ctx.account_id)
        if export is None:
            raise NotFound("Export not found.")
        return export

    def _downloadable(self, ctx: AccountContext, export_id: uuid.UUID) -> m.AccountExport:
        export = self.get_export(ctx, export_id)
        if export.status != "ready" or not export.object_key:
            raise ApiError("This export isn't ready yet.", code="export_not_ready", status_code=409)
        if export.expires_at and export.expires_at <= utcnow():
            raise ApiError("This export has expired. Request a new one.", code="export_expired", status_code=410)
        return export

    def download(self, ctx: AccountContext, export_id: uuid.UUID) -> bytes:
        export = self._downloadable(ctx, export_id)
        assert export.object_key
        body = self.storage.read(export.object_key, (export.byte_size or 0) + 1)
        audit(self.uow, ctx.account_id, ctx.actor, "export.downloaded", target_type="export", target_id=export.id)
        self.uow.commit()
        return body

    def download_link(self, ctx: AccountContext, export_id: uuid.UUID) -> str:
        """A short-lived link straight to storage, so large exports don't pass through the API."""
        export = self._downloadable(ctx, export_id)
        assert export.object_key
        url = self.storage.presign_download(export.object_key, EXPORT_LINK_SECONDS,
                                            filename=f"mailvender-export-{export.id}.zip")
        audit(self.uow, ctx.account_id, ctx.actor, "export.downloaded", target_type="export", target_id=export.id)
        self.uow.commit()
        return url

    def build_export(self, export_id: uuid.UUID) -> None:
        """Worker: writes a ZIP of JSON files to private storage."""
        export = self.uow.exports.get_by_id(export_id)
        if export is None or export.status != "pending":
            return
        account = self.uow.accounts.get(export.account_id)
        assert account is not None
        assets = self.uow.assets.list_all_for_account(account.id)
        files: dict[str, Any] = {
            "account.json": _row(account, ["id", "name", "kind", "physical_address", "hourly_recipient_limit",
                                           "daily_recipient_limit", "sending_suspended_at", "suspension_reason",
                                           "created_at"]),
            "members.json": [
                {**_row(membership, ["user_id", "role", "created_at"]), "email": user.email}
                for membership, user in self.uow.memberships.list_for_account(account.id,
                                                                              PageRequest(limit=10_000)).items
            ],
            "domains.json": [
                _row(d, ["id", "name", "status", "return_path_host", "verified_at", "created_at"])
                for d in self.uow.domains.list_all_for_account(account.id)
            ],
            "sender_identities.json": [
                _row(i, ["id", "email", "display_name", "status", "verified_at", "disabled_at", "created_at"])
                for i in self.uow.sender_identities.list_all_for_account(account.id)
            ],
            "assets.json": [
                _row(a, ["id", "status", "original_filename", "mime_type", "byte_size", "width", "height",
                         "public_url", "created_at", "finalized_at", "deleted_at"])
                for a in assets
            ],
            "suppressions.json": [
                _row(s, ["email", "reason", "source", "created_at"])
                for s in self.uow.suppressions.list_all_for_account(account.id)
            ],
            "messages.json": [
                {
                    **_row(message, ["id", "from_email", "subject", "category", "created_at"]),
                    "metadata": message.metadata_,
                    "recipients": [_row(r, ["id", "email", "status", "created_at", "accepted_at", "failed_at",
                                            "bounced_at", "complained_at"]) for r in recipients],
                }
                for message, recipients in self.uow.messages.list_for_export(account.id, EXPORT_MESSAGE_LIMIT)
            ],
        }
        records: dict[str, list[dict[str, Any]]] = {}
        blobs: dict[str, str] = {}  # file in the ZIP -> object key, copied one at a time
        for record in self.uow.sync.list_all_for_account(account.id):
            row: dict[str, Any] = {"id": str(record.id), "parent_id": str(record.parent_id) if record.parent_id else None,
                                   "revision": record.revision, "updated_at": record.updated_at.isoformat(),
                                   "data": record.data}
            if record.object_key:
                # Content kept in object storage (datasets) goes in as its own file.
                name = f"workspace/{record.collection}/{record.id}.json"
                blobs[name] = record.object_key
                row["content_file"] = name
            records.setdefault(record.collection, []).append(row)
        for collection, rows in records.items():
            files[f"workspace/{collection}.json"] = rows
        for asset_row, asset in zip(files["assets.json"], assets, strict=True):
            if asset.status == "ready" and asset.public_key:
                name = f"images/{asset.id}-{safe_filename(asset.original_filename)}"
                blobs[name] = asset.public_key
                asset_row["file"] = name
        files["README.txt"] = (
            "Mailvender account export.\n\nworkspace/*.json – pages, components, brand settings, datasets and "
            "version history (the same JSON the editor stores); each dataset's full content is in "
            "workspace/datasets/<id>.json.\nimages/ – every image uploaded to this account (listed in "
            "assets.json).\nmessages.json – message metadata and recipient "
            "statuses (email bodies are not included).\nsuppressions.json – addresses that won't receive email "
            "from this account.\n"
        )
        # Built in a temporary file (images and datasets can add up), then streamed to storage.
        with tempfile.TemporaryFile() as buffer:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in files.items():
                    archive.writestr(name, content if isinstance(content, (str, bytes))
                                     else json.dumps(content, indent=2, default=str))
                for name, object_key in blobs.items():
                    archive.writestr(name, self._read_whole(object_key))
            size = buffer.tell()
            buffer.seek(0)
            key = f"{self.settings.aws_s3_prefix}exports/{account.id}/{export.id}-{uuid.uuid4().hex}.zip"
            self.storage.put_file(key, buffer, "application/zip")
        export.object_key = key
        export.byte_size = size
        export.status = "ready"
        export.completed_at = utcnow()
        export.expires_at = utcnow() + timedelta(hours=self.settings.export_ttl_hours)
        audit(self.uow, account.id, Actor.system(), "export.completed", target_type="export", target_id=export.id,
              bytes=size)
        log_event(log, "export.completed", export_id=str(export.id), bytes=size)

    def _read_whole(self, key: str) -> bytes:
        info = self.storage.head(key)
        return self.storage.read(key, (info.size if info else 0) + 1) if info else b""

    # ---- Deletion and restore ----

    def request_deletion(self, ctx: AccountContext, confirm_name: str) -> None:
        ctx.require_owner()
        account = self.uow.accounts.get(ctx.account_id, for_update=True)
        assert account is not None
        if confirm_name.strip() != account.name:
            raise ValidationFailed([field_error("confirm_name", "Type the account's name to confirm.", "mismatch")])
        self._delete_account(account, ctx.actor)
        self.uow.commit()

    def _delete_account(self, account: m.Account, actor: Actor, *, notify: bool = True) -> None:
        """Stops the account now; its data is erased by the retention job unless an owner restores it."""
        now = utcnow()
        member_ids = self.uow.memberships.list_user_ids(account.id)
        account.status = "deleted"
        account.deletion_requested_at = now
        revoked_keys = self.uow.api_keys.revoke_all(account.id, now)
        revoked_sessions = self.uow.sessions.revoke_for_users(member_ids, now)
        for domain in self.uow.domains.list_all_for_account(account.id):
            if domain.status != "disabled":
                domain.status, domain.disabled_at, domain.bulk_eligible = "disabled", now, False
        for identity in self.uow.sender_identities.list_all_for_account(account.id):
            if identity.status != "disabled":
                identity.status, identity.disabled_at = "disabled", now
        stopped = self.uow.messages.fail_pending_for_account(account.id, "Account deleted", now)
        self.uow.outbox.cancel_for_account(account.id)
        # Memberships stay (hidden while the account is deleted) so a restore brings everyone back.
        for user_id in member_ids:
            user = self.uow.users.get(user_id)
            if user and user.default_account_id == account.id:
                others = self.uow.accounts.list_for_user(user_id)
                user.default_account_id = others[0][0].id if others else None
        purge_on = purge_date(account)
        for user_id in member_ids if notify else []:
            membership = self.uow.memberships.get(account.id, user_id)
            user = self.uow.users.get(user_id)
            if membership and membership.role == "owner" and user:
                enqueue_system_email(self.uow, user.email, emails.account_deletion_scheduled(
                    account.name, now, purge_on, app_url("/settings")), kind="account_deletion_scheduled")
        audit(self.uow, account.id, actor, "account.deletion_requested", target_type="account",
              target_id=account.id, revoked_api_keys=revoked_keys, revoked_sessions=revoked_sessions,
              stopped_recipients=stopped, purge_on=purge_on.isoformat())
        log_event(log, "account.deletion_requested", deleted_account_id=str(account.id))

    def restore(self, user: m.User, account_id: uuid.UUID) -> m.Account:
        membership = self.uow.memberships.get(account_id, user.id)
        account = self.uow.accounts.get(account_id, for_update=True) if membership else None
        if membership is None or account is None or account.status != "deleted" or account.deleted_at:
            raise NotFound("There's no deleted account to restore here.")
        if membership.role != "owner":
            raise Forbidden("Only an owner can restore this account.", code="owner_required")
        deleted_at = account.deletion_requested_at
        account.status, account.deletion_requested_at = "active", None
        restored_domains, taken_domains = [], []
        for domain in self.uow.domains.list_all_for_account(account.id):
            if domain.disabled_at != deleted_at:
                continue  # disabled before the deletion: left as it was
            if self.uow.domains.get_live_by_name(domain.name):
                taken_domains.append(domain.name)  # someone else added it meanwhile
                continue
            # Its DNS records are unchanged, so "Check DNS" verifies it again.
            domain.status, domain.disabled_at = "pending", None
            restored_domains.append(domain.name)
        for identity in self.uow.sender_identities.list_all_for_account(account.id):
            if identity.disabled_at == deleted_at:
                identity.status, identity.disabled_at, identity.verified_at = "pending", None, None
        if user.default_account_id is None:
            user.default_account_id = account.id
        audit(self.uow, account.id, Actor("user", user.id), "account.restored", target_type="account",
              target_id=account.id, domains=restored_domains, domains_taken=taken_domains)
        self.uow.commit()
        log_event(log, "account.restored", restored_account_id=str(account.id))
        return account

    def delete_account_assets(self, account_id: uuid.UUID) -> int:
        """Outbox events queued before deletion became restorable; the retention job does this now."""
        count = AssetService(self.uow, self.storage).delete_all_for_account(account_id)
        SyncService(self.uow, self.storage).delete_account_content(account_id)
        audit(self.uow, account_id, Actor.system(), "account.assets_deleted", target_type="account",
              target_id=account_id, count=count)
        return count

    # ---- The person: their own data and login ----

    def personal_export(self, user: m.User) -> bytes:
        """A ZIP of what Mailvender holds about this person (workspace content is in each account's export)."""
        now = utcnow()
        memberships = self.uow.accounts.list_memberships_for_user(user.id)
        files: dict[str, Any] = {
            "profile.json": _row(user, ["id", "email", "email_verified_at", "default_account_id", "created_at"]),
            "accounts.json": [
                {**_row(account, ["id", "name", "kind", "status", "created_at"]), "role": membership.role,
                 "joined_at": _iso(membership.created_at)}
                for account, membership in memberships
            ],
            "invitations.json": [
                {**_row(i, ["account_id", "role", "created_at", "expires_at", "accepted_at", "revoked_at"])}
                for i in self.uow.invitations.list_for_email(user.email)
            ],
            "sessions.json": [
                _row(s, ["created_at", "last_seen_at", "expires_at", "user_agent", "ip_address"])
                for s in self.uow.sessions.list_active_for_user(user.id, now)
            ],
            "activity.json": [
                {**_row(e, ["account_id", "action", "target_type", "target_id", "created_at"]), "data": e.data}
                for e in self.uow.audit.list_by_user(user.id, PERSONAL_ACTIVITY_LIMIT)
            ],
            "README.txt": (
                "Your Mailvender data.\n\nprofile.json – your login (the password is stored only as a hash and "
                "isn't included).\naccounts.json – the workspaces you belong to and your role.\n"
                "invitations.json – invitations sent to your address.\nsessions.json – where you're signed in.\n"
                f"activity.json – what you did, newest first (up to {PERSONAL_ACTIVITY_LIMIT}).\n\n"
                "Workspace content (pages, images, data sources, messages) is in each workspace's own export: "
                "Settings → Your data → Request export.\n"
            ),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content if isinstance(content, str)
                                 else json.dumps(content, indent=2, default=str))
        audit(self.uow, None, Actor("user", user.id), "user.exported", target_type="user", target_id=user.id)
        self.uow.commit()
        return buffer.getvalue()

    def delete_user(self, user: m.User, password: str) -> None:
        if not verify_password(user.password_hash, password):
            raise ValidationFailed([field_error("password", "That password isn't right.", "wrong_password")])
        memberships = self.uow.accounts.list_memberships_for_user(user.id)
        owned = [account for account, membership in memberships
                 if account.status == "active" and membership.role == "owner"]
        shared = [account for account in owned if len(self.uow.memberships.list_user_ids(account.id)) > 1]
        if shared:
            names = ", ".join(account.name for account in shared)
            raise Conflict(f"You own {names}, which other people use. Make someone else the owner, or delete "
                           "it, first.", code="owns_shared_accounts",
                           details={"accounts": [{"id": str(a.id), "name": a.name} for a in shared]})
        now = utcnow()
        actor = Actor("user", user.id)
        for account in owned:
            locked = self.uow.accounts.get(account.id, for_update=True)
            assert locked is not None
            self._delete_account(locked, actor, notify=False)  # they can't restore it; one goodbye email
        email = user.email
        self.uow.memberships.delete_all_for_user(user.id)
        self.uow.sessions.revoke_for_users([user.id], now)
        self.uow.email_tokens.delete_for_user(user.id)
        self.uow.invitations.revoke_pending_for_email(email, now)
        enqueue_system_email(self.uow, email, emails.login_deleted(email, now), kind="login_deleted")
        # The row stays (history points at it) but nothing identifies the person any more.
        user.email = f"deleted-{user.id.hex}@deleted.invalid"
        user.password_hash = hash_password(new_token())
        user.disabled_at = user.deleted_at = now
        user.default_account_id = None
        user.is_operator = False
        audit(self.uow, None, actor, "user.deleted", target_type="user", target_id=user.id,
              deleted_accounts=[str(a.id) for a in owned], left_accounts=len(memberships) - len(owned))
        self.uow.commit()
        log_event(log, "user.deleted", deleted_user_id=str(user.id))

    # ---- Retention ----

    def enforce_retention(self) -> RetentionReport:
        s = self.settings
        now = utcnow()
        report = RetentionReport()
        report.message_content_purged = self.uow.messages.purge_content_before(
            now - timedelta(days=s.retention_message_content_days), now)
        report.messages_deleted = self.uow.messages.delete_before(now - timedelta(days=s.retention_messages_days))
        report.delivery_events_deleted = self.uow.delivery.delete_before(
            now - timedelta(days=s.retention_delivery_events_days))
        report.outbox_deleted = self.uow.outbox.delete_processed_before(now - timedelta(days=7))
        report.sessions_deleted = self.uow.sessions.delete_expired_before(now - timedelta(days=30))
        report.tokens_deleted = self.uow.email_tokens.delete_expired_before(now - timedelta(days=7))
        report.rate_counters_deleted = self.uow.rate_limits.delete_before(now - timedelta(days=2))
        retired_before = now - timedelta(days=s.retention_retired_dkim_days)
        report.dkim_keys_deleted = (self.uow.dkim_keys.delete_retired_before(retired_before)
                                    + self.uow.platform_dkim_keys.delete_retired_before(retired_before))
        for export in self.uow.exports.list_expired(now):
            if export.object_key:
                self.storage.delete(export.object_key)
            export.status, export.object_key = "expired", None
            report.exports_expired += 1
        for account in self.uow.accounts.deleted_before(now - timedelta(days=s.retention_deleted_account_days)):
            # Images and workspace data go; suppressions stay so opted-out people are never emailed again.
            AssetService(self.uow, self.storage).delete_all_for_account(account.id)
            SyncService(self.uow, self.storage).delete_account_content(account.id)
            self.uow.sync.delete_all_for_account(account.id)
            self.uow.memberships.delete_all_for_account(account.id)
            account.deleted_at = now
            account.physical_address = None
            audit(self.uow, account.id, Actor.system(), "account.purged", target_type="account", target_id=account.id)
            report.accounts_purged += 1
        report.uploads_cleaned = AssetService(self.uow, self.storage).cleanup_abandoned_uploads()
        report.uploads_cleaned += SyncService(self.uow, self.storage).cleanup_expired_uploads()
        report.inbound_deleted = InboundService(self.uow, self.storage).delete_old(
            now - timedelta(days=s.retention_inbound_mail_days))
        report.dmarc_rows_deleted = self.uow.inbound.delete_dmarc_before(now - timedelta(days=s.retention_dmarc_rows_days))
        report.admin_sessions_deleted = self.uow.admins.delete_sessions_before(now - timedelta(days=30))
        report.admin_mail_deleted = AdminMailService(self.uow, self.storage).delete_old(
            now - timedelta(days=s.retention_inbound_mail_days), now - timedelta(days=1))
        self.uow.commit()
        log_event(log, "retention.completed", **report.__dict__)
        return report
