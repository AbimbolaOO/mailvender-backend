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
from typing import Any

from app import models as m
from app.config import get_settings
from app.errors import ApiError, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.common import AccountContext, audit
from app.services.storage import ObjectStorage

log = get_logger("sync")

COLLECTIONS = ("pages", "workspace", "saved_pages", "components", "brand", "datasets", "versions")
MAX_CHANGES = 500
MAX_SNAPSHOT = 200


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


def sheet_key(record: m.SyncRecord) -> str:
    return f"{get_settings().aws_s3_prefix}sheets/{record.created_by or record.account_id}/{record.id}.json"


def _check_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValidationFailed([field_error("collection", f"Use one of: {', '.join(COLLECTIONS)}.", "invalid")])


class SyncService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage | None = None):
        self.uow = uow
        self.storage = storage

    def _mirror_sheet(self, record: m.SyncRecord) -> None:
        if record.collection != "datasets" or self.storage is None:
            return
        key = sheet_key(record)
        try:
            if record.deleted:
                self.storage.delete(key)
            else:
                body = json.dumps(record.data, ensure_ascii=False, separators=(",", ":")).encode()
                self.storage.put(key, body, "application/json")
        except Exception as error:  # noqa: BLE001 – the synced record is already committed
            log_event(log, "sync.sheet_mirror_failed", logging.WARNING, record_id=str(record.id), error=type(error).__name__)

    def put(
        self,
        ctx: AccountContext,
        collection: str,
        record_id: uuid.UUID,
        expected_revision: int,
        data: dict[str, Any],
        parent_id: uuid.UUID | None = None,
    ) -> m.SyncRecord:
        _check_collection(collection)
        if collection == "versions" and parent_id is None:
            raise ValidationFailed([field_error("parent_id", "Versions need the page's id.", "required")])
        self.uow.sync.lock_account(ctx.account_id)
        record = self.uow.sync.get(ctx.account_id, collection, record_id)
        now = utcnow()
        user_id = ctx.user.id if ctx.user else None
        if record is None:
            if expected_revision != 0:
                raise _conflict(None)
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
            if record.revision != expected_revision:
                raise _conflict(record)
            record.revision += 1
            record.data = data
            record.deleted = False
            record.parent_id = parent_id or record.parent_id
            record.seq = self.uow.sync.next_seq()
            record.updated_at = now
            record.updated_by = user_id
        self.uow.commit()
        self._mirror_sheet(record)
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
        self.uow.commit()
        self._mirror_sheet(record)
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
