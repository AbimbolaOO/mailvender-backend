"""Sending domains: DKIM keys, required DNS records, verification and sender identities.

Records required by the Postfix deployment (see docs/RUNBOOK.md#dns):

* ``<selector>._domainkey.<domain>`` TXT – the DKIM public key OpenDKIM signs with
  (``d=<domain>``, so DKIM always aligns with the visible From domain).
* ``<return_path_subdomain>.<domain>`` MX – the envelope sender (MAIL FROM /
  Return-Path) host, pointing at Mailvender's bounce-processing MX.
* ``<return_path_subdomain>.<domain>`` TXT – SPF for the envelope sender,
  including Mailvender's outbound IPs. It aligns (relaxed) with the From domain.
* ``_dmarc.<domain>`` TXT – recommended; required for bulk sending.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Conflict, Forbidden, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import encrypt_secret, generate_dkim_key_pair, new_token, utcnow
from app.services.common import AccountContext, Actor, audit, normalize_email
from app.services.dns import DnsLookupError, DnsResolver

log = get_logger("domains")

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_domain(value: str) -> str:
    name = value.strip().lower().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        name = ""
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels) or labels[-1].isdigit():
        raise ValidationFailed([field_error("name", "Enter a domain name like example.com.", "invalid_domain")])
    return name


def _squash(value: str) -> str:
    return re.sub(r'[\s"]', "", value).lower()


def _tags(record: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in record.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep:
            tags[key.strip().lower()] = value.strip()
    return tags


@dataclass
class DnsRecord:
    key: str
    purpose: str
    type: str
    name: str
    value: str
    required: bool
    status: str = "unchecked"  # verified | missing | mismatched | unchecked
    found: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class DomainView:
    domain: m.Domain
    records: list[DnsRecord]
    active_selector: str | None


class DomainService:
    def __init__(self, uow: UnitOfWork, resolver: DnsResolver):
        self.uow = uow
        self.resolver = resolver
        self.settings = get_settings()

    # ---- Registration ----

    def register(self, ctx: AccountContext, name: str) -> DomainView:
        normalized = normalize_domain(name)
        existing = self.uow.domains.get_live_by_name(normalized)
        if existing:
            if existing.account_id == ctx.account_id:
                raise Conflict("You've already added this domain.", code="domain_exists")
            # Never reveal which account holds it.
            raise Conflict(
                "This domain is already registered with Mailvender. Contact support if you control it.",
                code="domain_unavailable",
            )
        domain = self.uow.domains.add(
            m.Domain(
                account_id=ctx.account_id,
                name=normalized,
                status="pending",
                return_path_host=f"{self.settings.return_path_subdomain}.{normalized}",
                record_status={},
            )
        )
        self._new_key(domain, status="active")
        audit(self.uow, ctx.account_id, ctx.actor, "domain.registered", target_type="domain",
              target_id=domain.id, name=normalized)
        self.uow.commit()
        return self.view(domain)

    def _new_key(self, domain: m.Domain, *, status: str) -> m.DkimKey:
        pair = generate_dkim_key_pair(2048)
        now = utcnow()
        return self.uow.dkim_keys.add(
            m.DkimKey(
                account_id=domain.account_id,
                domain_id=domain.id,
                selector=f"mv{now:%Y%m}{new_token()[:6].lower().replace('-', 'x').replace('_', 'y')}",
                private_key_encrypted=encrypt_secret(pair.private_pem),
                public_key=pair.public_dns_value,
                status=status,
                activated_at=now if status == "active" else None,
            )
        )

    def get(self, ctx: AccountContext, domain_id: uuid.UUID) -> m.Domain:
        domain = self.uow.domains.get(domain_id, ctx.account_id)
        if domain is None:
            raise NotFound("Domain not found.")
        return domain

    def list_page(self, ctx: AccountContext, page: PageRequest) -> Page[DomainView]:
        result = self.uow.domains.list_page(ctx.account_id, page)
        return Page(items=[self.view(domain) for domain in result.items], next_cursor=result.next_cursor)

    # ---- Records ----

    def _keys(self, domain: m.Domain) -> tuple[m.DkimKey | None, m.DkimKey | None]:
        keys = self.uow.dkim_keys.list_for_domain(domain.id)
        active = next((k for k in keys if k.status == "active"), None)
        pending = next((k for k in keys if k.status == "pending"), None)
        return active, pending

    def expected_records(self, domain: m.Domain) -> list[DnsRecord]:
        active, pending = self._keys(domain)
        records: list[DnsRecord] = []
        if active:
            records.append(DnsRecord(
                key="dkim", purpose="DKIM signature (OpenDKIM)", type="TXT",
                name=f"{active.selector}._domainkey.{domain.name}",
                value=f"v=DKIM1; k=rsa; p={active.public_key}", required=True,
                note="Long values may need to be split into several quoted strings by your DNS provider.",
            ))
        if pending:
            records.append(DnsRecord(
                key="dkim_next", purpose="Next DKIM key (rotation) – add it now, it becomes active once found",
                type="TXT", name=f"{pending.selector}._domainkey.{domain.name}",
                value=f"v=DKIM1; k=rsa; p={pending.public_key}", required=False,
            ))
        records += [
            DnsRecord(
                key="return_path_mx", purpose="Return-Path (MAIL FROM) – receives bounces", type="MX",
                name=domain.return_path_host, value=f"10 {self.settings.return_path_mx_host}", required=True,
            ),
            DnsRecord(
                key="spf", purpose="SPF for the Return-Path domain", type="TXT",
                name=domain.return_path_host,
                value=f"v=spf1 include:{self.settings.spf_include_domain} -all", required=True,
            ),
            DnsRecord(
                key="dmarc", purpose="DMARC policy (recommended; required for bulk sending)", type="TXT",
                name=f"_dmarc.{domain.name}",
                value=f"v=DMARC1; p=none; rua=mailto:dmarc-reports@{domain.name}; adkim=r; aspf=r",
                required=False,
                note="Start with p=none, then move to quarantine/reject. Keep aspf=r so SPF aligns.",
            ),
        ]
        return records

    def view(self, domain: m.Domain) -> DomainView:
        records = self.expected_records(domain)
        stored = domain.record_status or {}
        for record in records:
            saved = stored.get(record.key)
            if saved and saved.get("name") == record.name:
                record.status = saved.get("status", "unchecked")
                record.found = saved.get("found", [])
        active, _ = self._keys(domain)
        return DomainView(domain=domain, records=records, active_selector=active.selector if active else None)

    # ---- Verification ----

    def _check(self, record: DnsRecord) -> None:
        try:
            if record.type == "MX":
                found = self.resolver.mx(record.name)
                record.found = found
                expected = self.settings.return_path_mx_host.lower().rstrip(".")
                record.status = "missing" if not found else ("verified" if expected in found else "mismatched")
                return
            found = self.resolver.txt(record.name)
        except DnsLookupError as error:
            record.status, record.found, record.note = "missing", [], f"Lookup failed: {error}"
            return
        if record.key in ("dkim", "dkim_next"):
            candidates = [txt for txt in found if "p=" in txt.lower()]
            record.found = candidates
            want = _squash(record.value.split("p=", 1)[1])
            ok = any(_squash(_tags(txt).get("p", "")) == want for txt in candidates)
            record.status = "missing" if not candidates else ("verified" if ok else "mismatched")
        elif record.key == "spf":
            spf = [txt for txt in found if txt.lower().startswith("v=spf1")]
            record.found = spf
            include = f"include:{self.settings.spf_include_domain}".lower()
            ok = len(spf) == 1 and include in spf[0].lower().split()
            record.status = "missing" if not spf else ("verified" if ok else "mismatched")
            if len(spf) > 1:
                record.note = "More than one SPF record – merge them into one."
        elif record.key == "dmarc":
            dmarc = [txt for txt in found if txt.lower().replace(" ", "").startswith("v=dmarc1")]
            record.found = dmarc
            record.status = "missing" if not dmarc else ("verified" if len(dmarc) == 1 and _tags(dmarc[0]).get("p")
                                                         else "mismatched")

    def check_dns(self, domain: m.Domain) -> DomainView:
        """Looks up every record, updates the domain's status and returns the result."""
        records = self.expected_records(domain)
        for record in records:
            self._check(record)
        now = utcnow()

        # Rotation: the next key takes over once its record is published.
        rotated = next((r for r in records if r.key == "dkim_next" and r.status == "verified"), None)
        if rotated:
            active, pending = self._keys(domain)
            if pending:
                if active:
                    active.status, active.retired_at = "retired", now
                pending.status, pending.activated_at = "active", now
                audit(self.uow, domain.account_id, Actor.system(), "domain.dkim_rotated", target_type="domain",
                      target_id=domain.id, selector=pending.selector)
                records = self.expected_records(domain)
                for record in records:
                    self._check(record)

        domain.record_status = {
            r.key: {"name": r.name, "status": r.status, "found": r.found[:5], "checked_at": now.isoformat()}
            for r in records
        }
        domain.last_checked_at = now
        all_required = all(r.status == "verified" for r in records if r.required)
        dmarc = next((r for r in records if r.key == "dmarc"), None)
        previous = domain.status
        if domain.status != "disabled":
            domain.status = "verified" if all_required else "pending"
            if domain.status == "verified" and previous != "verified":
                domain.verified_at = now
        # Bulk: DKIM (d=domain) and SPF (relaxed, Return-Path subdomain) must align under DMARC.
        aligned_spf = True
        if dmarc and dmarc.status == "verified":
            aligned_spf = _tags(dmarc.found[0]).get("aspf", "r").lower() != "s"
        domain.bulk_eligible = bool(domain.status == "verified" and dmarc and dmarc.status == "verified"
                                    and aligned_spf)
        if dmarc and dmarc.status == "verified" and not aligned_spf:
            dmarc.note = "aspf=s (strict) – the Return-Path subdomain won't align. Use aspf=r for bulk sending."

        if previous != domain.status:
            action = "domain.verified" if domain.status == "verified" else "domain.verification_lost"
            audit(self.uow, domain.account_id, Actor.system(), action, target_type="domain", target_id=domain.id,
                  records={r.key: r.status for r in records})
            log_event(log, action, logging.INFO if domain.status == "verified" else logging.WARNING,
                      domain_id=str(domain.id))
        active, _ = self._keys(domain)
        return DomainView(domain=domain, records=records, active_selector=active.selector if active else None)

    def verify(self, ctx: AccountContext, domain_id: uuid.UUID) -> DomainView:
        domain = self.get(ctx, domain_id)
        if domain.status == "disabled":
            raise Conflict("Enable the domain before verifying it.", code="domain_disabled")
        view = self.check_dns(domain)
        self.uow.commit()
        return view

    def recheck_due(self, batch: int = 50) -> int:
        """Scheduled: re-verify verified domains whose last check is older than the interval."""
        before = utcnow() - timedelta(minutes=self.settings.dns_recheck_interval_minutes)
        domains = self.uow.domains.list_due_for_recheck(before, batch)
        for domain in domains:
            self.check_dns(domain)
        self.uow.commit()
        return len(domains)

    # ---- State ----

    def set_disabled(self, ctx: AccountContext, domain_id: uuid.UUID, disabled: bool) -> DomainView:
        domain = self.get(ctx, domain_id)
        if disabled and domain.status != "disabled":
            domain.status = "disabled"
            domain.bulk_eligible = False
            audit(self.uow, ctx.account_id, ctx.actor, "domain.disabled", target_type="domain", target_id=domain.id)
        elif not disabled and domain.status == "disabled":
            domain.status = "pending"
            audit(self.uow, ctx.account_id, ctx.actor, "domain.enabled", target_type="domain", target_id=domain.id)
        self.uow.commit()
        return self.view(domain)

    def rotate_dkim(self, ctx: AccountContext, domain_id: uuid.UUID) -> DomainView:
        domain = self.get(ctx, domain_id)
        _, pending = self._keys(domain)
        if pending:
            raise Conflict("A new DKIM key is already waiting for its DNS record.", code="rotation_pending")
        key = self._new_key(domain, status="pending")
        audit(self.uow, ctx.account_id, ctx.actor, "domain.dkim_rotation_started", target_type="domain",
              target_id=domain.id, selector=key.selector)
        self.uow.commit()
        return self.view(domain)

    # ---- Sender identities ----

    def create_identity(self, ctx: AccountContext, domain_id: uuid.UUID, email: str,
                        display_name: str | None) -> m.SenderIdentity:
        ctx.require_owner()
        domain = self.get(ctx, domain_id)
        address = normalize_email(email)
        if address.rsplit("@", 1)[1] != domain.name:
            raise ValidationFailed([field_error("email", f"Use an address at {domain.name}.", "domain_mismatch")])
        if self.uow.sender_identities.get_by_email(ctx.account_id, address):
            raise Conflict("This sender already exists.", code="identity_exists")
        identity = self.uow.sender_identities.add(
            m.SenderIdentity(
                account_id=ctx.account_id,
                domain_id=domain.id,
                email=address,
                display_name=(display_name or "").strip() or None,
                status="pending",
                created_by=ctx.require_user().id,
            )
        )
        audit(self.uow, ctx.account_id, ctx.actor, "sender_identity.created", target_type="sender_identity",
              target_id=identity.id, email=address)
        self.uow.commit()
        return identity

    def get_identity(self, ctx: AccountContext, identity_id: uuid.UUID) -> m.SenderIdentity:
        identity = self.uow.sender_identities.get(identity_id, ctx.account_id)
        if identity is None:
            raise NotFound("Sender not found.")
        return identity

    def list_identities(self, ctx: AccountContext, page: PageRequest) -> Page[m.SenderIdentity]:
        return self.uow.sender_identities.list_page(ctx.account_id, page)

    def verify_identity(self, ctx: AccountContext, identity_id: uuid.UUID) -> m.SenderIdentity:
        ctx.require_owner()
        identity = self.get_identity(ctx, identity_id)
        if identity.status == "disabled":
            raise Conflict("Enable this sender first.", code="identity_disabled")
        domain = self.uow.domains.get(identity.domain_id, ctx.account_id)
        if domain is None or domain.status != "verified":
            raise ApiError("Verify the domain's DNS records first.", code="domain_not_verified", status_code=409)
        if identity.status != "verified":
            identity.status = "verified"
            identity.verified_at = utcnow()
            audit(self.uow, ctx.account_id, ctx.actor, "sender_identity.verified", target_type="sender_identity",
                  target_id=identity.id)
        self.uow.commit()
        return identity

    def set_identity_disabled(self, ctx: AccountContext, identity_id: uuid.UUID, disabled: bool) -> m.SenderIdentity:
        ctx.require_owner()
        identity = self.get_identity(ctx, identity_id)
        if disabled and identity.status != "disabled":
            identity.status, identity.disabled_at = "disabled", utcnow()
            audit(self.uow, ctx.account_id, ctx.actor, "sender_identity.disabled", target_type="sender_identity",
                  target_id=identity.id)
        elif not disabled and identity.status == "disabled":
            identity.status, identity.disabled_at, identity.verified_at = "pending", None, None
            audit(self.uow, ctx.account_id, ctx.actor, "sender_identity.enabled", target_type="sender_identity",
                  target_id=identity.id)
        self.uow.commit()
        return identity

    def identity_history(self, ctx: AccountContext, identity_id: uuid.UUID, page: PageRequest) -> Page[m.AuditEvent]:
        identity = self.get_identity(ctx, identity_id)
        return self.uow.audit.list_page(ctx.account_id, page, target_type="sender_identity", target_id=identity.id)


def ensure_member_may_manage_domains(ctx: AccountContext) -> None:
    """Owners and members both manage domains; API keys may only read them."""
    if ctx.api_key is not None:
        raise Forbidden("API keys can't change domains.", code="user_required")
