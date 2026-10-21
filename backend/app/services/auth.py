"""Signup, email verification, login sessions and password reset."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound, Unauthenticated, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import hash_password, hash_token, new_token, utcnow, verify_password
from app.services import emails
from app.services.common import (
    Actor,
    Limit,
    RateLimiter,
    app_url,
    audit,
    enqueue_system_email,
    normalize_email,
    try_normalize_email,
)
from app.services.pwned import BREACHED, times_breached
from app.services.two_factor import use_code

log = get_logger("auth")

VERIFY = "verify_email"
RESET = "password_reset"
MIN_PASSWORD = 10
MAX_PASSWORD = 256

INVALID_TOKEN = ApiError(
    "This link is invalid or has expired. Request a new one.", code="invalid_token", status_code=400
)


def validate_password(password: str, field: str = "password") -> None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValidationFailed(
            [field_error(field, f"Use between {MIN_PASSWORD} and {MAX_PASSWORD} characters.", "password_length")]
        )
    if times_breached(password):
        raise ValidationFailed([field_error(field, BREACHED, "password_breached")])


def send_password_reset(uow: UnitOfWork, user: m.User) -> None:
    """Queues a password reset link (any earlier link stops working). The caller commits."""
    settings = get_settings()
    now = utcnow()
    uow.email_tokens.invalidate_for_user(user.id, RESET, now)
    token = new_token()
    uow.email_tokens.add(m.EmailToken(user_id=user.id, purpose=RESET, token_hash=hash_token(token),
                                      expires_at=now + timedelta(minutes=settings.password_reset_ttl_minutes)))
    enqueue_system_email(uow, user.email, emails.password_reset(app_url("/reset-password?token=" + token),
                                                                settings.password_reset_ttl_minutes),
                         kind="password_reset")


def send_verification(uow: UnitOfWork, user: m.User) -> None:
    """Queues a fresh verification link (older links stop working). Also used by the admin's "Resend"."""
    settings = get_settings()
    now = utcnow()
    uow.email_tokens.invalidate_for_user(user.id, VERIFY, now)
    token = new_token()
    uow.email_tokens.add(
        m.EmailToken(
            user_id=user.id,
            purpose=VERIFY,
            token_hash=hash_token(token),
            expires_at=now + timedelta(hours=settings.email_verification_ttl_hours),
        )
    )
    enqueue_system_email(
        uow,
        user.email,
        emails.verify_email(app_url("/verify-email?token=" + token), settings.email_verification_ttl_hours),
        kind="verify_email",
    )


@dataclass
class NewSession:
    session: m.Session
    token: str
    user: m.User


