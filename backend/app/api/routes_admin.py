"""The admin API (/v1/admin/*) used by the Mailvender Admin app (mailvender-admin/).

Only reachable through the admin app's server: every request must carry X-Mailvender-Admin-Secret
(ADMIN_PROXY_SECRET); anything else gets a 404, so the routes don't appear to exist. Admins sign in
with their own accounts and cookie (see services/admin_auth.py). Not part of the public
/openapi.json – `python -m app.cli openapi --admin` prints this contract.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from app import models as m
from app.api import schemas as s
from app.api import schemas_admin as a
from app.api.deps import (
    Admin,
    AnyAdmin,
    Uow,
    admin_client_ip,
    get_rate_limiter,
    get_storage,
    require_admin_secret,
)
from app.config import get_settings
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT, page_request
from app.services.admin_auth import AdminAuth, AdminAuthService
from app.services.admin_customers import (
    AccountDetail,
    AdminCustomersService,
    UserDetail,
    UserRow,
    account_status,
    mask_email,
    user_status,
)
from app.services.admin_dashboard import AdminDashboardService, Port25Probe, port25_open
from app.services.admin_issues import TYPES, AdminIssuesService
from app.services.common import RateLimiter
from app.services.inbound import InboundService
from app.services.storage import ObjectStorage

router = APIRouter(prefix="/v1/admin", tags=["admin"], responses=ERROR_RESPONSES,
                   dependencies=[Depends(require_admin_secret)])

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1000)]
Search = Annotated[str | None, Query(max_length=200)]
Days = Annotated[int, Query(ge=7, le=90)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]


def get_port25_probe() -> Port25Probe:
    return port25_open


Probe = Annotated[Port25Probe, Depends(get_port25_probe)]


# ---- Sign-in ----


def admin_out(admin: m.AdminUser, auth: AdminAuth | None = None) -> a.AdminOut:
    settings = get_settings()
    next_step = auth.next_step if auth else None
    return a.AdminOut(id=admin.id, email=admin.email, two_factor_enabled=admin.totp_enabled_at is not None,
                      recovery_codes_left=len(admin.recovery_code_hashes or []), next_step=next_step,
                      last_login_at=admin.last_login_at,
                      limits=a.LimitsOut(bounce_rate=settings.health_max_bounce_rate,
                                         complaint_rate=settings.health_max_complaint_rate,
                                         gmail_spam_warning=0.001, gmail_spam_limit=0.003))


@router.post("/auth/login", response_model=a.AdminSessionOut, summary="Sign in (sets the admin session cookie)",
             description="Password, then `code` (authenticator or recovery code) once two-factor is set up: "
                         "without it the answer is 401 `totp_required`. Wrong details are 401 "
                         "`invalid_credentials`; repeated failures lock the admin for a while.")
def login(body: a.AdminLoginIn, request: Request, response: Response, uow: Uow,
          limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]) -> a.AdminSessionOut:
    settings = get_settings()
    created = AdminAuthService(uow, limiter).login(body.email, body.password, body.code, admin_client_ip(request),
                                                   request.headers.get("user-agent"))
    response.set_cookie(
        settings.admin_cookie_name,
        created.token,
        max_age=settings.admin_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    auth = AdminAuth(admin=created.admin, session=created.session, ip="")
    return a.AdminSessionOut(admin=admin_out(created.admin, auth), expires_at=created.session.expires_at,
                             idle_minutes=settings.admin_session_idle_minutes)


@router.post("/auth/logout", status_code=204, summary="Sign out")
def logout(auth: AnyAdmin, response: Response, uow: Uow) -> Response:
    AdminAuthService(uow).logout(auth)
    response.delete_cookie(get_settings().admin_cookie_name, path="/", secure=get_settings().session_cookie_secure,
                           httponly=True, samesite="strict")
    response.status_code = 204
    return response


@router.get("/auth/me", response_model=a.AdminOut, summary="The signed-in admin and what they must do next")
def me(auth: AnyAdmin) -> a.AdminOut:
    return admin_out(auth.admin, auth)


@router.post("/auth/password", response_model=a.AdminOut, summary="Change the password (required on first sign-in)")
def change_password(body: a.PasswordChangeIn, auth: AnyAdmin, uow: Uow) -> a.AdminOut:
    AdminAuthService(uow).change_password(auth, body.current_password, body.new_password)
    return admin_out(auth.admin, auth)


@router.post("/auth/two-factor/setup", response_model=a.TwoFactorSetupOut,
             summary="Start two-factor setup: a new secret for the authenticator app")
def start_two_factor(auth: AnyAdmin, uow: Uow) -> a.TwoFactorSetupOut:
    secret, uri = AdminAuthService(uow).start_two_factor(auth)
    return a.TwoFactorSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/auth/two-factor/confirm", response_model=a.RecoveryCodesOut,
             summary="Finish two-factor setup with a code from the app; returns the recovery codes once")
def confirm_two_factor(body: a.CodeIn, auth: AnyAdmin, uow: Uow) -> a.RecoveryCodesOut:
    return a.RecoveryCodesOut(recovery_codes=AdminAuthService(uow).confirm_two_factor(auth, body.code))


# ---- Overview, deliverability, system ----


@router.get("/overview", response_model=a.OverviewOut, summary="Today's numbers, daily charts and system checks")
def overview(_: Admin, uow: Uow, storage: Storage, probe: Probe, days: Days = 30) -> a.OverviewOut:
    return a.OverviewOut.model_validate(AdminDashboardService(uow, storage, probe).overview(days))


@router.get("/deliverability", response_model=a.DeliverabilityOut,
            summary="Spam complaint and bounce rates: platform, by mailbox provider, by account; DMARC results")
def deliverability(_: Admin, uow: Uow, storage: Storage, probe: Probe, days: Days = 30) -> a.DeliverabilityOut:
    return a.DeliverabilityOut.model_validate(AdminDashboardService(uow, storage, probe).deliverability(days))


@router.get("/system", response_model=a.SystemOut, summary="Jobs, queue, platform DKIM, backups and port 25")
def system(_: Admin, uow: Uow, storage: Storage, probe: Probe) -> a.SystemOut:
    return a.SystemOut.model_validate(AdminDashboardService(uow, storage, probe).system())


# ---- Users ----


def user_row(row: UserRow) -> a.AdminUserRowOut:
    user = row.user
    return a.AdminUserRowOut(id=user.id, email=user.email, status=row.status, created_at=user.created_at,
                             email_verified_at=user.email_verified_at, disabled_at=user.disabled_at,
                             accounts=row.accounts, last_seen_at=row.last_seen_at, sent_30d=row.sent_30d)


def user_detail_out(detail: UserDetail) -> a.AdminUserDetailOut:
    row = UserRow(detail.user, len(detail.accounts), detail.last_seen_at, 0)
    return a.AdminUserDetailOut(
        user=user_row(row),
        accounts=[a.UserAccountOut(account=s.AccountOut.model_validate(account), role=role,
                                   status=account_status(account)) for account, role in detail.accounts],
        active_sessions=detail.active_sessions,
    )


@router.get("/users", response_model=a.PageOut[a.AdminUserRowOut], summary="Users (search by email, filter by status)")
def users(_: Admin, uow: Uow, q: Search = None, status: Literal["active", "blocked", "unverified"] | None = None,
          limit: Limit = None, cursor: Cursor = None) -> a.PageOut[a.AdminUserRowOut]:
    page = AdminCustomersService(uow).users_page(page_request(limit, cursor), q, status)
    return a.PageOut[a.AdminUserRowOut](items=[user_row(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/users/{user_id}", response_model=a.AdminUserDetailOut, summary="One user (the view is audited)")
def user(user_id: uuid.UUID, auth: Admin, uow: Uow) -> a.AdminUserDetailOut:
    return user_detail_out(AdminCustomersService(uow).user_detail(auth, user_id))


@router.post("/users/{user_id}/block", response_model=a.AdminUserDetailOut,
             summary="Block a user: signs them out everywhere; optionally suspends the accounts they own")
def block_user(user_id: uuid.UUID, body: a.BlockIn, auth: Admin, uow: Uow) -> a.AdminUserDetailOut:
    return user_detail_out(AdminCustomersService(uow).block_user(auth, user_id, body.reason, body.suspend_accounts))


@router.post("/users/{user_id}/unblock", response_model=a.AdminUserDetailOut,
             summary="Unblock a user (their accounts stay suspended until reinstated)")
def unblock_user(user_id: uuid.UUID, body: a.DecisionIn, auth: Admin, uow: Uow) -> a.AdminUserDetailOut:
    return user_detail_out(AdminCustomersService(uow).unblock_user(auth, user_id, body.reason))


@router.post("/users/{user_id}/resend-verification", response_model=a.AdminUserDetailOut,
             summary="Email a new verification link to an unverified user")
def resend_verification(user_id: uuid.UUID, auth: Admin, uow: Uow) -> a.AdminUserDetailOut:
    return user_detail_out(AdminCustomersService(uow).resend_verification(auth, user_id))


# ---- Accounts ----


def account_detail_out(detail: AccountDetail) -> a.AdminAccountDetailOut:
    account = detail.health.account
    return a.AdminAccountDetailOut(
        account=s.AccountOut.model_validate(account),
        status=account_status(account),
        owner_email=detail.owner_email,
        metrics=s.HealthMetricsOut.model_validate(detail.health.metrics),
        latest_decision=s.AuditEventOut.model_validate(detail.health.latest_decision)
        if detail.health.latest_decision else None,
        members=[a.MemberRowOut(user_id=user.id, email=user.email, role=role, status=user_status(user))
                 for user, role in detail.members],
        domains=[a.AdminDomainOut(id=view.domain.id, name=view.domain.name, status=view.domain.status,
                                  bulk_eligible=view.domain.bulk_eligible, verified_at=view.domain.verified_at,
                                  last_checked_at=view.domain.last_checked_at,
                                  records=[s.DnsRecordOut.model_validate(r) for r in view.records])
                 for view in detail.domains],
        senders=[s.SenderIdentityOut.model_validate(x) for x in detail.senders],
        api_keys=[s.ApiKeyOut.model_validate(k) for k in detail.api_keys],
        recent_events=[a.DeliveryEventOut(type=e.type, classification=e.classification, status_code=e.status_code,
                                          diagnostic=(e.diagnostic or "")[:300] or None, email=mask_email(e.email),
                                          created_at=e.created_at) for e in detail.recent_events],
        suppressions=detail.suppressions,
        audit=[s.AuditEventOut.model_validate(e) for e in detail.audit],
    )


@router.get("/accounts", response_model=a.PageOut[a.AdminAccountRowOut],
            summary="Accounts (search by name or owner email, filter by status)")
def accounts(_: Admin, uow: Uow, q: Search = None, status: Literal["active", "suspended", "deleted"] | None = None,
             limit: Limit = None, cursor: Cursor = None) -> a.PageOut[a.AdminAccountRowOut]:
    page = AdminCustomersService(uow).accounts_page(page_request(limit, cursor), q, status)
    return a.PageOut[a.AdminAccountRowOut](
        items=[a.AdminAccountRowOut(account=s.AccountOut.model_validate(r.health.account),
                                    status=account_status(r.health.account), owner_email=r.owner_email,
                                    members=r.members, verified_domains=r.verified_domains, sent_today=r.sent_today,
                                    metrics=s.HealthMetricsOut.model_validate(r.health.metrics))
               for r in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/accounts/{account_id}", response_model=a.AdminAccountDetailOut, summary="One account (audited)")
def account(account_id: uuid.UUID, auth: Admin, uow: Uow) -> a.AdminAccountDetailOut:
    return account_detail_out(AdminCustomersService(uow).account_detail(auth, account_id))


@router.post("/accounts/{account_id}/suspend", response_model=a.AdminAccountDetailOut, summary="Suspend sending")
def suspend_account(account_id: uuid.UUID, body: a.DecisionIn, auth: Admin, uow: Uow) -> a.AdminAccountDetailOut:
    return account_detail_out(AdminCustomersService(uow).decide(auth, account_id, "suspend", body.reason))


@router.post("/accounts/{account_id}/reinstate", response_model=a.AdminAccountDetailOut,
             summary="Reinstate sending")
def reinstate_account(account_id: uuid.UUID, body: a.DecisionIn, auth: Admin, uow: Uow) -> a.AdminAccountDetailOut:
    return account_detail_out(AdminCustomersService(uow).decide(auth, account_id, "reinstate", body.reason))


@router.put("/accounts/{account_id}/limits", response_model=a.AdminAccountDetailOut,
            summary="Change hourly/daily sending limits")
def account_limits(account_id: uuid.UUID, body: a.AdminLimitsIn, auth: Admin, uow: Uow) -> a.AdminAccountDetailOut:
    return account_detail_out(AdminCustomersService(uow).decide(
        auth, account_id, "limits", body.reason, body.hourly_recipient_limit, body.daily_recipient_limit))


@router.post("/accounts/{account_id}/reviews", response_model=a.AdminAccountDetailOut,
             summary="Record a review (a note on the account's history)")
def review_account(account_id: uuid.UUID, body: a.DecisionIn, auth: Admin, uow: Uow) -> a.AdminAccountDetailOut:
    return account_detail_out(AdminCustomersService(uow).decide(auth, account_id, "review", body.reason))


# ---- Needs help ----


@router.get("/issues", response_model=a.IssuesOut, summary="Customers who need help, worst first, with how to fix it")
def issues(_: Admin, uow: Uow, type: str | None = Query(None, max_length=40)) -> a.IssuesOut:
    items, counts = AdminIssuesService(uow).find(type)
    return a.IssuesOut(
        items=[a.IssueOut(key=i.key, type=i.type, label=i.label, severity=i.severity, who=i.who, problem=i.problem,
                          how_to_fix=i.how_to_fix, details=i.details, since=i.since, user_id=i.user_id,
                          account_id=i.account_id, contact_email=i.contact_email, action=i.action, state=i.state,
                          note=i.note, state_updated_at=i.state_updated_at, reopened=i.reopened) for i in items],
        types=[a.IssueTypeOut(type=name, label=label, severity=severity, count=counts.get(name, 0))
               for name, (severity, label) in TYPES.items()],
    )


@router.post("/issues/state", status_code=204, summary="Mark a problem contacted / resolved / open, with a note")
def issue_state(body: a.IssueStateIn, auth: Admin, uow: Uow) -> Response:
    AdminIssuesService(uow).set_state(auth, body.key, body.status, body.note)
    return Response(status_code=204)


# ---- Inbox (postmaster@, abuse@, dmarc@) ----


def inbox_out(message: m.InboundMessage) -> a.InboxMessageOut:
    return a.InboxMessageOut.model_validate(message)


@router.get("/inbox", response_model=a.PageOut[a.InboxMessageOut], summary="Messages in one mailbox")
def inbox(_: Admin, uow: Uow, storage: Storage, mailbox: a.Mailbox = "postmaster", q: Search = None,
          state: Literal["inbox", "unread", "archived"] = "inbox", limit: Limit = None,
          cursor: Cursor = None) -> a.PageOut[a.InboxMessageOut]:
    page = InboundService(uow, storage).list_page(mailbox, page_request(limit, cursor), q, state)
    return a.PageOut[a.InboxMessageOut](items=[inbox_out(x) for x in page.items], next_cursor=page.next_cursor)


@router.get("/inbox/counts", response_model=a.InboxCountsOut, summary="Unread messages per mailbox")
def inbox_counts(_: Admin, uow: Uow, storage: Storage) -> a.InboxCountsOut:
    return a.InboxCountsOut(unread=InboundService(uow, storage).unread_counts())


@router.get("/inbox/{message_id}", response_model=a.InboxMessageDetailOut,
            summary="One message: headers, text, attachments (marks it read; audited)")
def inbox_message(message_id: uuid.UUID, auth: Admin, uow: Uow, storage: Storage) -> a.InboxMessageDetailOut:
    detail = InboundService(uow, storage).detail(auth, message_id)
    return a.InboxMessageDetailOut(
        message=inbox_out(detail.message),
        headers=[a.HeaderOut(name=n, value=v) for n, v in detail.headers],
        text=detail.text,
        has_html=detail.has_html,
        attachments=[a.AttachmentOut(index=x.index, filename=x.filename, content_type=x.content_type, size=x.size)
                     for x in detail.attachments],
        dmarc_rows=[a.DmarcRowOut.model_validate(r) for r in detail.dmarc_rows],
    )


@router.get("/inbox/{message_id}/body", response_class=Response,
            summary="The message as an HTML document for a sandboxed iframe (scripts never run; remote images "
                    "only with images=true)")
def inbox_body(message_id: uuid.UUID, _: Admin, uow: Uow, storage: Storage, images: bool = False) -> Response:
    document, csp = InboundService(uow, storage).body_document(message_id, images)
    return Response(document, media_type="text/html; charset=utf-8", headers={
        "Content-Security-Policy": csp,
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })


@router.get("/inbox/{message_id}/attachments/{index}", response_class=Response,
            summary="Download one attachment (never shown inline)")
def inbox_attachment(message_id: uuid.UUID, index: int, _: Admin, uow: Uow, storage: Storage) -> Response:
    filename, data = InboundService(uow, storage).attachment(message_id, index)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


@router.get("/inbox/{message_id}/raw", response_class=Response, summary="Download the original .eml (audited)")
def inbox_raw(message_id: uuid.UUID, auth: Admin, uow: Uow, storage: Storage) -> Response:
    data = InboundService(uow, storage).raw(auth, message_id)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{message_id}.eml"',
        "Cache-Control": "no-store",
    })


@router.post("/inbox/{message_id}", response_model=a.InboxMessageOut, summary="Mark read/unread, archive/unarchive")
def inbox_mark(message_id: uuid.UUID, body: a.InboxMarkIn, auth: Admin, uow: Uow,
               storage: Storage) -> a.InboxMessageOut:
    return inbox_out(InboundService(uow, storage).mark(auth, message_id, read=body.read, archived=body.archived))


# ---- Audit log ----


@router.get("/audit", response_model=a.PageOut[s.AuditEventOut], summary="Everything admins did, newest first")
def audit_log(_: Admin, uow: Uow, admin_id: uuid.UUID | None = None,
              action: str | None = Query(None, max_length=64), limit: Limit = None,
              cursor: Cursor = None) -> a.PageOut[s.AuditEventOut]:
    page = AdminCustomersService(uow).audit_page(page_request(limit, cursor), admin_id, action)
    return a.PageOut[s.AuditEventOut](items=[s.AuditEventOut.model_validate(e) for e in page.items],
                                      next_cursor=page.next_cursor)
