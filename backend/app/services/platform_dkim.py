"""DKIM for Mailvender's own domain (system emails: verification, password reset, …).

The platform domain (``Settings.platform_domain``, from SYSTEM_FROM_EMAIL) isn't
a customer domain: no account owns it, and customers can't add it. Its keys live
in ``platform_dkim_keys``:

* A new key starts ``pending``. The ``platform_dkim`` job (and
  ``python -m app.cli platform-dkim``) looks up its TXT record and activates it
  once it's published, retiring the previous key.
* The worker's DKIM sync signs ``*@<platform domain>`` with the active key.
"""

import logging
from dataclasses import dataclass

from app import models as m
from app.config import get_settings
from app.errors import Conflict
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import encrypt_secret, generate_dkim_key_pair, utcnow
from app.services.dns import DnsLookupError, DnsResolver
from app.services.domains import dkim_record_status, new_dkim_selector

log = get_logger("platform_dkim")


@dataclass
class PlatformKeyView:
    key: m.PlatformDkimKey
    record_name: str
    record_value: str
    dns_status: str  # verified | missing | mismatched | unchecked


class PlatformDkimService:
    def __init__(self, uow: UnitOfWork, resolver: DnsResolver):
        self.uow = uow
        self.resolver = resolver
        self.domain = get_settings().platform_domain

    def _keys(self) -> tuple[m.PlatformDkimKey | None, m.PlatformDkimKey | None]:
        keys = self.uow.platform_dkim_keys.list_for_domain(self.domain)
        active = next((k for k in keys if k.status == "active"), None)
        pending = next((k for k in keys if k.status == "pending"), None)
        return active, pending

    def _new_key(self) -> m.PlatformDkimKey:
        pair = generate_dkim_key_pair(2048)
        key = self.uow.platform_dkim_keys.add(
            m.PlatformDkimKey(
                domain=self.domain,
                selector=new_dkim_selector("mvsys"),
                private_key_encrypted=encrypt_secret(pair.private_pem),
                public_key=pair.public_dns_value,
                status="pending",
            )
        )
        log_event(log, "platform_dkim.key_created", domain=self.domain, selector=key.selector)
        return key

    def _lookup(self, key: m.PlatformDkimKey) -> str:
        try:
            found = self.resolver.txt(f"{key.selector}._domainkey.{self.domain}")
        except DnsLookupError:
            return "unchecked"
        return dkim_record_status(found, key.public_key)[0]

    def _view(self, key: m.PlatformDkimKey, dns_status: str) -> PlatformKeyView:
        return PlatformKeyView(key=key, record_name=f"{key.selector}._domainkey.{self.domain}",
                               record_value=f"v=DKIM1; k=rsa; p={key.public_key}", dns_status=dns_status)

    def check(self) -> list[PlatformKeyView]:
        """Creates the first key if there's none, activates a pending key once its record is published."""
        active, pending = self._keys()
        if active is None and pending is None:
            pending = self._new_key()
        views = []
        if pending is not None:
            status = self._lookup(pending)
            if status == "verified":
                now = utcnow()
                if active is not None:
                    active.status, active.retired_at = "retired", now
                pending.status, pending.activated_at = "active", now
                log_event(log, "platform_dkim.key_activated", domain=self.domain, selector=pending.selector)
                active = None
            views.append(self._view(pending, status))
        if active is not None:
            status = self._lookup(active)
            if status in ("missing", "mismatched"):
                log_event(log, "platform_dkim.record_missing", logging.WARNING, domain=self.domain,
                          selector=active.selector, dns_status=status)
            views.insert(0, self._view(active, status))
        self.uow.commit()
        return views

    def rotate(self) -> PlatformKeyView:
        """Starts a rotation: a new pending key that takes over once its record is published."""
        active, pending = self._keys()
        if pending is not None:
            raise Conflict("A new platform DKIM key is already waiting for its DNS record.", code="rotation_pending")
        if active is None:
            raise Conflict("There's no active platform DKIM key to rotate yet.", code="no_active_key")
        key = self._new_key()
        self.uow.commit()
        return self._view(key, "unchecked")