class AuthService:
    def __init__(self, uow: UnitOfWork, rate_limiter: RateLimiter):
        self.uow = uow
        self.limits = rate_limiter
        self.settings = get_settings()

    # ---- Signup and verification ----

    def signup(self, email: str, password: str, ip: str) -> None:
        """Always answers the same way, so it doesn't reveal registered addresses."""
        self.limits.hit("signup:ip", ip, Limit.parse(self.settings.rate_limit_signup_ip))
        normalized = normalize_email(email)
        self.limits.hit("signup:email", normalized, Limit.parse(self.settings.rate_limit_signup_email))
        validate_password(password)

        user = self.uow.users.get_by_email(normalized)
        if user and user.email_verified_at:
            enqueue_system_email(
                self.uow,
                normalized,
                emails.already_registered(app_url("/login"), app_url("/forgot-password")),
                kind="already_registered",
            )
        else:
            if user is None:
                user = self.uow.users.add(m.User(email=normalized, password_hash=hash_password(password)))
                log_event(log, "auth.signup", user_id=str(user.id))
            else:
                # Unverified: the latest password wins, the old link stops working.
                user.password_hash = hash_password(password)
            send_verification(self.uow, user)
        self.uow.commit()

    def resend_verification(self, email: str, ip: str) -> None:
        self.limits.hit("resend:ip", ip, Limit.parse(self.settings.rate_limit_resend_ip))
        normalized = try_normalize_email(email)
        if normalized is None:
            return
        self.limits.hit("resend:email", normalized, Limit.parse(self.settings.rate_limit_resend_email))
        user = self.uow.users.get_by_email(normalized)
        if user and not user.email_verified_at:
            send_verification(self.uow, user)
            self.uow.commit()

    def _use_token(self, token: str, purpose: str) -> m.EmailToken:
        record = self.uow.email_tokens.get_by_hash(hash_token(token), purpose, for_update=True)
        if record is None or record.used_at is not None or record.expires_at <= utcnow():
            raise INVALID_TOKEN
        record.used_at = utcnow()
        return record

    def verify_email(self, token: str) -> m.User:
        """Marks the email verified and creates the personal account + owner membership (one transaction)."""
        record = self._use_token(token, VERIFY)
        user = self.uow.users.get(record.user_id)
        assert user is not None
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
            account = self.uow.accounts.add(
                m.Account(
                    name="Personal",
                    kind="personal",
                    hourly_recipient_limit=self.settings.default_hourly_recipient_limit,
                    daily_recipient_limit=self.settings.default_daily_recipient_limit,
                )
            )
            self.uow.memberships.add(m.Membership(account_id=account.id, user_id=user.id, role="owner"))
            user.default_account_id = account.id
            audit(self.uow, account.id, Actor("user", user.id), "account.created", target_type="account",
                  target_id=account.id, kind="personal")
            audit(self.uow, account.id, Actor.system(), "account.limits_set", target_type="account",
                  target_id=account.id, reason="Default limits for new accounts",
                  hourly=account.hourly_recipient_limit, daily=account.daily_recipient_limit)
            enqueue_system_email(self.uow, user.email, emails.welcome(app_url("/")), kind="welcome")
        self.uow.commit()
        log_event(log, "auth.email_verified", user_id=str(user.id))
        return user

    # ---- Sessions ----

    def login(self, email: str, password: str, ip: str, user_agent: str | None, code: str | None = None) -> NewSession:
        self.limits.hit("login:ip", ip, Limit.parse(self.settings.rate_limit_login_ip))
        normalized = try_normalize_email(email) or email.strip().lower()
        self.limits.hit("login:email", normalized, Limit.parse(self.settings.rate_limit_login_email))
        user = self.uow.users.get_by_email(normalized)
        if not verify_password(user.password_hash if user else None, password) or user is None:
            log_event(log, "auth.login_failed")
            raise ApiError("Email or password is incorrect.", code="invalid_credentials", status_code=401)
        if user.disabled_at:
            raise Forbidden("This user is disabled.", code="user_disabled")
        if user.email_verified_at is None:
            raise Forbidden("Verify your email address first – check your inbox for the link.",
                            code="email_not_verified")
        if user.totp_enabled_at:
            if not code:
                raise ApiError("Enter the 6-digit code from your authenticator app.", code="totp_required",
                               status_code=401)
            if not use_code(self.uow, user, code):
                self.uow.commit()
                log_event(log, "auth.login_failed", reason="bad_code")
                raise ApiError("That code didn't work. Use the one showing in your app now, or a recovery code.",
                               code="invalid_code", status_code=401)
        token = new_token()
        session = self.uow.sessions.add(
            m.Session(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=utcnow() + timedelta(hours=self.settings.session_ttl_hours),
                user_agent=(user_agent or "")[:512] or None,
                ip_address=ip[:64],
            )
        )
        self.uow.commit()
        log_event(log, "auth.login", user_id=str(user.id), session_id=str(session.id))
        return NewSession(session=session, token=token, user=user)

    def authenticate(self, token: str) -> tuple[m.User, m.Session]:
        session = self.uow.sessions.get_by_token_hash(hash_token(token))
        now = utcnow()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            raise Unauthenticated("Your session has ended. Sign in again.", code="session_invalid")
        user = self.uow.users.get(session.user_id)
        if user is None or user.disabled_at or user.email_verified_at is None:
            raise Unauthenticated("Your session has ended. Sign in again.", code="session_invalid")
        if session.last_seen_at is None or now - session.last_seen_at > timedelta(minutes=5):
            session.last_seen_at = now
            self.uow.users.mark_active(user.id, now.date())  # the admin's "active users" chart
            self.uow.commit()
        return user, session

    def logout(self, session: m.Session) -> None:
        session.revoked_at = utcnow()
        self.uow.commit()
        log_event(log, "auth.logout", session_id=str(session.id))

    def sessions(self, user: m.User) -> list[m.Session]:
        """The user's signed-in devices, newest first."""
        found = self.uow.sessions.list_active_for_user(user.id, utcnow())
        return sorted(found, key=lambda s: s.last_seen_at or s.created_at, reverse=True)

    def revoke_session(self, user: m.User, current: m.Session, session_id: uuid.UUID) -> None:
        if session_id == current.id:
            raise ApiError("That's this device – use Sign out instead.", code="current_session", status_code=409)
        session = self.uow.sessions.get_for_user(session_id, user.id)
        if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
            raise NotFound("Session not found.")
        session.revoked_at = utcnow()
        audit(self.uow, user.default_account_id, Actor("user", user.id), "user.session_revoked",
              target_type="user", target_id=user.id, session_id=str(session.id))
        self.uow.commit()
        log_event(log, "auth.session_revoked", user_id=str(user.id), session_id=str(session.id))

    def revoke_other_sessions(self, user: m.User, current: m.Session) -> int:
        revoked = self.uow.sessions.revoke_others(user.id, current.id, utcnow())
        audit(self.uow, user.default_account_id, Actor("user", user.id), "user.sessions_revoked",
              target_type="user", target_id=user.id, sessions=revoked)
        self.uow.commit()
        log_event(log, "auth.sessions_revoked", user_id=str(user.id), sessions=revoked)
        return revoked

    def change_password(self, user: m.User, current: m.Session, current_password: str, new_password: str) -> None:
        """Changes the password while signed in: this device stays signed in, every other one is signed out."""
        self.limits.hit("password_change:user", str(user.id), Limit.parse(self.settings.rate_limit_login_email))
        if not verify_password(user.password_hash, current_password):
            raise ValidationFailed([field_error("current_password", "That isn't your current password.",
                                                "invalid_password")])
        if current_password == new_password:
            raise ValidationFailed([field_error("new_password", "Choose a password you haven't used here.",
                                                "password_unchanged")])
        validate_password(new_password, "new_password")
        now = utcnow()
        user.password_hash = hash_password(new_password)
        self.uow.email_tokens.invalidate_for_user(user.id, RESET, now)
        self.uow.sessions.revoke_others(user.id, current.id, now)
        audit(self.uow, user.default_account_id, Actor("user", user.id), "user.password_changed",
              target_type="user", target_id=user.id)
        enqueue_system_email(self.uow, user.email, emails.password_changed(user.email, now,
                                                                           app_url("/forgot-password"),
                                                                           kept_device=True),
                             kind="password_changed")
        self.uow.commit()
        log_event(log, "auth.password_changed", user_id=str(user.id))

    # ---- Password reset ----

    def request_password_reset(self, email: str, ip: str) -> None:
        """Same response whether or not the address is registered."""
        self.limits.hit("reset:ip", ip, Limit.parse(self.settings.rate_limit_reset_ip))
        normalized = try_normalize_email(email)
        if normalized is None:
            return
        self.limits.hit("reset:email", normalized, Limit.parse(self.settings.rate_limit_reset_email))
        user = self.uow.users.get_by_email(normalized)
        if user is None or user.email_verified_at is None or user.disabled_at:
            return
        send_password_reset(self.uow, user)
        self.uow.commit()

    def reset_password(self, token: str, new_password: str) -> None:
        validate_password(new_password)
        record = self._use_token(token, RESET)
        user = self.uow.users.get(record.user_id)
        assert user is not None
        user.password_hash = hash_password(new_password)
        now = utcnow()
        self.uow.email_tokens.invalidate_for_user(user.id, RESET, now)
        # Every existing session ends – whoever knew the old password is signed out.
        self.uow.sessions.revoke_for_users([user.id], now)
        audit(self.uow, user.default_account_id, Actor("user", user.id), "user.password_reset",
              target_type="user", target_id=user.id)
        enqueue_system_email(self.uow, user.email, emails.password_changed(user.email, now, app_url("/forgot-password")),
                             kind="password_changed")
        self.uow.commit()
        log_event(log, "auth.password_reset", user_id=str(user.id))

    def get_user(self, user_id: uuid.UUID) -> m.User | None:
        return self.uow.users.get(user_id)
