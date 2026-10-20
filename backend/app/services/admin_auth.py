"""Admin sign-in: Mailvender staff only, separate from customer users and sessions.

* An admin is created on the server (`python -m app.cli create-admin <email>`), which prints a
  temporary password once. Only ADMIN_MAX_ACCOUNTS admins (default 1) can be active at once; disable
  one to make room for another.
* First sign-in: the admin must change that password, then set up an authenticator app (TOTP).
  Until both are done, only those steps (and sign-out) work.
* Every later sign-in needs the password and a 6-digit code, or a one-time recovery code.
* Sessions end after ADMIN_SESSION_IDLE_MINUTES without use, and after ADMIN_SESSION_TTL_HOURS.
* Wrong passwords and codes count towards a lockout (ADMIN_MAX_FAILED_LOGINS → ADMIN_LOCKOUT_MINUTES).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Conflict, Forbidden, Unauthenticated, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    hash_token,
    match_totp,
    new_admin_password,
    new_recovery_codes,
    new_token,
    new_totp_secret,
    totp_uri,
    utcnow,
    verify_password,
)
from app.services.common import Actor, Limit, RateLimiter, audit, normalize_email

log = get_logger("admin_auth")

MIN_ADMIN_PASSWORD = 14
MAX_ADMIN_PASSWORD = 256
# Long but well-known passwords; anything on this list is refused.
COMMON_PASSWORDS = {
    "passwordpassword", "password123456", "12345678901234", "qwertyuiopasdf", "iloveyou123456",
    "administrator1", "administrator123", "letmein1234567", "welcome1234567", "mailvender1234",
    "mailvenderadmin", "changeme123456", "correcthorsebatterystaple", "123456789012345",
}

INVALID_CREDENTIALS = "Email, password or code is incorrect."


def invalid_credentials() -> ApiError:
    return ApiError(INVALID_CREDENTIALS, code="invalid_credentials", status_code=401)


@dataclass
class AdminAuth:
    """The signed-in admin of a request."""

    admin: m.AdminUser
    session: m.AdminSession
    ip: str

    @property
    def actor(self) -> Actor:
        return Actor("operator", self.admin.id)

    @property
    def next_step(self) -> str | None:
        if self.admin.must_change_password:
            return "change_password"
        if self.admin.totp_enabled_at is None:
            return "setup_two_factor"
        return None


@dataclass
class NewAdminSession:
    session: m.AdminSession
    token: str
    admin: m.AdminUser


def admin_audit(uow: UnitOfWork, auth: AdminAuth, action: str, *, target_type: str | None = None,
                target_id: uuid.UUID | None = None, reason: str | None = None, **data: Any) -> m.AuditEvent:
    """Every admin action and sensitive view is recorded (immutable), with who, from where and why.

    These rows have no account_id, so customers never see them in their account's audit log
    (the admin's email and IP stay private); the admin's Audit log page lists them.
    """
    return audit(uow, None, auth.actor, action, target_type=target_type, target_id=target_id, reason=reason,
                 admin_email=auth.admin.email, ip=auth.ip, **data)


def validate_admin_password(password: str, *, email: str, current: str | None = None) -> None:
    field = "new_password"
    if not MIN_ADMIN_PASSWORD <= len(password) <= MAX_ADMIN_PASSWORD:
        raise ValidationFailed([field_error(field, f"Use at least {MIN_ADMIN_PASSWORD} characters.",
                                            "password_length")])
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(password)) < 5:
        raise ValidationFailed([field_error(field, "That password is too easy to guess. Try a few unrelated "
                                                   "words.", "password_common")])
    if email.split("@", 1)[0].lower() in lowered:
        raise ValidationFailed([field_error(field, "Don't use your email address in the password.",
                                            "password_contains_email")])
    if current is not None and password == current:
        raise ValidationFailed([field_error(field, "Choose a new password, not the one you have now.",
                                            "password_reused")])


class AdminAuthService:
    def __init__(self, uow: UnitOfWork, rate_limiter: RateLimiter | None = None):
        self.uow = uow
        self.limits = rate_limiter
        self.settings = get_settings()

    # ---- Managing admins (server CLI only) ----

    def create(self, email: str) -> tuple[m.AdminUser, str]:
        normalized = normalize_email(email)
        if self.uow.admins.get_by_email(normalized):
            raise Conflict("An admin with this email already exists.", code="admin_exists")
        self._check_room()
        password = new_admin_password()
        admin = self.uow.admins.add(m.AdminUser(email=normalized, password_hash=hash_password(password),
                                                must_change_password=True, recovery_code_hashes=[]))
        audit(self.uow, None, Actor.system(), "admin.created", target_type="admin", target_id=admin.id,
              admin_email=normalized)
        self.uow.commit()
        return admin, password

    def reset(self, email: str) -> str:
        """New temporary password, two-factor cleared, every session ended (lost phone, forgotten password)."""
        admin = self._by_email(email)
        if admin.disabled_at:
            self._check_room()
        password = new_admin_password()
        admin.password_hash = hash_password(password)
        admin.must_change_password = True
        admin.totp_secret_encrypted = None
        admin.totp_enabled_at = None
        admin.totp_last_step = None
        admin.recovery_code_hashes = []
        admin.failed_logins, admin.locked_until, admin.disabled_at = 0, None, None
        self.uow.admins.revoke_sessions(admin.id, utcnow())
        audit(self.uow, None, Actor.system(), "admin.reset", target_type="admin", target_id=admin.id,
              admin_email=admin.email)
        self.uow.commit()
        return password

    def disable(self, email: str) -> None:
        admin = self._by_email(email)
        admin.disabled_at = utcnow()
        self.uow.admins.revoke_sessions(admin.id, admin.disabled_at)
        audit(self.uow, None, Actor.system(), "admin.disabled", target_type="admin", target_id=admin.id,
              admin_email=admin.email)
        self.uow.commit()

    def _check_room(self) -> None:
        """Refuses another active admin once ADMIN_MAX_ACCOUNTS are active."""
        self.uow.admins.lock()
        limit = self.settings.admin_max_accounts
        if self.uow.admins.count_active() >= limit:
            raise Conflict(f"Only {limit} active admin{'s' if limit != 1 else ''} allowed. Disable one first "
                           "(`python -m app.cli disable-admin <email>`).", code="admin_limit")

    def _by_email(self, email: str) -> m.AdminUser:
        admin = self.uow.admins.get_by_email(normalize_email(email), for_update=True)
        if admin is None:
            raise ApiError("No admin with this email.", code="admin_not_found", status_code=404)
        return admin

    # ---- Sign-in ----

    def login(self, email: str, password: str, code: str | None, ip: str, user_agent: str | None) -> NewAdminSession:
        if self.limits:
            self.limits.hit("admin_login:ip", ip, Limit.parse(self.settings.rate_limit_admin_login_ip))
        normalized = email.strip().lower()
        if self.limits:
            self.limits.hit("admin_login:email", normalized, Limit.parse(self.settings.rate_limit_admin_login_email))
        now = utcnow()
        admin = self.uow.admins.get_by_email(normalized, for_update=True)
        if admin is None or admin.disabled_at or (admin.locked_until and admin.locked_until > now):
            verify_password(None, password)  # same time as a real check
            log_event(log, "admin.login_failed", logging.WARNING, reason="unknown_disabled_or_locked")
            raise invalid_credentials()
        if not verify_password(admin.password_hash, password):
            self._failed(admin, ip)
            raise invalid_credentials()
        if admin.totp_enabled_at is not None:
            if not code or not code.strip():
                # The password was right; ask for the second factor without counting a failure.
                raise ApiError("Enter the 6-digit code from your authenticator app.", code="totp_required",
                               status_code=401)
            if not self._use_code(admin, code):
                self._failed(admin, ip)
                raise invalid_credentials()
        admin.failed_logins, admin.locked_until, admin.last_login_at = 0, None, now
        token = new_token()
        session = self.uow.admins.add_session(m.AdminSession(
            admin_id=admin.id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(hours=self.settings.admin_session_ttl_hours),
            last_seen_at=now,
            ip_address=ip[:64],
            user_agent=(user_agent or "")[:512] or None,
        ))
        audit(self.uow, None, Actor("operator", admin.id), "admin.signed_in", target_type="admin",
              target_id=admin.id, admin_email=admin.email, ip=ip)
        self.uow.commit()
        log_event(log, "admin.login", admin_id=str(admin.id))
        return NewAdminSession(session=session, token=token, admin=admin)

    def _failed(self, admin: m.AdminUser, ip: str) -> None:
        admin.failed_logins += 1
        if admin.failed_logins >= self.settings.admin_max_failed_logins:
            admin.failed_logins = 0
            admin.locked_until = utcnow() + timedelta(minutes=self.settings.admin_lockout_minutes)
            audit(self.uow, None, Actor.system(), "admin.locked", target_type="admin", target_id=admin.id,
                  admin_email=admin.email, ip=ip)
            log_event(log, "admin.locked", logging.WARNING, admin_id=str(admin.id))
        log_event(log, "admin.login_failed", logging.WARNING, admin_id=str(admin.id))
        self.uow.commit()

    def _use_code(self, admin: m.AdminUser, code: str) -> bool:
        """A TOTP code (each accepted once) or a recovery code (removed once used)."""
        if admin.totp_secret_encrypted:
            step = match_totp(decrypt_secret(admin.totp_secret_encrypted), code, after_step=admin.totp_last_step)
            if step is not None:
                admin.totp_last_step = step
                return True
        hashed = hash_token(code.strip().lower())
        if hashed in (admin.recovery_code_hashes or []):
            admin.recovery_code_hashes = [h for h in admin.recovery_code_hashes if h != hashed]
            audit(self.uow, None, Actor("operator", admin.id), "admin.recovery_code_used", target_type="admin",
                  target_id=admin.id, remaining=len(admin.recovery_code_hashes))
            return True
        return False

    def authenticate(self, token: str, ip: str) -> AdminAuth:
        now = utcnow()
        session = self.uow.admins.get_session_by_token_hash(hash_token(token))
        ended = Unauthenticated("Your admin session has ended. Sign in again.", code="admin_session_invalid")
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            raise ended
        if now - session.last_seen_at > timedelta(minutes=self.settings.admin_session_idle_minutes):
            session.revoked_at = now
            self.uow.commit()
            raise ended
        admin = self.uow.admins.get(session.admin_id)
        if admin is None or admin.disabled_at:
            raise ended
        if now - session.last_seen_at > timedelta(seconds=30):
            session.last_seen_at = now
            self.uow.commit()
        return AdminAuth(admin=admin, session=session, ip=ip)

    @staticmethod
    def require_ready(auth: AdminAuth) -> AdminAuth:
        if auth.next_step == "change_password":
            raise Forbidden("Change your temporary password first.", code="password_change_required")
        if auth.next_step == "setup_two_factor":
            raise Forbidden("Set up two-factor sign-in first.", code="two_factor_setup_required")
        return auth

    def logout(self, auth: AdminAuth) -> None:
        auth.session.revoked_at = utcnow()
        self.uow.commit()

    # ---- First sign-in: password, then two-factor ----

    def change_password(self, auth: AdminAuth, current: str, new: str) -> None:
        admin = auth.admin
        if not verify_password(admin.password_hash, current):
            raise ValidationFailed([field_error("current_password", "That isn't your current password.",
                                                "invalid_password")])
        validate_admin_password(new, email=admin.email, current=current)
        admin.password_hash = hash_password(new)
        admin.must_change_password = False
        # Whoever knew the old password is signed out everywhere else.
        self.uow.admins.revoke_sessions(admin.id, utcnow(), keep=auth.session.id)
        admin_audit(self.uow, auth, "admin.password_changed", target_type="admin", target_id=admin.id)
        self.uow.commit()

    def start_two_factor(self, auth: AdminAuth) -> tuple[str, str]:
        if auth.admin.must_change_password:
            raise Forbidden("Change your temporary password first.", code="password_change_required")
        if auth.admin.totp_enabled_at is not None:
            raise Conflict("Two-factor sign-in is already set up. To move it to a new phone, ask for a reset "
                           "(app.cli reset-admin).", code="two_factor_enabled")
        secret = new_totp_secret()
        auth.admin.totp_secret_encrypted = encrypt_secret(secret)
        self.uow.commit()
        return secret, totp_uri(secret, auth.admin.email)

    def confirm_two_factor(self, auth: AdminAuth, code: str) -> list[str]:
        admin = auth.admin
        if admin.totp_enabled_at is not None:
            raise Conflict("Two-factor sign-in is already set up.", code="two_factor_enabled")
        if not admin.totp_secret_encrypted:
            raise Conflict("Start the two-factor setup first.", code="two_factor_not_started")
        step = match_totp(decrypt_secret(admin.totp_secret_encrypted), code)
        if step is None:
            raise ValidationFailed([field_error("code", "That code didn't work. Codes change every 30 seconds – "
                                                        "try the one showing now.", "invalid_code")])
        admin.totp_enabled_at, admin.totp_last_step = utcnow(), step
        codes = new_recovery_codes()
        admin.recovery_code_hashes = [hash_token(c) for c in codes]
        admin_audit(self.uow, auth, "admin.two_factor_enabled", target_type="admin", target_id=admin.id)
        self.uow.commit()
        return codes
