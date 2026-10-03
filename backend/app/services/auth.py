"""Signup, email verification, login sessions and password reset."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Forbidden, Unauthenticated, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import hash_password, hash_token, new_token, utcnow, verify_password
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
                "You already have a Mailvender account",
                "Someone (hopefully you) tried to sign up with this address, but it already has an "
                f"account.\n\nSign in: {app_url('/login')}\nForgot your password? {app_url('/forgot-password')}\n",
                kind="already_registered",
            )
        else:
            if user is None:
                user = self.uow.users.add(m.User(email=normalized, password_hash=hash_password(password)))
                log_event(log, "auth.signup", user_id=str(user.id))
            else:
                # Unverified: the latest password wins, the old link stops working.
                user.password_hash = hash_password(password)
            self._send_verification(user)
        self.uow.commit()

    def resend_verification(self, email: str, ip: str) -> None:
        self.limits.hit("resend:ip", ip, Limit.parse(self.settings.rate_limit_resend_ip))
        normalized = try_normalize_email(email)
        if normalized is None:
            return
        self.limits.hit("resend:email", normalized, Limit.parse(self.settings.rate_limit_resend_email))
        user = self.uow.users.get_by_email(normalized)
        if user and not user.email_verified_at:
            self._send_verification(user)
            self.uow.commit()

    def _send_verification(self, user: m.User) -> None:
        now = utcnow()
        self.uow.email_tokens.invalidate_for_user(user.id, VERIFY, now)
        token = new_token()
        self.uow.email_tokens.add(
            m.EmailToken(
                user_id=user.id,
                purpose=VERIFY,
                token_hash=hash_token(token),
                expires_at=now + timedelta(hours=self.settings.email_verification_ttl_hours),
            )
        )
        enqueue_system_email(
            self.uow,
            user.email,
            "Verify your email for Mailvender",
            "Welcome to Mailvender! Confirm your email address to finish creating your account:\n\n"
            f"{app_url('/verify-email?token=' + token)}\n\n"
            f"The link expires in {self.settings.email_verification_ttl_hours} hours and works once.\n",
            kind="verify_email",
        )

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
        self.uow.commit()
        log_event(log, "auth.email_verified", user_id=str(user.id))
        return user

    # ---- Sessions ----

    def login(self, email: str, password: str, ip: str, user_agent: str | None) -> NewSession:
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
            self.uow.commit()
        return user, session

    def logout(self, session: m.Session) -> None:
        session.revoked_at = utcnow()
        self.uow.commit()
        log_event(log, "auth.logout", session_id=str(session.id))

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
        now = utcnow()
        self.uow.email_tokens.invalidate_for_user(user.id, RESET, now)
        token = new_token()
        self.uow.email_tokens.add(
            m.EmailToken(
                user_id=user.id,
                purpose=RESET,
                token_hash=hash_token(token),
                expires_at=now + timedelta(minutes=self.settings.password_reset_ttl_minutes),
            )
        )
        enqueue_system_email(
            self.uow,
            user.email,
            "Reset your Mailvender password",
            "Use this link to choose a new password:\n\n"
            f"{app_url('/reset-password?token=' + token)}\n\n"
            f"It expires in {self.settings.password_reset_ttl_minutes} minutes and works once. "
            "If you didn't ask for this, ignore this email.\n",
            kind="password_reset",
        )
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
        self.uow.commit()
        log_event(log, "auth.password_reset", user_id=str(user.id))

    def get_user(self, user_id: uuid.UUID) -> m.User | None:
        return self.uow.users.get(user_id)
