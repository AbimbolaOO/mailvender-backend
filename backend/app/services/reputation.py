"""Sender reputation: DNS blocklists for our IPs and domain, and for links in customers' emails.

Mailbox providers reject or junk mail from an IP or domain on a major blocklist (Spamhaus,
SpamCop, Barracuda), and one bad customer can get our shared IP listed. So:

* the `reputation` job checks every sending IP (KNOWN_SENDING_IPS) against BLOCKLIST_IP_ZONES and
  the platform domain against BLOCKLIST_DOMAIN_ZONES each hour; the result is kept in
  `system_state` and shown (and alerted on) by the admin's health checks;
* at send time, every domain linked from a customer's email is looked up in LINK_BLOCKLIST_ZONES
  (Spamhaus DBL lists phishing, malware and spam domains): a listed link stops the send, before
  our IP's reputation pays for it.

A blocklist answers a query for `<reversed IP or domain>.<zone>` with an address in 127.0.0.0/8
when the target is listed. 127.255.255.x means the query itself was refused (e.g. sent through a
public resolver Spamhaus doesn't serve) – that's "unknown", never "listed".
"""

import ipaddress
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal

from app.config import get_settings
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.dns import DnsLookupError, DnsResolver

log = get_logger("reputation")

Status = Literal["listed", "clean", "unknown"]
STATE_KEY = "blocklists"
MAX_LINK_DOMAINS = 25
LINK_CACHE_SECONDS = 3600
# Second-level labels under which domains are registered one level deeper (example.co.uk).
SECOND_LEVELS = {"co", "com", "net", "org", "ac", "gov", "edu", "ltd", "plc", "me", "or", "ne", "go"}
URL = re.compile(r"""https?://([^/\s"'<>?#:]+)""", re.IGNORECASE)

# Where to ask for a listing to be removed.
DELIST = {
    "zen.spamhaus.org": "https://check.spamhaus.org/",
    "dbl.spamhaus.org": "https://check.spamhaus.org/",
    "bl.spamcop.net": "https://www.spamcop.net/bl.shtml",
    "b.barracudacentral.org": "https://www.barracudacentral.org/rbl/removal-request",
}


@dataclass(frozen=True)
class Listing:
    target: str
    zone: str
    status: Status
    answer: str | None = None


def lookup(resolver: DnsResolver, name: str) -> tuple[Status, str | None]:
    try:
        answers = resolver.a(name)
    except DnsLookupError:
        return "unknown", None
    if not answers:
        return "clean", None
    if all(a.startswith("127.255.255.") for a in answers):
        return "unknown", answers[0]  # the blocklist refused the query
    listed = [a for a in answers if a.startswith("127.")]
    return ("listed", listed[0]) if listed else ("unknown", answers[0])


def ip_query(ip: str, zone: str) -> str | None:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version != 4:
        return None  # these blocklists are queried for IPv4; we send over IPv4 only
    return ".".join(reversed(ip.split("."))) + "." + zone


def base_domain(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVELS and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def link_domains(*bodies: str | None) -> list[str]:
    """Registered domains linked from the email (http/https links), first seen first."""
    found: dict[str, None] = {}
    for body in bodies:
        for host in URL.findall(unescape(body or "")):
            host = host.lower().rstrip(".")
            if "." not in host or "{" in host:
                continue
            try:
                ipaddress.ip_address(host)
                continue  # raw IP links aren't in a domain blocklist
            except ValueError:
                pass
            found[base_domain(host)] = None
    return list(found)


_link_cache: dict[tuple[str, str], tuple[float, Status]] = {}


class ReputationService:
    def __init__(self, uow: UnitOfWork, resolver: DnsResolver):
        self.uow = uow
        self.resolver = resolver
        self.settings = get_settings()

    def check(self) -> list[Listing]:
        """Scheduled: looks up our IPs and domain on every configured blocklist and stores the result."""
        results: list[Listing] = []
        for ip in sorted(self.settings.known_sending_ip_set):
            for zone in self.settings.blocklist_ip_zone_list:
                name = ip_query(ip, zone)
                if name:
                    status, answer = lookup(self.resolver, name)
                    results.append(Listing(ip, zone, status, answer))
        for zone in self.settings.blocklist_domain_zone_list:
            status, answer = lookup(self.resolver, f"{self.settings.platform_domain}.{zone}")
            results.append(Listing(self.settings.platform_domain, zone, status, answer))
        previous = {(r["target"], r["zone"]): r for r in (self.latest() or {}).get("results", [])}
        now = utcnow()
        stored = []
        for item in results:
            since = previous.get((item.target, item.zone), {}).get("since") if item.status == "listed" else None
            stored.append({"target": item.target, "zone": item.zone, "status": item.status, "answer": item.answer,
                           "since": since or (now.isoformat() if item.status == "listed" else None)})
            if item.status == "listed":
                log_event(log, "reputation.listed", target=item.target, zone=item.zone, answer=item.answer)
        self.uow.system_state.put(STATE_KEY, {"checked_at": now.isoformat(), "results": stored}, now)
        self.uow.commit()
        return results

    def latest(self) -> dict[str, Any] | None:
        return self.uow.system_state.get(STATE_KEY)

    def listed_links(self, html: str | None, text: str | None) -> list[str]:
        """Domains linked from an email that a link blocklist lists (cached for an hour per domain)."""
        zones = self.settings.link_blocklist_zone_list
        if not zones:
            return []
        listed = []
        now = time.monotonic()
        for domain in link_domains(html, text)[:MAX_LINK_DOMAINS]:
            if domain == self.settings.platform_domain:
                continue
            for zone in zones:
                cached = _link_cache.get((domain, zone))
                if cached and cached[0] > now:
                    status = cached[1]
                else:
                    status, _ = lookup(self.resolver, f"{domain}.{zone}")
                    _link_cache[(domain, zone)] = (now + LINK_CACHE_SECONDS, status)
                if status == "listed":
                    listed.append(domain)
                    break
        return listed
