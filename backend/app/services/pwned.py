"""Breached passwords: refuse passwords that appear in known data breaches.

Uses Have I Been Pwned's k-anonymity "range" API: only the first five hex characters of the
password's SHA-1 are sent; the full hash and the password never leave the server. Responses are
padded so their size doesn't hint at the result. If the service is slow or down, the password is
allowed (PWNED_PASSWORDS_CHECK=false turns the check off).
"""

import hashlib
import logging

import httpx

from app.config import get_settings
from app.logging import get_logger, log_event

log = get_logger("pwned")

RANGE_URL = "https://api.pwnedpasswords.com/range/"
BREACHED = ("This password has appeared in a data breach, so attackers try it first. Choose a different one "
            "(a few unrelated words work well).")


def times_breached(password: str) -> int:
    """How often the password appears in breaches (0 = never seen, or the check is off/unavailable)."""
    if not get_settings().pwned_passwords_check:
        return 0
    digest = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 - the API's format, not for storage
    prefix, suffix = digest[:5], digest[5:]
    try:
        response = httpx.get(RANGE_URL + prefix, timeout=3,
                             headers={"Add-Padding": "true", "User-Agent": "Mailvender password check"})
        response.raise_for_status()
    except httpx.HTTPError as error:
        log_event(log, "pwned.unavailable", logging.WARNING, error=str(error)[:200])
        return 0
    for line in response.text.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip() == suffix:
            return int(count.strip() or 0)  # padding lines have a count of 0
    return 0
