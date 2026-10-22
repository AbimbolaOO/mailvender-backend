import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api import schemas as s
from app import models as m
from app.api.deps import (
    CSRF_HEADER,
    CSRF_VALUE,
    CurrentUser,
    SessionUser,
    Uow,
    client_ip,
    get_rate_limiter,
    get_storage,
)
from app.config import get_settings
from app.errors import ERROR_RESPONSES, Forbidden
from app.repositories.interfaces import UnitOfWork
from app.services.auth import AuthService
from app.services.common import RateLimiter
from app.services.lifecycle import LifecycleService, deleted_accounts, purge_date
from app.services.storage import ObjectStorage
from app.services.two_factor import TwoFactorService

router = APIRouter(prefix="/v1/auth", tags=["auth"], responses=ERROR_RESPONSES)

CHECK_INBOX = "If that address can sign up, we've sent it an email. Check your inbox."


def csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise Forbidden("Missing the X-Requested-With: mailvender header.", code="csrf_failed")


def auth_service(uow: Uow, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]) -> AuthService:
    return AuthService(uow, limiter)


Auth = Annotated[AuthService, Depends(auth_service)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
Public = [Depends(csrf_header)]


def accounts_for(uow: UnitOfWork, user_id) -> list[s.MembershipAccountOut]:
    last_pages = uow.memberships.last_pages(user_id)
    return [
        s.MembershipAccountOut.model_validate({**s.AccountOut.model_validate(account).model_dump(), "role": role,
                                               "last_page_id": last_pages.get(account.id)})
        for account, role in uow.accounts.list_for_user(user_id)
    ]


def deleted_accounts_for(uow: UnitOfWork, user: m.User) -> list[s.DeletedAccountOut]:
    return [s.DeletedAccountOut.model_validate({**s.AccountOut.model_validate(a).model_dump(),
                                                "deletion_requested_at": a.deletion_requested_at,
                                                "purge_on": purge_date(a)})
            for a in deleted_accounts(uow, user)]


@router.post("/signup", status_code=202, response_model=s.Accepted, dependencies=Public,
             summary="Create an account (sends a verification link)",
             description="Rate limited per IP and per email (429 `rate_limited` with Retry-After). The response is "
                         "the same whether or not the address is already registered.")
def signup(body: s.SignupIn, request: Request, auth: Auth) -> s.Accepted:
    auth.signup(body.email, body.password, client_ip(request))
    return s.Accepted(message=CHECK_INBOX)


@router.post("/verify-email", response_model=s.UserOut, dependencies=Public,
             summary="Verify the email address; creates the personal account")
def verify_email(body: s.TokenIn, auth: Auth) -> s.UserOut:
    return s.UserOut.model_validate(auth.verify_email(body.token))


@router.post("/resend-verification", status_code=202, response_model=s.Accepted, dependencies=Public,
             summary="Send a new verification link", description="Rate limited per IP and per email.")
def resend_verification(body: s.EmailIn, request: Request, auth: Auth) -> s.Accepted:
    auth.resend_verification(body.email, client_ip(request))
    return s.Accepted(message=CHECK_INBOX)


@router.post("/login", response_model=s.SessionOut, dependencies=Public,
             summary="Sign in; sets the HttpOnly session cookie",
             description="Unverified users get 403 `email_not_verified`. With two-factor sign-in on, 401 "
                         "`totp_required` until `code` is sent (`invalid_code` when it's wrong). Rate limited per IP "
                         "and per email.")
def login(body: s.LoginIn, request: Request, response: Response, auth: Auth, uow: Uow) -> s.SessionOut:
    created = auth.login(body.email, body.password, client_ip(request), request.headers.get("user-agent"), body.code)
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        created.token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )
    return s.SessionOut(user=s.UserOut.model_validate(created.user), accounts=accounts_for(uow, created.user.id),
                        deleted_accounts=deleted_accounts_for(uow, created.user), expires_at=created.session.expires_at)


@router.post("/logout", status_code=204, summary="Sign out (revokes this session)")
def logout(auth_state: SessionUser, response: Response, auth: Auth) -> Response:
    auth.logout(auth_state[1])
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    response.status_code = 204
    return response


@router.post("/password-reset/request", status_code=202, response_model=s.Accepted, dependencies=Public,
             summary="Email a password-reset link",
             description="Same response whether or not the address is registered. Rate limited per IP and email.")
def request_reset(body: s.EmailIn, request: Request, auth: Auth) -> s.Accepted:
    auth.request_password_reset(body.email, client_ip(request))
    return s.Accepted(message="If that address has an account, we've sent it a reset link.")


@router.post("/password-reset/confirm", status_code=204, dependencies=Public,
             summary="Set a new password (signs out every session)")
