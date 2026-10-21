"""Two-factor sign-in for customers (optional): an authenticator app code after the password.

Accounts send email in our name, so a stolen password is worth a lot to a spammer. Turning this on
(Settings → You) means a password alone no longer signs in:

* setup → a new secret (QR code); confirm with a code from the app → 10 one-time recovery codes;
* sign-in then needs the password and a code (`totp_required` until it's given), or a recovery code;
* turning it off needs the password and a current code; so does making new recovery codes (password);
* a password reset doesn't turn it off – someone with only the person's inbox still can't sign in;
* a person who lost their phone and their codes asks support: an admin resets it (audited).

Each change is emailed to the person, so they notice one they didn't make.
"""

from app import models as m
from app.errors import Conflict, ValidationFailed, field_error
from app.repositories.interfaces import UnitOfWork
from app.security import (
    decrypt_secret,
    encrypt_secret,
    hash_token,
    match_totp,
    new_recovery_codes,
    new_totp_secret,
    totp_uri,
    utcnow,
    verify_password,
)
from app.services import emails
from app.services.common import Actor, app_url, audit, enqueue_system_email

ISSUER = "Mailvender"


def use_code(uow: UnitOfWork, user: m.User, code: str) -> bool:
    """A current authenticator code (each accepted once) or a recovery code (removed when used)."""
    if user.totp_secret_encrypted:
        step = match_totp(decrypt_secret(user.totp_secret_encrypted), code, after_step=user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            return True
    hashed = hash_token(code.strip().lower())
    if hashed in (user.recovery_code_hashes or []):
        user.recovery_code_hashes = [h for h in user.recovery_code_hashes if h != hashed]
        audit(uow, None, Actor("user", user.id), "user.recovery_code_used", target_type="user", target_id=user.id,
              remaining=len(user.recovery_code_hashes))
        return True
    return False


def _notify(uow: UnitOfWork, user: m.User, enabled: bool) -> None:
    enqueue_system_email(uow, user.email, emails.two_factor_changed(user.email, enabled, utcnow(),
                                                                    app_url("/forgot-password")),
                         kind="two_factor_changed")


def _check_password(user: m.User, password: str) -> None:
    if not verify_password(user.password_hash, password):
        raise ValidationFailed([field_error("password", "That isn't your password.", "invalid_password")])


class TwoFactorService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow

    def start(self, user: m.User) -> tuple[str, str]:
        if user.totp_enabled_at:
            raise Conflict("Two-factor sign-in is already on.", code="two_factor_enabled")
        secret = new_totp_secret()
        user.totp_secret_encrypted = encrypt_secret(secret)
        self.uow.commit()
        return secret, totp_uri(secret, user.email, issuer=ISSUER)

    def confirm(self, user: m.User, code: str) -> list[str]:
        if user.totp_enabled_at:
            raise Conflict("Two-factor sign-in is already on.", code="two_factor_enabled")
        if not user.totp_secret_encrypted:
            raise Conflict("Start the setup first.", code="two_factor_not_started")
        step = match_totp(decrypt_secret(user.totp_secret_encrypted), code)
        if step is None:
            raise ValidationFailed([field_error("code", "That code didn't work. Codes change every 30 seconds – try "
                                                        "the one showing now.", "invalid_code")])
        user.totp_enabled_at, user.totp_last_step = utcnow(), step
        codes = new_recovery_codes()
        user.recovery_code_hashes = [hash_token(c) for c in codes]
        audit(self.uow, None, Actor("user", user.id), "user.two_factor_enabled", target_type="user", target_id=user.id)
        _notify(self.uow, user, True)
        self.uow.commit()
        return codes

    def disable(self, user: m.User, password: str, code: str) -> None:
        if not user.totp_enabled_at:
            raise Conflict("Two-factor sign-in is already off.", code="two_factor_disabled")
        _check_password(user, password)
        if not use_code(self.uow, user, code):
            raise ValidationFailed([field_error("code", "That code didn't work.", "invalid_code")])
        self._clear(user)
        audit(self.uow, None, Actor("user", user.id), "user.two_factor_disabled", target_type="user",
              target_id=user.id)
        _notify(self.uow, user, False)
        self.uow.commit()

    def replace_recovery_codes(self, user: m.User, password: str) -> list[str]:
        if not user.totp_enabled_at:
            raise Conflict("Turn on two-factor sign-in first.", code="two_factor_disabled")
        _check_password(user, password)
        codes = new_recovery_codes()
        user.recovery_code_hashes = [hash_token(c) for c in codes]
        audit(self.uow, None, Actor("user", user.id), "user.recovery_codes_replaced", target_type="user",
              target_id=user.id)
        self.uow.commit()
        return codes

    def reset_by_admin(self, user: m.User) -> None:
        """Support: lost phone and recovery codes. The caller audits (with the admin and reason) and commits."""
        self._clear(user)
        _notify(self.uow, user, False)

    @staticmethod
    def _clear(user: m.User) -> None:
        user.totp_secret_encrypted = None
        user.totp_enabled_at = None
        user.totp_last_step = None
        user.recovery_code_hashes = []
