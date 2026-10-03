from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api import schemas as s
from app.api.deps import (
    CSRF_HEADER,
    CSRF_VALUE,
    CurrentUser,
    SessionUser,
    Uow,
    client_ip,
    get_rate_limiter,
)
from app.config import get_settings
from app.errors import ERROR_RESPONSES, Forbidden
from app.repositories.interfaces import UnitOfWork
from app.services.auth import AuthService
from app.services.common import RateLimiter

router = APIRouter(prefix="/v1/auth", tags=["auth"], responses=ERROR_RESPONSES)

CHECK_INBOX = "If that address can sign up, we've sent it an email. Check your inbox."


def csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise Forbidden("Missing the X-Requested-With: mailvender header.", code="csrf_failed")


def auth_service(uow: Uow, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]) -> AuthService:
    return AuthService(uow, limiter)


Auth = Annotated[AuthService, Depends(auth_service)]
Public = [Depends(csrf_header)]


def accounts_for(uow: UnitOfWork, user_id) -> list[s.MembershipAccountOut]:
    return [
        s.MembershipAccountOut.model_validate({**s.AccountOut.model_validate(account).model_dump(), "role": role})
        for account, role in uow.accounts.list_for_user(user_id)
    ]


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
             description="Unverified users get 403 `email_not_verified`. Rate limited per IP and per email.")
def login(body: s.LoginIn, request: Request, response: Response, auth: Auth, uow: Uow) -> s.SessionOut:
    created = auth.login(body.email, body.password, client_ip(request), request.headers.get("user-agent"))
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
                        expires_at=created.session.expires_at)


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
    return s.MeOut(user=s.UserOut.model_validate(user), accounts=accounts_for(uow, user.id))
