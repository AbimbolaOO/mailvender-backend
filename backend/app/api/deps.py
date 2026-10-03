"""FastAPI dependencies: unit of work, infrastructure adapters and authentication.

Authentication:

* Browser: the `mv_session` cookie (HttpOnly, Secure, SameSite=Lax). Unsafe
  requests (POST/PUT/PATCH/DELETE) must also send `X-Requested-With: mailvender`
  – a custom header a cross-site form can't add (CSRF protection).
* API: `Authorization: Bearer mv_…` with a per-account API key. Only endpoints
  marked "API key: yes" in their description accept it.

Account context: browser requests name the active account in `X-Account-Id`;
the API checks membership on every request. An API key is bound to its account.
The account is never taken from the request body.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request

from app import models as m
from app.config import get_settings
from app.db import get_session_factory
from app.errors import Forbidden, Unauthenticated, ValidationFailed, field_error
from app.logging import user_id_var
from app.repositories.interfaces import UnitOfWork
from app.repositories.sql import SqlUnitOfWork
from app.security import API_KEY_PREFIX
from app.services.accounts import AccountService
from app.services.auth import AuthService
from app.services.common import AccountContext, RateLimiter, UnitOfWorkFactory
from app.services.dns import DnsResolver, SystemDnsResolver
from app.services.storage import ObjectStorage, S3Storage

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "mailvender"


# ---- Infrastructure (overridden in tests) ----


@contextmanager
def _sql_uow() -> Iterator[UnitOfWork]:
    session = get_session_factory()()
    try:
        yield SqlUnitOfWork(session)
    finally:
        session.close()


def get_uow_factory() -> UnitOfWorkFactory:
    return _sql_uow


def get_uow(factory: Annotated[UnitOfWorkFactory, Depends(get_uow_factory)]) -> Iterator[UnitOfWork]:
    # Anything not committed by a service is rolled back when the session closes.
    with factory() as uow:
        yield uow


@lru_cache
def _storage() -> S3Storage:
    return S3Storage()


def get_storage() -> ObjectStorage:
    return _storage()


def get_resolver() -> DnsResolver:
    return SystemDnsResolver()


def get_rate_limiter(factory: Annotated[UnitOfWorkFactory, Depends(get_uow_factory)]) -> RateLimiter:
    return RateLimiter(factory)


Uow = Annotated[UnitOfWork, Depends(get_uow)]


def client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---- Authentication ----


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _check_csrf(request: Request) -> None:
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise Forbidden("Missing the X-Requested-With: mailvender header.", code="csrf_failed")


def session_user(request: Request, uow: Uow) -> tuple[m.User, m.Session]:
    if _bearer(request):
        raise Forbidden("This endpoint needs a signed-in user, not an API key.", code="user_required")
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise Unauthenticated()
    user, session = AuthService(uow, RateLimiter(_sql_uow)).authenticate(token)
    _check_csrf(request)
    user_id_var.set(str(user.id))
    request.state.user_id = str(user.id)
    return user, session


SessionUser = Annotated[tuple[m.User, m.Session], Depends(session_user)]


def current_user(auth: SessionUser) -> m.User:
    return auth[0]


CurrentUser = Annotated[m.User, Depends(current_user)]


def _account_header(value: str | None) -> uuid.UUID:
    if not value:
        raise ValidationFailed([field_error("X-Account-Id", "Choose an account (X-Account-Id header).", "required")])
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValidationFailed([field_error("X-Account-Id", "Not a valid account id.", "invalid")]) from error


def account_context(
    request: Request,
    uow: Uow,
    x_account_id: Annotated[str | None, Header(description="Active account (browser sessions)")] = None,
) -> AccountContext:
    """Session user with membership of `X-Account-Id` (no API keys)."""
    user, _ = session_user(request, uow)
    ctx = AccountService(uow).context_for_user(user, _account_header(x_account_id))
    request.state.account_id = str(ctx.account_id)
    return ctx


def account_context_or_key(
    request: Request,
    uow: Uow,
    x_account_id: Annotated[str | None, Header(description="Active account (browser sessions)")] = None,
) -> AccountContext:
    """Session user with membership, or an active API key of the account."""
    token = _bearer(request)
    if token is None:
        return account_context(request, uow, x_account_id)
    if not token.startswith(API_KEY_PREFIX):
        raise Unauthenticated("This API key is invalid or revoked.", code="api_key_invalid")
    ctx = AccountService(uow).context_for_api_key(token)
    if x_account_id and _account_header(x_account_id) != ctx.account_id:
        raise Forbidden("This API key belongs to a different account.", code="account_mismatch")
    request.state.account_id = str(ctx.account_id)
    return ctx


Ctx = Annotated[AccountContext, Depends(account_context)]
CtxOrKey = Annotated[AccountContext, Depends(account_context_or_key)]


def require_owner(ctx: Ctx) -> AccountContext:
    ctx.require_owner()
    return ctx


OwnerCtx = Annotated[AccountContext, Depends(require_owner)]


def operator_user(user: CurrentUser) -> m.User:
    if not user.is_operator:
        raise Forbidden("Operators only.", code="operator_required")
    return user


Operator = Annotated[m.User, Depends(operator_user)]
