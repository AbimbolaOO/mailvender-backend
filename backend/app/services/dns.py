"""DNS lookups behind an interface, so verification can be tested without the network."""

from typing import Protocol

import dns.exception
import dns.resolver

from app.config import get_settings


class DnsLookupError(Exception):
    """The lookup itself failed (timeout, SERVFAIL) – not the same as "no record"."""


class DnsResolver(Protocol):
    def txt(self, name: str) -> list[str]: ...
    def mx(self, name: str) -> list[str]: ...


class SystemDnsResolver:
    def __init__(self) -> None:
        self.resolver = dns.resolver.Resolver()
        nameservers = [ns.strip() for ns in get_settings().dns_nameservers.split(",") if ns.strip()]
        if nameservers:
            self.resolver.nameservers = nameservers
        self.resolver.lifetime = 5.0

    def _query(self, name: str, rdtype: str) -> list[dns.resolver.Answer] | dns.resolver.Answer | None:
        try:
            return self.resolver.resolve(name, rdtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return None
        except dns.exception.DNSException as error:
            raise DnsLookupError(str(error)) from error

    def txt(self, name: str) -> list[str]:
        answer = self._query(name, "TXT")
        if not answer:
            return []
        # A TXT record may be split into several strings; they're concatenated.
        return [b"".join(record.strings).decode(errors="replace") for record in answer]  # type: ignore[union-attr]

    def mx(self, name: str) -> list[str]:
        answer = self._query(name, "MX")
        if not answer:
            return []
        return [str(record.exchange).rstrip(".").lower() for record in answer]  # type: ignore[union-attr]
