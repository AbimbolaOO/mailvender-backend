"""Account export, account deletion and retention (see docs/RETENTION.md)."""

import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app import models as m
from app.config import get_settings
from app.errors import ApiError, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.assets import AssetService
from app.services.common import AccountContext, Actor, audit
from app.services.storage import ObjectStorage
from app.services.sync import sheet_key

log = get_logger("lifecycle")

EXPORT_MESSAGE_LIMIT = 50_000


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row(entity: Any, fields: list[str]) -> dict[str, Any]:
    return {name: _iso(getattr(entity, name)) if not isinstance(getattr(entity, name), uuid.UUID)
            else str(getattr(entity, name)) for name in fields}


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

    def download(self, ctx: AccountContext, export_id: uuid.UUID) -> bytes:
        export = self.get_export(ctx, export_id)
        if export.status != "ready" or not export.object_key:
            raise ApiError("This export isn't ready yet.", code="export_not_ready", status_code=409)
        if export.expires_at and export.expires_at <= utcnow():
            raise ApiError("This export has expired. Request a new one.", code="export_expired", status_code=410)
        body = self.storage.read(export.object_key, (export.byte_size or 0) + 1)
        audit(self.uow, ctx.account_id, ctx.actor, "export.downloaded", target_type="export", target_id=export.id)
        self.uow.commit()
        return body

    def build_export(self, export_id: uuid.UUID) -> None:
        """Worker: writes a ZIP of JSON files to private storage."""
        export = self.uow.exports.get_by_id(export_id)
        if export is None or export.status != "pending":
            return
        account = self.uow.accounts.get(export.account_id)
        assert account is not None
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
                for a in self.uow.assets.list_all_for_account(account.id)
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
        for record in self.uow.sync.list_all_for_account(account.id):
            records.setdefault(record.collection, []).append(
                {"id": str(record.id), "parent_id": str(record.parent_id) if record.parent_id else None,
                 "revision": record.revision, "updated_at": record.updated_at.isoformat(), "data": record.data}
            )
        for collection, rows in records.items():
            files[f"workspace/{collection}.json"] = rows
        files["README.txt"] = (
            "Mailvender account export.\n\nworkspace/*.json – pages, components, brand settings, datasets and "
            "version history (the same JSON the editor stores).\nmessages.json – message metadata and recipient "
            "statuses (email bodies are not included).\nsuppressions.json – addresses that won't receive email "
            "from this account.\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content if isinstance(content, str) else json.dumps(content, indent=2,
                                                                                           default=str))
        body = buffer.getvalue()
        key = f"{self.settings.aws_s3_prefix}exports/{account.id}/{export.id}-{uuid.uuid4().hex}.zip"
        self.storage.put(key, body, "application/zip")
        export.object_key = key
        export.byte_size = len(body)
        export.status = "ready"
        export.completed_at = utcnow()
        export.expires_at = utcnow() + timedelta(hours=self.settings.export_ttl_hours)
        audit(self.uow, account.id, Actor.system(), "export.completed", target_type="export", target_id=export.id,
              bytes=len(body))
        log_event(log, "export.completed", export_id=str(export.id), bytes=len(body))

    # ---- Deletion ----

    def request_deletion(self, ctx: AccountContext, confirm_name: str) -> None:
        ctx.require_owner()
        account = self.uow.accounts.get(ctx.account_id, for_update=True)
        assert account is not None
        if confirm_name.strip() != account.name:
            raise ValidationFailed([field_error("confirm_name", "Type the account's name to confirm.", "mismatch")])
        now = utcnow()
        member_ids = self.uow.memberships.list_user_ids(account.id)
        account.status = "deleted"
        account.deletion_requested_at = now
        revoked_keys = self.uow.api_keys.revoke_all(account.id, now)
        revoked_sessions = self.uow.sessions.revoke_for_users(member_ids, now)
        for domain in self.uow.domains.list_all_for_account(account.id):
            domain.status, domain.disabled_at, domain.bulk_eligible = "disabled", now, False
        for identity in self.uow.sender_identities.list_all_for_account(account.id):
            identity.status, identity.disabled_at = "disabled", now
        stopped = self.uow.messages.fail_pending_for_account(account.id, "Account deleted", now)
        self.uow.outbox.cancel_for_account(account.id)
        self.uow.memberships.delete_all_for_account(account.id)
        for user_id in member_ids:
            user = self.uow.users.get(user_id)
            if user and user.default_account_id == account.id:
                others = self.uow.accounts.list_for_user(user_id)
                user.default_account_id = others[0][0].id if others else None
        self.uow.outbox.add(
            m.OutboxEvent(kind="account_assets_delete", account_id=account.id, payload={"account_id": str(account.id)},
                          available_at=now)
        )
        audit(self.uow, account.id, ctx.actor, "account.deletion_requested", target_type="account",
              target_id=account.id, revoked_api_keys=revoked_keys, revoked_sessions=revoked_sessions,
              stopped_recipients=stopped)
        self.uow.commit()
        log_event(log, "account.deletion_requested", deleted_account_id=str(account.id))

    def delete_account_assets(self, account_id: uuid.UUID) -> int:
        count = AssetService(self.uow, self.storage).delete_all_for_account(account_id)
        for record in self.uow.sync.list_all_for_account(account_id):
            if record.collection == "datasets":
                self.storage.delete(sheet_key(record))
        audit(self.uow, account_id, Actor.system(), "account.assets_deleted", target_type="account",
              target_id=account_id, count=count)
        return count

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
        report.dkim_keys_deleted = self.uow.dkim_keys.delete_retired_before(
            now - timedelta(days=s.retention_retired_dkim_days))
        for export in self.uow.exports.list_expired(now):
            if export.object_key:
                self.storage.delete(export.object_key)
            export.status, export.object_key = "expired", None
            report.exports_expired += 1
        for account in self.uow.accounts.deleted_before(now - timedelta(days=s.retention_deleted_account_days)):
            # Workspace data goes; suppressions stay so opted-out people are never emailed again.
            self.uow.sync.delete_all_for_account(account.id)
            account.deleted_at = now
            account.physical_address = None
            audit(self.uow, account.id, Actor.system(), "account.purged", target_type="account", target_id=account.id)
            report.accounts_purged += 1
        report.uploads_cleaned = AssetService(self.uow, self.storage).cleanup_abandoned_uploads()
        self.uow.commit()
        log_event(log, "retention.completed", **report.__dict__)
        return report
