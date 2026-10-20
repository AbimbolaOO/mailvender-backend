"""Password hashing, opaque tokens, encrypted secrets and signed recipient tokens."""

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings

# Argon2id (the argon2-cffi default type) with OWASP-recommended parameters.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def utcnow() -> datetime:
    return datetime.now(UTC)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        # Spend comparable time so timing doesn't reveal unknown accounts.
        _hasher.hash(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token(prefix: str = "") -> str:
    """A high-entropy URL-safe secret (256 bits)."""
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are random, so a keyed SHA-256 is enough (and is indexable)."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


# ---- API keys ----

API_KEY_PREFIX = "mv_"


@dataclass(frozen=True)
class NewApiKey:
    plaintext: str
    prefix: str
    key_hash: str


def generate_api_key() -> NewApiKey:
    lookup = secrets.token_hex(4)
    plaintext = f"{API_KEY_PREFIX}{lookup}_{secrets.token_urlsafe(32)}"
    return NewApiKey(plaintext=plaintext, prefix=f"{API_KEY_PREFIX}{lookup}", key_hash=hash_token(plaintext))


# ---- Secrets at rest ----


def _fernet() -> Fernet:
    return Fernet(get_settings().dkim_encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


# ---- DKIM ----


@dataclass(frozen=True)
class DkimKeyPair:
    private_pem: str
    public_dns_value: str  # base64 SubjectPublicKeyInfo, the `p=` value


def generate_dkim_key_pair(bits: int = 2048) -> DkimKeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return DkimKeyPair(private_pem=private_pem, public_dns_value=base64.b64encode(public_der).decode())


# ---- Signed, expiring, opaque recipient tokens (unsubscribe / view in browser) ----


def _token_fernet() -> Fernet:
    digest = hashlib.sha256(b"recipient-token:" + get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def sign_recipient_token(purpose: str, recipient_id: uuid.UUID, account_id: uuid.UUID) -> str:
    """Encrypted + authenticated (Fernet), so the token reveals nothing and can't be forged."""
    payload = json.dumps({"p": purpose, "r": str(recipient_id), "a": str(account_id)}).encode()
    return _token_fernet().encrypt(payload).decode()


@dataclass(frozen=True)
class RecipientToken:
    recipient_id: uuid.UUID
    account_id: uuid.UUID


def read_recipient_token(token: str, purpose: str, ttl: timedelta) -> RecipientToken | None:
    try:
        payload = json.loads(_token_fernet().decrypt(token.encode(), ttl=int(ttl.total_seconds())))
        if payload.get("p") != purpose:
            return None
        return RecipientToken(recipient_id=uuid.UUID(payload["r"]), account_id=uuid.UUID(payload["a"]))
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def expires_in(**delta: float) -> datetime:
    return utcnow() + timedelta(**delta)


# ---- TOTP two-factor codes (RFC 6238: 30-second steps, 6 digits, SHA-1 – what authenticator apps expect) ----

TOTP_STEP_SECONDS = 30


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def totp_step(at: datetime | None = None) -> int:
    return int((at or utcnow()).timestamp()) // TOTP_STEP_SECONDS


def match_totp(secret: str, code: str, *, after_step: int | None = None, at: datetime | None = None) -> int | None:
    """The time step `code` belongs to (±1 step for clock drift), or None. Steps ≤ `after_step` were used already."""
    code = code.strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return None
    now = totp_step(at)
    for step in (now - 1, now, now + 1):
        if after_step is not None and step <= after_step:
            continue
        if hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def totp_uri(secret: str, account: str, issuer: str = "Mailvender Admin") -> str:
    from urllib.parse import quote

    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits=6&period={TOTP_STEP_SECONDS}")


def new_recovery_codes(count: int = 10) -> list[str]:
    """Readable one-time codes like `k7mq-2xpa-9fne`."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(count)]


def new_admin_password() -> str:
    """A temporary password, printed once by `app.cli create-admin`."""
    return secrets.token_urlsafe(18)
