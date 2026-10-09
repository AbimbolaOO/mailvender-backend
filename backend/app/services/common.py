"""Building blocks shared by the application services."""

import logging
import math
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from email_validator import EmailNotValidError, validate_email

from app import models as m
from app.config import get_settings
from app.errors import Forbidden, RateLimited, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.emails import RenderedEmail

log = get_logger("services")

UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]


# ---- Who is acting ----


@dataclass(frozen=True)
class Actor:
    type: str  # user | api_key | system | operator | public
    id: uuid.UUID | None = None

    @staticmethod
    def system() -> "Actor":
        return Actor("system")


@dataclass
class AccountContext:
    """The active account of an authenticated request (session or API key)."""

    account: m.Account
    role: str
    user: m.User | None = None
    api_key: m.ApiKey | None = None

    @property
    def account_id(self) -> uuid.UUID:
        return self.account.id

    @property
    def actor(self) -> Actor:
        if self.api_key:
            return Actor("api_key", self.api_key.id)
        return Actor("user", self.user.id if self.user else None)

    @property
    def is_owner(self) -> bool:
        return self.role == "owner" and self.api_key is None

    def require_owner(self) -> None:
        if not self.is_owner:
            raise Forbidden("Only account owners can do that.", code="owner_required")

    def require_user(self) -> m.User:
        if self.user is None or self.api_key is not None:
            raise Forbidden("This action needs a signed-in user, not an API key.", code="user_required")
        return self.user


# ---- Audit ----


def audit(
    uow: UnitOfWork,
    account_id: uuid.UUID | None,
    actor: Actor,
    action: str,
    *,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    reason: str | None = None,
    **data: Any,
) -> m.AuditEvent:
    event = uow.audit.add(
        m.AuditEvent(
            account_id=account_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            data={key: value for key, value in data.items() if value is not None},
        )
    )
    log_event(log, f"audit.{action}", audit_id=str(event.id), target_id=str(target_id) if target_id else None)
    return event


# ---- Email addresses ----


def normalize_email(value: str, field: str = "email") -> str:
    """Validated, trimmed address with lowercased domain and local part.

    Mailvender treats addresses case-insensitively (as virtually every mailbox
    provider does) so one person can't hold two accounts or dodge a suppression
    by changing case.
    """
    try:
        result = validate_email(value.strip(), check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as error:
        raise ValidationFailed([field_error(field, "Enter a valid email address.", "invalid_email")]) from error
    return result.normalized.lower()


def try_normalize_email(value: str) -> str | None:
    try:
        return validate_email(value.strip(), check_deliverability=False, allow_smtputf8=False).normalized.lower()
    except EmailNotValidError:
        return None


# ---- Rate limits ----


@dataclass(frozen=True)
class Limit:
    count: int
    seconds: int

    @staticmethod
    def parse(spec: str) -> "Limit":
        count, _, seconds = spec.partition("/")
        return Limit(int(count), int(seconds))


class RateLimiter:
    """Fixed-window counters in Postgres, committed independently of the request.

    A request that fails afterwards still counts, which is what makes the limit
    effective against credential stuffing.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory):
        self.uow_factory = uow_factory

    def hit(self, scope: str, subject: str, limit: Limit, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        window = math.floor(now.timestamp() / limit.seconds) * limit.seconds
        window_start = datetime.fromtimestamp(window, tz=now.tzinfo)
        key = f"{scope}:{subject}"
        with self.uow_factory() as uow:
            count = uow.rate_limits.hit(key, window_start)
            uow.commit()
        if count > limit.count:
            retry_after = int(window + limit.seconds - now.timestamp()) + 1
            log_event(log, "rate_limit.exceeded", logging.WARNING, scope=scope, limit=limit.count,
                      window_seconds=limit.seconds)
            raise RateLimited(
                "Too many attempts. Wait a moment and try again.",
                headers={"Retry-After": str(max(retry_after, 1))},
                details={"retry_after_seconds": max(retry_after, 1)},
            )


# ---- System email (verification, password reset, invitations) ----


def enqueue_system_email(uow: UnitOfWork, to: str, email: RenderedEmail, *, kind: str) -> None:
    """Queued in the outbox and sent by the worker through Postfix (templates in services/emails.py)."""
    uow.outbox.add(
        m.OutboxEvent(
            kind="system_email",
            payload={"to": to, "subject": email.subject, "text": email.text, "html": email.html, "template": kind},
            available_at=utcnow(),
        )
    )


def app_url(path: str) -> str:
    return get_settings().app_base_url.rstrip("/") + path


def hours(value: float) -> timedelta:
    return timedelta(hours=value)
