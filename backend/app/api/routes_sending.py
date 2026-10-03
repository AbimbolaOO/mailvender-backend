"""Domains, sender identities, assets, messages and suppressions."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api import schemas as s
from app.api.deps import Ctx, CtxOrKey, OwnerCtx, Uow, get_resolver, get_storage
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT, page_request
from app.services.assets import AssetService
from app.services.common import AccountContext
from app.services.dns import DnsResolver
from app.services.domains import DomainService, DomainView, ensure_member_may_manage_domains
from app.services.feedback import FeedbackService
from app.services.sending import SendingService, SendRequest
from app.services.storage import ObjectStorage

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=500)]
Resolver = Annotated[DnsResolver, Depends(get_resolver)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]


def domain_out(view: DomainView) -> s.DomainOut:
    d = view.domain
    return s.DomainOut(
        id=d.id, name=d.name, status=d.status, bulk_eligible=d.bulk_eligible,  # type: ignore[arg-type]
        return_path_host=d.return_path_host, dkim_selector=view.active_selector,
        records=[s.DnsRecordOut.model_validate(r) for r in view.records],
        last_checked_at=d.last_checked_at, verified_at=d.verified_at, created_at=d.created_at,
    )


def managing(ctx: Ctx) -> AccountContext:
    ensure_member_may_manage_domains(ctx)
    return ctx


Manager = Annotated[AccountContext, Depends(managing)]


# ---- Domains (owners and members) ----


@router.post("/domains", status_code=201, response_model=s.DomainOut, tags=["domains"],
             summary="Register a sending domain (generates a 2048-bit DKIM key)",
             description="409 `domain_exists` if this account has it; 409 `domain_unavailable` if another account "
                         "does (which account is never revealed).")
def register_domain(body: s.DomainIn, ctx: Manager, uow: Uow, resolver: Resolver) -> s.DomainOut:
    return domain_out(DomainService(uow, resolver).register(ctx, body.name))


@router.get("/domains", response_model=s.PageOut[s.DomainOut], tags=["domains"])
def list_domains(ctx: CtxOrKey, uow: Uow, resolver: Resolver, limit: Limit = None,
                 cursor: Cursor = None) -> s.PageOut[s.DomainOut]:
    page = DomainService(uow, resolver).list_page(ctx, page_request(limit, cursor))
    return s.PageOut[s.DomainOut](items=[domain_out(v) for v in page.items], next_cursor=page.next_cursor)


@router.get("/domains/{domain_id}", response_model=s.DomainOut, tags=["domains"],
            summary="A domain with its copy-paste-ready DNS records and their last verification status")
def get_domain(domain_id: uuid.UUID, ctx: CtxOrKey, uow: Uow, resolver: Resolver) -> s.DomainOut:
    service = DomainService(uow, resolver)
    return domain_out(service.view(service.get(ctx, domain_id)))


@router.post("/domains/{domain_id}/verify", response_model=s.DomainOut, tags=["domains"],
             summary="Look up the DNS records now; each is reported verified, missing or mismatched")
def verify_domain(domain_id: uuid.UUID, ctx: Manager, uow: Uow, resolver: Resolver) -> s.DomainOut:
    return domain_out(DomainService(uow, resolver).verify(ctx, domain_id))


@router.post("/domains/{domain_id}/disable", response_model=s.DomainOut, tags=["domains"])
def disable_domain(domain_id: uuid.UUID, ctx: Manager, uow: Uow, resolver: Resolver) -> s.DomainOut:
    return domain_out(DomainService(uow, resolver).set_disabled(ctx, domain_id, True))


@router.post("/domains/{domain_id}/enable", response_model=s.DomainOut, tags=["domains"],
             summary="Re-enable (returns to pending until verified again)")
def enable_domain(domain_id: uuid.UUID, ctx: Manager, uow: Uow, resolver: Resolver) -> s.DomainOut:
    return domain_out(DomainService(uow, resolver).set_disabled(ctx, domain_id, False))


@router.post("/domains/{domain_id}/dkim/rotate", response_model=s.DomainOut, tags=["domains"],
             summary="Start a DKIM key rotation",
             description="Adds a `dkim_next` record. Once it verifies, the new key becomes active and the old one "
                         "is retired (and deleted after the retention period).")
def rotate_dkim(domain_id: uuid.UUID, ctx: Manager, uow: Uow, resolver: Resolver) -> s.DomainOut:
    return domain_out(DomainService(uow, resolver).rotate_dkim(ctx, domain_id))


# ---- Sender identities (owners define them) ----


@router.post("/sender-identities", status_code=201, response_model=s.SenderIdentityOut, tags=["sender identities"])
def create_identity(body: s.SenderIdentityIn, ctx: OwnerCtx, uow: Uow, resolver: Resolver) -> s.SenderIdentityOut:
    return s.SenderIdentityOut.model_validate(
        DomainService(uow, resolver).create_identity(ctx, body.domain_id, body.email, body.display_name)
    )


@router.get("/sender-identities", response_model=s.PageOut[s.SenderIdentityOut], tags=["sender identities"])
def list_identities(ctx: CtxOrKey, uow: Uow, resolver: Resolver, limit: Limit = None,
                    cursor: Cursor = None) -> s.PageOut[s.SenderIdentityOut]:
    page = DomainService(uow, resolver).list_identities(ctx, page_request(limit, cursor))
    return s.PageOut[s.SenderIdentityOut](items=[s.SenderIdentityOut.model_validate(i) for i in page.items],
                                          next_cursor=page.next_cursor)


@router.post("/sender-identities/{identity_id}/verify", response_model=s.SenderIdentityOut, tags=["sender identities"],
             summary="Approve the sender (its domain must be verified)")
def verify_identity(identity_id: uuid.UUID, ctx: OwnerCtx, uow: Uow, resolver: Resolver) -> s.SenderIdentityOut:
    return s.SenderIdentityOut.model_validate(DomainService(uow, resolver).verify_identity(ctx, identity_id))


@router.post("/sender-identities/{identity_id}/disable", response_model=s.SenderIdentityOut, tags=["sender identities"])
def disable_identity(identity_id: uuid.UUID, ctx: OwnerCtx, uow: Uow, resolver: Resolver) -> s.SenderIdentityOut:
    return s.SenderIdentityOut.model_validate(DomainService(uow, resolver).set_identity_disabled(ctx, identity_id, True))


@router.post("/sender-identities/{identity_id}/enable", response_model=s.SenderIdentityOut, tags=["sender identities"])
def enable_identity(identity_id: uuid.UUID, ctx: OwnerCtx, uow: Uow, resolver: Resolver) -> s.SenderIdentityOut:
    return s.SenderIdentityOut.model_validate(
        DomainService(uow, resolver).set_identity_disabled(ctx, identity_id, False)
    )


@router.get("/sender-identities/{identity_id}/history", response_model=s.PageOut[s.AuditEventOut],
            tags=["sender identities"], summary="Audit history of one sender")
def identity_history(identity_id: uuid.UUID, ctx: Ctx, uow: Uow, resolver: Resolver, limit: Limit = None,
                     cursor: Cursor = None) -> s.PageOut[s.AuditEventOut]:
    page = DomainService(uow, resolver).identity_history(ctx, identity_id, page_request(limit, cursor))
    return s.PageOut[s.AuditEventOut](items=[s.AuditEventOut.model_validate(e) for e in page.items],
                                      next_cursor=page.next_cursor)


# ---- Assets ----


@router.post("/assets/uploads", status_code=201, response_model=s.UploadOut, tags=["assets"],
             summary="Get a presigned upload for one image",
             description="PNG, JPEG, GIF or WebP up to 5 MB. POST the returned `fields` plus the file (as `file`) "
                         "to `url` within `expires_in` seconds, then call finalize.")
def create_upload(body: s.UploadIn, ctx: Ctx, uow: Uow, storage: Storage) -> s.UploadOut:
    asset, upload = AssetService(uow, storage).create_upload(ctx, body.filename, body.content_type, body.size)
    return s.UploadOut(asset=s.AssetOut.model_validate(asset), upload=s.PresignedUploadOut.model_validate(upload))


@router.post("/assets/{asset_id}/finalize", response_model=s.AssetOut, tags=["assets"],
             summary="Validate the uploaded image and publish it at a stable public URL")
def finalize_asset(asset_id: uuid.UUID, ctx: Ctx, uow: Uow, storage: Storage) -> s.AssetOut:
    return s.AssetOut.model_validate(AssetService(uow, storage).finalize(ctx, asset_id))


@router.get("/assets", response_model=s.PageOut[s.AssetOut], tags=["assets"])
def list_assets(ctx: Ctx, uow: Uow, storage: Storage, limit: Limit = None,
                cursor: Cursor = None) -> s.PageOut[s.AssetOut]:
    page = AssetService(uow, storage).list_page(ctx, page_request(limit, cursor))
    return s.PageOut[s.AssetOut](items=[s.AssetOut.model_validate(a) for a in page.items],
                                 next_cursor=page.next_cursor)


@router.delete("/assets/{asset_id}", status_code=204, tags=["assets"],
               summary="Delete an asset (its public URL stops working)")
def delete_asset(asset_id: uuid.UUID, ctx: Ctx, uow: Uow, storage: Storage) -> Response:
    AssetService(uow, storage).delete(ctx, asset_id)
    return Response(status_code=204)


# ---- Messages ----


def message_out(message, recipients) -> s.MessageOut:
    return s.MessageOut(
        id=message.id, from_email=message.from_email, from_name=message.from_name, subject=message.subject,
        category=message.category, metadata=message.metadata_, created_at=message.created_at,
        recipients=[s.RecipientOut.model_validate(r) for r in recipients],
    )


@router.post(
    "/messages",
    status_code=202,
    response_model=s.MessageOut,
    tags=["messages"],
    summary="Send an email (API key: yes)",
    description=(
        "Queues the message durably and returns 202 with its id – before SMTP delivery. Poll GET /v1/messages/{id} "
        "for per-recipient status.\n\n"
        "* `Idempotency-Key` is required. Retrying with the same key and identical body returns the original "
        "message (202); the same key with a different body is 409 `idempotency_key_reused`.\n"
        "* Validation is all-or-nothing: any invalid, duplicate or suppressed recipient, an unapproved sender, "
        "or an unverified/disabled/foreign domain fails the whole request with 422 and per-field errors.\n"
        "* Placeholders resolved per recipient at send time: `{{unsubscribe_url}}`, `{{view_in_browser_url}}`, "
        "`{{physical_address}}`.\n"
        "* 403 `account_suspended` when sending is suspended; 429 `sending_limit_exceeded` over hourly/daily limits."
    ),
)
def send_message(
    body: s.SendIn,
    ctx: CtxOrKey,
    uow: Uow,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> s.MessageOut:
    result = SendingService(uow).send(
        ctx,
        SendRequest(
            from_email=body.from_, from_name=body.from_name, reply_to=body.reply_to, to=body.to,
            subject=body.subject, html=body.html, text=body.text, category=body.category, metadata=body.metadata,
        ),
        idempotency_key,
    )
    return message_out(result.message, result.recipients)


@router.get("/messages", response_model=s.PageOut[s.MessageSummaryOut], tags=["messages"],
            summary="Recent messages (API key: yes)")
def list_messages(ctx: CtxOrKey, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.MessageSummaryOut]:
    page = SendingService(uow).list_page(ctx, page_request(limit, cursor))
    return s.PageOut[s.MessageSummaryOut](items=[s.MessageSummaryOut.model_validate(m) for m in page.items],
                                          next_cursor=page.next_cursor)


@router.get("/messages/{message_id}", response_model=s.MessageOut, tags=["messages"],
            summary="A message with per-recipient status (API key: yes)")
def get_message(message_id: uuid.UUID, ctx: CtxOrKey, uow: Uow) -> s.MessageOut:
    message, recipients = SendingService(uow).get(ctx, message_id)
    return message_out(message, recipients)


# ---- Suppressions ----


@router.get("/suppressions", response_model=s.PageOut[s.SuppressionOut], tags=["suppressions"],
            summary="Addresses that won't receive email from this account (API key: yes)")
def list_suppressions(ctx: CtxOrKey, uow: Uow, limit: Limit = None,
                      cursor: Cursor = None) -> s.PageOut[s.SuppressionOut]:
    page = FeedbackService(uow).list_page(ctx, page_request(limit, cursor))
    return s.PageOut[s.SuppressionOut](items=[s.SuppressionOut.model_validate(x) for x in page.items],
                                       next_cursor=page.next_cursor)


@router.post("/suppressions", status_code=201, response_model=s.SuppressionOut, tags=["suppressions"],
             summary="Suppress an address manually")
def add_suppression(body: s.SuppressionIn, ctx: Ctx, uow: Uow) -> s.SuppressionOut:
    return s.SuppressionOut.model_validate(FeedbackService(uow).add_manual(ctx, body.email))


@router.delete("/suppressions/{email}", status_code=204, tags=["suppressions"],
               summary="Remove a manual suppression (others are kept for compliance)")
def remove_suppression(email: str, ctx: Ctx, uow: Uow) -> Response:
    FeedbackService(uow).remove(ctx, email)
    return Response(status_code=204)
