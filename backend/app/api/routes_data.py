"""Cloud sync, version history, export/deletion and the operator API."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api import schemas as s
from app.api.deps import Ctx, Operator, OwnerCtx, Uow, get_storage
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT, page_request
from app.services.health import AccountHealth, HealthService
from app.services.lifecycle import LifecycleService
from app.services.storage import ObjectStorage
from app.services.sync import SyncService

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1000)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]

SYNC_POLICY = (
    "Every record has `revision`, `updated_at` and `deleted` (tombstone). Writes and deletes send "
    "`expected_revision` (0 to create); a stale revision gets 409 `revision_conflict` with the current record in "
    "`error.details.current` and nothing is overwritten.\n\n**Conflict policy (client):** for a queued/offline change "
    "that conflicts, take the current record from the 409 and retry the local change with "
    "`expected_revision = current.revision` (last write wins – the retried local change is the last write), then "
    "show a non-blocking notice that the server version was replaced. A local delete is retried the same way; a "
    "local edit of a record deleted elsewhere re-creates it."
)


# ---- Sync ----


@router.get("/sync/snapshot", response_model=s.SnapshotOut, tags=["sync"],
            summary="All live records of the account (paged), for a new device", description=SYNC_POLICY)
def snapshot(
    ctx: Ctx,
    uow: Uow,
    collections: Annotated[list[str] | None, Query()] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> s.SnapshotOut:
    page = SyncService(uow).snapshot(ctx, collections, cursor, limit)
    return s.SnapshotOut(records=[s.SyncRecordOut.model_validate(r) for r in page.records],
                         next_cursor=page.next_cursor, watermark=page.watermark)


@router.get("/sync/changes", response_model=s.ChangesOut, tags=["sync"],
            summary="Changes (including deletions) after `since`", description=SYNC_POLICY)
def changes(ctx: Ctx, uow: Uow, since: Annotated[int, Query(ge=0)] = 0,
            limit: Annotated[int, Query(ge=1, le=500)] = 200) -> s.ChangesOut:
    page = SyncService(uow).changes(ctx, since, limit)
    return s.ChangesOut(records=[s.SyncRecordOut.model_validate(r) for r in page.records],
                        next_since=page.next_since, has_more=page.has_more)


@router.get("/sync/{collection}/{record_id}", response_model=s.SyncRecordOut, tags=["sync"])
def get_record(collection: s.Collection, record_id: uuid.UUID, ctx: Ctx, uow: Uow) -> s.SyncRecordOut:
    return s.SyncRecordOut.model_validate(SyncService(uow).get(ctx, collection, record_id))


@router.put("/sync/{collection}/{record_id}", response_model=s.SyncRecordOut, tags=["sync"],
            summary="Create or update a record", description=SYNC_POLICY)
def put_record(collection: s.Collection, record_id: uuid.UUID, body: s.SyncPutIn, ctx: Ctx,
               uow: Uow) -> s.SyncRecordOut:
    record = SyncService(uow).put(ctx, collection, record_id, body.expected_revision, body.data, body.parent_id)
    return s.SyncRecordOut.model_validate(record)


@router.delete("/sync/{collection}/{record_id}", response_model=s.SyncRecordOut, tags=["sync"],
               summary="Delete a record (leaves a tombstone)", description=SYNC_POLICY)
def delete_record(collection: s.Collection, record_id: uuid.UUID, ctx: Ctx, uow: Uow,
                  expected_revision: Annotated[int, Query(ge=1)]) -> s.SyncRecordOut:
    return s.SyncRecordOut.model_validate(SyncService(uow).delete(ctx, collection, record_id, expected_revision))


@router.get("/pages/{page_id}/versions", response_model=s.PageOut[s.SyncRecordOut], tags=["sync"],
            summary="Version history of a page (newest first)")
def list_versions(page_id: uuid.UUID, ctx: Ctx, uow: Uow, limit: Limit = None,
                  cursor: Cursor = None) -> s.PageOut[s.SyncRecordOut]:
    page = SyncService(uow).list_versions(ctx, page_id, page_request(limit, cursor))
    return s.PageOut[s.SyncRecordOut](items=[s.SyncRecordOut.model_validate(r) for r in page.items],
                                      next_cursor=page.next_cursor)


@router.post("/pages/{page_id}/versions/{version_id}/restore", response_model=s.SyncRecordOut, tags=["sync"],
             summary="Restore a version (the current page is saved as a new version first)")
def restore_version(page_id: uuid.UUID, version_id: uuid.UUID, ctx: Ctx, uow: Uow) -> s.SyncRecordOut:
    return s.SyncRecordOut.model_validate(SyncService(uow).restore_version(ctx, page_id, version_id))


# ---- Export and deletion (owners) ----


@router.post("/account/exports", status_code=202, response_model=s.ExportOut, tags=["data"],
             summary="Request an export (ZIP of JSON), built in the background")
def request_export(ctx: OwnerCtx, uow: Uow, storage: Storage) -> s.ExportOut:
    return s.ExportOut.model_validate(LifecycleService(uow, storage).request_export(ctx))


@router.get("/account/exports", response_model=s.PageOut[s.ExportOut], tags=["data"])
def list_exports(ctx: OwnerCtx, uow: Uow, storage: Storage, limit: Limit = None,
                 cursor: Cursor = None) -> s.PageOut[s.ExportOut]:
    page = LifecycleService(uow, storage).list_exports(ctx, page_request(limit, cursor))
    return s.PageOut[s.ExportOut](items=[s.ExportOut.model_validate(e) for e in page.items],
                                  next_cursor=page.next_cursor)


@router.get("/account/exports/{export_id}/download", tags=["data"],
            summary="Download an export (authenticated, until it expires; audited)",
            response_class=Response, responses={200: {"content": {"application/zip": {}}}})
def download_export(export_id: uuid.UUID, ctx: OwnerCtx, uow: Uow, storage: Storage) -> Response:
    body = LifecycleService(uow, storage).download(ctx, export_id)
    return Response(
        body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="mailvender-export-{export_id}.zip"',
                 "Cache-Control": "no-store"},
    )


@router.post("/account/deletion", status_code=202, response_model=s.Accepted, tags=["data"],
             summary="Delete the account",
             description="Revokes API keys and members' sessions, disables domains and senders, stops pending "
                         "sends, deletes assets and schedules the remaining data for deletion (see RETENTION.md).")
def delete_account(body: s.DeletionIn, ctx: OwnerCtx, uow: Uow, storage: Storage) -> s.Accepted:
    LifecycleService(uow, storage).request_deletion(ctx, body.confirm_name)
    return s.Accepted(message="The account is being deleted.")


# ---- Operator ----


def health_out(item: AccountHealth) -> s.AccountHealthOut:
    account = item.account
    status = "deleted" if account.status != "active" else ("suspended" if account.sending_suspended_at else "active")
    return s.AccountHealthOut(
        account=s.AccountOut.model_validate(account),
        status=status,
        metrics=s.HealthMetricsOut.model_validate(item.metrics),
        latest_decision=s.AuditEventOut.model_validate(item.latest_decision) if item.latest_decision else None,
    )


@router.get("/operator/accounts", response_model=s.PageOut[s.AccountHealthOut], tags=["operator"],
            summary="Account health, limits, suspension state and latest decision (operators only)")
def operator_accounts(_: Operator, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.AccountHealthOut]:
    page = HealthService(uow).list_page(page_request(limit, cursor))
    return s.PageOut[s.AccountHealthOut](items=[health_out(i) for i in page.items], next_cursor=page.next_cursor)


@router.get("/operator/accounts/{account_id}", response_model=s.AccountHealthOut, tags=["operator"])
def operator_account(account_id: uuid.UUID, _: Operator, uow: Uow) -> s.AccountHealthOut:
    return health_out(HealthService(uow).get(account_id))


@router.post("/operator/accounts/{account_id}/suspend", response_model=s.AccountHealthOut, tags=["operator"])
def operator_suspend(account_id: uuid.UUID, body: s.ReasonIn, op: Operator, uow: Uow) -> s.AccountHealthOut:
    return health_out(HealthService(uow).suspend(op, account_id, body.reason))


@router.post("/operator/accounts/{account_id}/reinstate", response_model=s.AccountHealthOut, tags=["operator"])
def operator_reinstate(account_id: uuid.UUID, body: s.ReasonIn, op: Operator, uow: Uow) -> s.AccountHealthOut:
    return health_out(HealthService(uow).reinstate(op, account_id, body.reason))


@router.put("/operator/accounts/{account_id}/limits", response_model=s.AccountHealthOut, tags=["operator"])
def operator_limits(account_id: uuid.UUID, body: s.LimitsIn, op: Operator, uow: Uow) -> s.AccountHealthOut:
    return health_out(HealthService(uow).set_limits(op, account_id, body.hourly_recipient_limit,
                                                    body.daily_recipient_limit, body.reason))


@router.post("/operator/accounts/{account_id}/reviews", response_model=s.AccountHealthOut, tags=["operator"],
             summary="Record a manual review")
def operator_review(account_id: uuid.UUID, body: s.ReasonIn, op: Operator, uow: Uow) -> s.AccountHealthOut:
    return health_out(HealthService(uow).review(op, account_id, body.reason))