def confirm_reset(body: s.ResetConfirmIn, auth: Auth) -> Response:
    auth.reset_password(body.token, body.password)
    return Response(status_code=204)


@router.get("/me", response_model=s.MeOut, summary="The signed-in user and their accounts")
def me(user: CurrentUser, uow: Uow) -> s.MeOut:
    return s.MeOut(user=s.UserOut.model_validate(user), accounts=accounts_for(uow, user.id),
                   deleted_accounts=deleted_accounts_for(uow, user))


@router.post("/two-factor/setup", response_model=s.TwoFactorSetupOut,
             summary="Start turning on two-factor sign-in: a new secret for the authenticator app")
def start_two_factor(user: CurrentUser, uow: Uow) -> s.TwoFactorSetupOut:
    secret, uri = TwoFactorService(uow).start(user)
    return s.TwoFactorSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/two-factor/confirm", response_model=s.RecoveryCodesOut,
             summary="Finish turning it on with a code from the app; returns the recovery codes once")
def confirm_two_factor(body: s.TwoFactorCodeIn, user: CurrentUser, uow: Uow) -> s.RecoveryCodesOut:
    return s.RecoveryCodesOut(recovery_codes=TwoFactorService(uow).confirm(user, body.code))


@router.post("/two-factor/disable", status_code=204, summary="Turn two-factor sign-in off (password and a code)")
def disable_two_factor(body: s.TwoFactorDisableIn, user: CurrentUser, uow: Uow) -> Response:
    TwoFactorService(uow).disable(user, body.password, body.code)
    return Response(status_code=204)


@router.post("/two-factor/recovery-codes", response_model=s.RecoveryCodesOut,
             summary="Replace the recovery codes (needs the password; the old ones stop working)")
def replace_recovery_codes(body: s.PasswordConfirmIn, user: CurrentUser, uow: Uow) -> s.RecoveryCodesOut:
    return s.RecoveryCodesOut(recovery_codes=TwoFactorService(uow).replace_recovery_codes(user, body.password))


@router.post("/password", status_code=204,
             summary="Change your password while signed in (every other device is signed out)",
             description="422 `invalid_password` on `current_password` when it's wrong; the new password has "
                         "the same rules as sign-up. An email tells the user it changed.")
def change_password(body: s.PasswordChangeIn, auth_state: SessionUser, auth: Auth) -> Response:
    user, session = auth_state
    auth.change_password(user, session, body.current_password, body.new_password)
    return Response(status_code=204)


@router.get("/sessions", response_model=list[s.SessionInfoOut], summary="Devices where you're signed in")
def list_sessions(auth_state: SessionUser, auth: Auth) -> list[s.SessionInfoOut]:
    user, current = auth_state
    return [s.SessionInfoOut.model_validate(x).model_copy(update={"current": x.id == current.id})
            for x in auth.sessions(user)]


@router.post("/sessions/revoke-others", response_model=s.RevokedOut,
             summary="Sign out every other device (this one stays signed in)")
def revoke_other_sessions(auth_state: SessionUser, auth: Auth) -> s.RevokedOut:
    user, current = auth_state
    return s.RevokedOut(revoked=auth.revoke_other_sessions(user, current))


@router.delete("/sessions/{session_id}", status_code=204,
               summary="Sign out one other device",
               description="409 `current_session` for this device (use `/v1/auth/logout`).")
def revoke_session(session_id: uuid.UUID, auth_state: SessionUser, auth: Auth) -> Response:
    user, current = auth_state
    auth.revoke_session(user, current, session_id)
    return Response(status_code=204)


@router.post("/me/export", summary="Download your personal data (ZIP of JSON; audited)",
             description="Your profile, the accounts you belong to, invitations, sessions and your activity. "
                         "Workspace content is in each account's export (`POST /v1/account/exports`).",
             response_class=Response, responses={200: {"content": {"application/zip": {}}}})
def export_me(user: CurrentUser, uow: Uow, storage: Storage) -> Response:
    body = LifecycleService(uow, storage).personal_export(user)
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="mailvender-my-data.zip"',
                             "Cache-Control": "no-store"})


@router.post("/me/deletion", status_code=204, summary="Delete your login permanently",
             description="Needs your password. Accounts only you own are deleted (restorable by nobody, erased "
                         "after the retention period); you leave every other account; your email address and "
                         "password are erased and every session ends. 409 `owns_shared_accounts` while you own "
                         "an account with other members (`details.accounts`): transfer or delete it first.")
def delete_me(body: s.UserDeletionIn, user: CurrentUser, uow: Uow, storage: Storage, response: Response) -> Response:
    LifecycleService(uow, storage).delete_user(user, body.password)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    response.status_code = 204
    return response
