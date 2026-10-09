"""Cloud sync: per-account records with revisions, tombstones and a change feed.

Contract (also in the OpenAPI description of the sync endpoints):

* Each record is identified by (collection, id) and carries `revision`,
  `updated_at` and `deleted` (a tombstone keeps the id and revision so other
  devices learn about the deletion).
* Writes and deletes send `expected_revision` – 0 to create. A mismatch returns
  `409 revision_conflict` with the current record in `error.details.current`;
  nothing is overwritten.
* Conflict policy (applied by the client): when a queued/offline change hits a
  conflict, the client takes the current record from the 409, then retries its
  own change with `expected_revision = current.revision` – last write wins, the
  retried local change being the last write – and shows a non-blocking notice
  that the server version was replaced.
* `seq` orders changes per account. A new device reads the snapshot (noting
  `watermark`) and then the changes after it.
"""

import base64
import json
import logging
import uuid
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
from app.services.common import AccountContext, audit
from app.services.storage import ObjectStorage, PresignedUpload

log = get_logger("sync")

COLLECTIONS = ("pages", "workspace", "saved_pages", "components", "brand", "datasets", "versions")
MAX_CHANGES = 500
MAX_SNAPSHOT = 200
# Collections whose content is stored as an object; `data` holds only a summary.
CONTENT_COLLECTIONS = ("datasets",)
SUMMARY_MAX_BYTES = 64 * 1024
CONTENT_TYPE = "application/json"


@dataclass
class SnapshotPage:
    records: list[m.SyncRecord]
    next_cursor: str | None
    watermark: int


@dataclass
class ChangesPage:
    records: list[m.SyncRecord]
    next_since: int
    has_more: bool


def record_state(record: m.SyncRecord) -> dict[str, Any]:
    return {
        "collection": record.collection,
        "id": str(record.id),
        "parent_id": str(record.parent_id) if record.parent_id else None,
        "revision": record.revision,
        "deleted": record.deleted,
        "data": record.data,
        "content_size": record.object_size,
        "seq": record.seq,
        "updated_at": record.updated_at.isoformat(),
        "created_at": record.created_at.isoformat(),
        "updated_by": str(record.updated_by) if record.updated_by else None,
    }


def _conflict(record: m.SyncRecord | None) -> ApiError:
    return ApiError(
        "This record changed on another device. Fetch the current version and retry.",
        code="revision_conflict",
        status_code=409,
        details={"current": record_state(record) if record else None},
    )


def _content_key(owner_id: uuid.UUID, record_id: uuid.UUID) -> str:
    return f"{get_settings().aws_s3_prefix}sheets/{owner_id}/{record_id}/{uuid.uuid4().hex}.json"


def _upload_invalid() -> ApiError:
    # The client uploads again and retries the write.
    return ApiError("The uploaded content is missing or expired. Upload it again.", code="content_upload_invalid",
                    status_code=409)


def _check_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValidationFailed([field_error("collection", f"Use one of: {', '.join(COLLECTIONS)}.", "invalid")])


