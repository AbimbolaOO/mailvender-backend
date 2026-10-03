import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api import schemas as s
from app.api.deps import Ctx, CurrentUser, OwnerCtx, Uow
from app.api.routes_auth import accounts_for
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT, page_request
from app.services.accounts import AccountService

router = APIRouter(prefix="/v1", tags=["accounts"], responses=ERROR_RESPONSES)

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=500)]


@router.get("/accounts", response_model=list[s.MembershipAccountOut], summary="Accounts you belong to")
def list_accounts(user: CurrentUser, uow: Uow) -> list[s.MembershipAccountOut]:
    # Bounded by memberships per user; personal account first.
    return accounts_for(uow, user.id)


@router.post("/accounts", status_code=201, response_model=s.MembershipAccountOut,
             summary="Create a team account (you become its owner)")
def create_account(body: s.AccountCreateIn, user: CurrentUser, uow: Uow) -> s.MembershipAccountOut:
    account = AccountService(uow).create_team(user, body.name)
    return s.MembershipAccountOut.model_validate({**s.AccountOut.model_validate(account).model_dump(), "role": "owner"})


@router.get("/account", response_model=s.MembershipAccountOut, summary="The active account (X-Account-Id)")
def get_account(ctx: Ctx) -> s.MembershipAccountOut:
    return s.MembershipAccountOut.model_validate({**s.AccountOut.model_validate(ctx.account).model_dump(),
                                                  "role": ctx.role})


@router.patch("/account", response_model=s.AccountOut, summary="Rename or set sending settings (postal address)")
def update_account(body: s.AccountUpdateIn, ctx: Ctx, uow: Uow) -> s.AccountOut:
    return s.AccountOut.model_validate(
        AccountService(uow).update_settings(ctx, name=body.name, physical_address=body.physical_address)
    )


@router.get("/account/members", response_model=s.PageOut[s.MemberOut])
def list_members(ctx: Ctx, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.MemberOut]:
    page = AccountService(uow).list_members(ctx, page_request(limit, cursor))
    return s.PageOut[s.MemberOut](
        items=[s.MemberOut(user_id=user.id, email=user.email, role=membership.role,  # type: ignore[arg-type]
                           created_at=membership.created_at) for membership, user in page.items],
        next_cursor=page.next_cursor,
    )


@router.delete("/account/members/{user_id}", status_code=204,
               summary="Remove a member (owners), or yourself (leave)",
               description="The last owner can't be removed (409 `last_owner`).")
def remove_member(user_id: uuid.UUID, ctx: Ctx, uow: Uow) -> Response:
    AccountService(uow).remove_member(ctx, user_id)
    return Response(status_code=204)


@router.post("/account/leave", status_code=204, summary="Leave the active account")
def leave(ctx: Ctx, uow: Uow) -> Response:
    AccountService(uow).leave(ctx)
    return Response(status_code=204)


@router.post("/account/transfer-ownership", status_code=204, summary="Make another member the owner (owners)")
def transfer(body: s.TransferIn, ctx: OwnerCtx, uow: Uow) -> Response:
    AccountService(uow).transfer_ownership(ctx, body.user_id)
    return Response(status_code=204)


@router.post("/account/invitations", status_code=201, response_model=s.InvitationOut, summary="Invite by email (owners)")
def invite(body: s.InvitationIn, ctx: OwnerCtx, uow: Uow) -> s.InvitationOut:
    return s.InvitationOut.model_validate(AccountService(uow).invite(ctx, body.email))


@router.get("/account/invitations", response_model=s.PageOut[s.InvitationOut], summary="Pending invitations (owners)")
def list_invitations(ctx: OwnerCtx, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.InvitationOut]:
    page = AccountService(uow).list_invitations(ctx, page_request(limit, cursor))
    return s.PageOut[s.InvitationOut](items=[s.InvitationOut.model_validate(i) for i in page.items],
                                      next_cursor=page.next_cursor)


@router.delete("/account/invitations/{invitation_id}", status_code=204, summary="Revoke an invitation (owners)")
def revoke_invitation(invitation_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> Response:
    AccountService(uow).revoke_invitation(ctx, invitation_id)
    return Response(status_code=204)


@router.post("/invitations/accept", response_model=s.MembershipAccountOut,
             summary="Accept an invitation (signed in with the invited email)")
def accept_invitation(body: s.TokenIn, user: CurrentUser, uow: Uow) -> s.MembershipAccountOut:
    account = AccountService(uow).accept_invitation(user, body.token)
    return s.MembershipAccountOut.model_validate({**s.AccountOut.model_validate(account).model_dump(),
                                                  "role": "member"})


@router.post("/account/api-keys", status_code=201, response_model=s.ApiKeyCreatedOut,
             summary="Create an API key (owners) – the secret is shown once")
def create_api_key(body: s.ApiKeyIn, ctx: OwnerCtx, uow: Uow) -> s.ApiKeyCreatedOut:
    created = AccountService(uow).create_api_key(ctx, body.name)
    return s.ApiKeyCreatedOut.model_validate({**s.ApiKeyOut.model_validate(created.key).model_dump(),
                                              "key": created.plaintext})


@router.get("/account/api-keys", response_model=s.PageOut[s.ApiKeyOut], summary="API key metadata (owners)")
def list_api_keys(ctx: OwnerCtx, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.ApiKeyOut]:
    page = AccountService(uow).list_api_keys(ctx, page_request(limit, cursor))
    return s.PageOut[s.ApiKeyOut](items=[s.ApiKeyOut.model_validate(k) for k in page.items],
                                  next_cursor=page.next_cursor)


@router.delete("/account/api-keys/{key_id}", response_model=s.ApiKeyOut, summary="Revoke an API key (owners)")
def revoke_api_key(key_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> s.ApiKeyOut:
    return s.ApiKeyOut.model_validate(AccountService(uow).revoke_api_key(ctx, key_id))


@router.get("/account/audit-events", response_model=s.PageOut[s.AuditEventOut], summary="Audit log (owners)")
def audit_events(ctx: OwnerCtx, uow: Uow, limit: Limit = None, cursor: Cursor = None) -> s.PageOut[s.AuditEventOut]:
    page = AccountService(uow).audit_log(ctx, page_request(limit, cursor))
    return s.PageOut[s.AuditEventOut](items=[s.AuditEventOut.model_validate(e) for e in page.items],
                                      next_cursor=page.next_cursor)