class SyncService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage | None = None):
        self.uow = uow
        self.storage = storage

    def _delete_object(self, key: str | None) -> None:
        """Best effort: the committed records no longer point at `key`; a leftover object is only storage."""
        if not key or self.storage is None:
            return
        try:
            self.storage.delete(key)
        except Exception as error:  # noqa: BLE001
            log_event(log, "sync.object_delete_failed", logging.WARNING, key=key, error=type(error).__name__)

    def _storage(self) -> ObjectStorage:
        assert self.storage is not None, "content collections need object storage"
        return self.storage

    def content_url(self, record: m.SyncRecord) -> str | None:
        if record.deleted or not record.object_key or self.storage is None:
            return None
        return self.storage.presign_download(record.object_key, get_settings().sync_content_url_ttl_seconds)

    def create_content_upload(self, ctx: AccountContext, collection: str, record_id: uuid.UUID,
                              size: int) -> tuple[m.SyncUpload, PresignedUpload]:
        _check_collection(collection)
        settings = get_settings()
        if collection not in CONTENT_COLLECTIONS:
            raise ValidationFailed([field_error("collection", "This collection has no separate content.", "invalid")])
        if not 0 < size <= settings.dataset_max_bytes:
            limit = settings.dataset_max_bytes // (1024 * 1024)
            raise ValidationFailed([field_error("size", f"Data sources can be up to {limit} MB.", "too_large")])
        record = self.uow.sync.get(ctx.account_id, collection, record_id)
        owner_id = (record.created_by if record else None) or (ctx.user.id if ctx.user else ctx.account_id)
        ttl = settings.sync_upload_url_ttl_seconds
        upload = self.uow.sync.add_upload(m.SyncUpload(
            account_id=ctx.account_id, collection=collection, record_id=record_id,
            object_key=_content_key(owner_id, record_id), max_bytes=size, expires_at=utcnow() + timedelta(seconds=ttl),
        ))
        presigned = self._storage().presign_upload(upload.object_key, CONTENT_TYPE, size, ttl)
        self.uow.commit()
        return upload, presigned

    def _claim_upload(self, ctx: AccountContext, collection: str, record_id: uuid.UUID,
                      upload_id: uuid.UUID | None, data: dict[str, Any]) -> tuple[m.SyncUpload, int] | None:
        if collection not in CONTENT_COLLECTIONS:
            if upload_id is not None:
                raise ValidationFailed([field_error("content_upload_id", "This collection has no separate content.",
                                                    "invalid")])
            return None
        if upload_id is None:
            raise ValidationFailed([field_error("content_upload_id", "Upload the content first.", "required")])
        if len(json.dumps(data)) > SUMMARY_MAX_BYTES:
            raise ValidationFailed([field_error("data", "Send only a summary; the content goes in the upload.",
                                                "too_large")])
        upload = self.uow.sync.get_upload(upload_id, ctx.account_id)
        if upload is None or upload.collection != collection or upload.record_id != record_id:
            raise _upload_invalid()
        info = self._storage().head(upload.object_key)
        if info is None:
            raise _upload_invalid()
        if info.size > upload.max_bytes:
            self._delete_object(upload.object_key)
            raise ValidationFailed([field_error("content_upload_id", "The upload is larger than declared.",
                                                "too_large")])
        return upload, info.size

    def put(
        self,
        ctx: AccountContext,
        collection: str,
        record_id: uuid.UUID,
        expected_revision: int,
        data: dict[str, Any],
        parent_id: uuid.UUID | None = None,
        content_upload_id: uuid.UUID | None = None,
    ) -> m.SyncRecord:
        _check_collection(collection)
        if collection == "versions" and parent_id is None:
            raise ValidationFailed([field_error("parent_id", "Versions need the page's id.", "required")])
        self.uow.sync.lock_account(ctx.account_id)
        record = self.uow.sync.get(ctx.account_id, collection, record_id)
        if (record.revision if record else 0) != expected_revision:
            raise _conflict(record)
        # Validated after the revision check, so a conflicting write keeps its upload for the retry.
        claimed = self._claim_upload(ctx, collection, record_id, content_upload_id, data)
        previous_key = record.object_key if record else None
        now = utcnow()
        user_id = ctx.user.id if ctx.user else None
        if record is None:
            record = self.uow.sync.add(
                m.SyncRecord(
                    account_id=ctx.account_id,
                    collection=collection,
                    id=record_id,
                    parent_id=parent_id,
                    revision=1,
                    data=data,
                    deleted=False,
                    seq=self.uow.sync.next_seq(),
                    created_by=user_id,
                    updated_at=now,
                    updated_by=user_id,
                )
            )
        else:
            record.revision += 1
            record.data = data
            record.deleted = False
            record.parent_id = parent_id or record.parent_id
            record.seq = self.uow.sync.next_seq()
            record.updated_at = now
            record.updated_by = user_id
        if claimed:
            upload, size = claimed
            record.object_key, record.object_size = upload.object_key, size
            self.uow.sync.delete_upload(upload)
        self.uow.commit()
        if record.object_key != previous_key:
            self._delete_object(previous_key)
        return record

    def delete(self, ctx: AccountContext, collection: str, record_id: uuid.UUID, expected_revision: int) -> m.SyncRecord:
        _check_collection(collection)
        self.uow.sync.lock_account(ctx.account_id)
        record = self.uow.sync.get(ctx.account_id, collection, record_id)
        if record is None:
            raise NotFound("Record not found.")
        if record.revision != expected_revision:
            raise _conflict(record)
        if not record.deleted:
            record.revision += 1
            record.deleted = True
            record.data = None
            record.seq = self.uow.sync.next_seq()
            record.updated_at = utcnow()
            record.updated_by = ctx.user.id if ctx.user else None
        previous_key = record.object_key
        record.object_key = record.object_size = None
        self.uow.commit()
        self._delete_object(previous_key)
        return record

    def get(self, ctx: AccountContext, collection: str, record_id: uuid.UUID) -> m.SyncRecord:
        _check_collection(collection)
        record = self.uow.sync.get(ctx.account_id, collection, record_id)
        if record is None:
            raise NotFound("Record not found.")
        return record

    def snapshot(self, ctx: AccountContext, collections: list[str] | None, cursor: str | None,
                 limit: int) -> SnapshotPage:
        wanted = collections or list(COLLECTIONS)
        for collection in wanted:
            _check_collection(collection)
        limit = max(1, min(limit, MAX_SNAPSHOT))
        after = None
        watermark = self.uow.sync.max_seq(ctx.account_id)
        if cursor:
            try:
                collection, id_, watermark = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                after = (collection, uuid.UUID(id_))
            except (ValueError, TypeError) as error:
                raise ValidationFailed([field_error("cursor", "This cursor is not valid.")]) from error
        records = self.uow.sync.snapshot(ctx.account_id, wanted, after, limit + 1)
        next_cursor = None
        if len(records) > limit:
            records = records[:limit]
            last = records[-1]
            raw = json.dumps([last.collection, str(last.id), watermark]).encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return SnapshotPage(records=records, next_cursor=next_cursor, watermark=watermark)

    def changes(self, ctx: AccountContext, since: int, limit: int) -> ChangesPage:
        limit = max(1, min(limit, MAX_CHANGES))
        records = self.uow.sync.changes(ctx.account_id, since, limit + 1)
        has_more = len(records) > limit
        records = records[:limit]
        return ChangesPage(records=records, next_since=records[-1].seq if records else since, has_more=has_more)

    # ---- Version history ----

    def list_versions(self, ctx: AccountContext, page_id: uuid.UUID, page: PageRequest) -> Page[m.SyncRecord]:
        return self.uow.sync.list_children(ctx.account_id, "versions", page_id, page)

    def restore_version(self, ctx: AccountContext, page_id: uuid.UUID, version_id: uuid.UUID) -> m.SyncRecord:
        """Restores a version; the page as it is now is first kept as a new version."""
        self.uow.sync.lock_account(ctx.account_id)
        page = self.uow.sync.get(ctx.account_id, "pages", page_id)
        version = self.uow.sync.get(ctx.account_id, "versions", version_id)
        if page is None or page.deleted or not page.data:
            raise NotFound("Page not found.")
        if version is None or version.deleted or version.parent_id != page_id or not version.data:
            raise NotFound("Version not found.")
        now = utcnow()
        user_id = ctx.user.id if ctx.user else None
        local_page_id = page.data.get("localId")
        backup_id = uuid.uuid4()
        self.uow.sync.add(
            m.SyncRecord(
                account_id=ctx.account_id,
                collection="versions",
                id=backup_id,
                parent_id=page_id,
                revision=1,
                data={
                    "localId": f"version_{backup_id.hex[:12]}",
                    "pageId": local_page_id,
                    "name": f"Before restoring “{version.data.get('name', 'a version')}”",
                    "createdAt": int(now.timestamp() * 1000),
                    "auto": True,
                    "doc": page.data.get("doc"),
                },
                deleted=False,
                seq=self.uow.sync.next_seq(),
                created_by=user_id,
                updated_at=now,
                updated_by=user_id,
            )
        )
        page.data = {**page.data, "doc": version.data.get("doc")}
        page.revision += 1
        page.seq = self.uow.sync.next_seq()
        page.updated_at = now
        page.updated_by = user_id
        audit(self.uow, ctx.account_id, ctx.actor, "page.version_restored", target_type="page", target_id=page_id,
              version_id=str(version_id))
        self.uow.commit()
        return page

    # ---- Content housekeeping ----

    def delete_account_content(self, account_id: uuid.UUID) -> int:
        """Deletes the account's content objects and pending uploads (account deletion)."""
        count = 0
        for record in self.uow.sync.list_all_for_account(account_id):
            if record.object_key:
                self._storage().delete(record.object_key)
                record.object_key = record.object_size = None
                count += 1
        for upload in self.uow.sync.list_uploads_for_account(account_id):
            self._storage().delete(upload.object_key)
            self.uow.sync.delete_upload(upload)
        return count

    def cleanup_expired_uploads(self) -> int:
        """Uploads never committed by a write (the client went away): delete their objects."""
        expired = self.uow.sync.list_expired_uploads(utcnow() - timedelta(hours=1), 500)
        for upload in expired:
            self._storage().delete(upload.object_key)
            self.uow.sync.delete_upload(upload)
        return len(expired)

    def move_inline_content(self, limit: int = 500) -> int:
        """One-off: moves datasets stored before content lived in object storage out of Postgres."""
        moved = 0
        for collection in CONTENT_COLLECTIONS:
            for record in self.uow.sync.list_inline(collection, limit):
                data = record.data or {}
                body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
                key = _content_key(record.created_by or record.account_id, record.id)
                self._storage().put(key, body, CONTENT_TYPE)
                dataset = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
                record.data = {"localId": data.get("localId"), "name": dataset.get("name"),
                               "rowCount": len(dataset.get("rows") or [])}
                record.object_key, record.object_size = key, len(body)
                self.uow.commit()
                moved += 1
        return moved
