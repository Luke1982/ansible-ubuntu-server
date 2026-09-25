"""Looking names up in DNS, and the shapes the tools report their findings in."""

from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol

import dns.exception
import dns.name
import dns.resolver
import dns.reversename

IPAddress = IPv4Address | IPv6Address


class Status(Enum):
    OK = "ok"
    WARN = "warning"
    FAIL = "problem"


@dataclass(frozen=True)
class DnsRecord:
    type: str
    name: str
    value: str


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fixes: tuple[DnsRecord, ...] = ()  # the records to publish when that would solve the problem


@dataclass(frozen=True)
class Srv:
    priority: int
    weight: int
    port: int
    target: str  # "." when the service isn't offered


class LookupFailed(Exception):
    """A lookup failed for another reason than the record not existing."""


class NotPointingHere(Exception):
    """The name has no address, or an address that isn't this server's."""


class Resolver(Protocol):
    def txt(self, name: str) -> list[str]: ...

    def mx(self, name: str) -> list[str]: ...

    def addresses(self, name: str) -> set[IPAddress]: ...

    def srv(self, name: str) -> list[Srv]: ...

    def ptr(self, address: IPAddress) -> list[str]: ...


class SystemResolver:
    def __init__(self, timeout: float = 5.0) -> None:
        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = timeout
        self._timeout = timeout

    def txt(self, name: str) -> list[str]:
        return [b"".join(answer.strings).decode(errors="replace") for answer in self._query(name, "TXT")]

    def mx(self, name: str) -> list[str]:
        """The mail hosts, most preferred first."""
        answers = sorted(self._query(name, "MX"), key=lambda answer: answer.preference)
        return [answer.exchange.to_text(omit_final_dot=True) for answer in answers if answer.exchange != dns.name.root]

    def addresses(self, name: str) -> set[IPAddress]:
        return {ip_address(answer.address) for kind in ("A", "AAAA") for answer in self._query(name, kind)}

    def srv(self, name: str) -> list[Srv]:
        return [
            Srv(answer.priority, answer.weight, answer.port,
                "." if answer.target == dns.name.root else answer.target.to_text(omit_final_dot=True))
            for answer in self._query(name, "SRV")
        ]

    def ptr(self, address: IPAddress) -> list[str]:
        name = dns.reversename.from_address(str(address)).to_text()
        return [answer.target.to_text(omit_final_dot=True) for answer in self._query(name, "PTR")]

    def addresses_at_nameservers(self, name: str) -> set[IPAddress]:
        """Where the name points according to the nameservers of its own zone, asked directly.

        This machine's resolver answers from its cache, so a record changed a moment ago can still look like the
        old one for as long as that answer lives. The nameservers of the zone are the ones that decide, and the
        rest of the internet follows them as its own caches expire.
        """
        servers = [str(ip) for ip in self._nameservers_of(name)]
        if not servers:
            return set()
        return {ip_address(answer.address) for kind in ("A", "AAAA") for answer in self._query(name, kind, servers)}

    def _nameservers_of(self, name: str) -> set[IPAddress]:
        """The addresses of the nameservers of the closest zone the name is in."""
        labels = name.split(".")
        for start in range(len(labels) - 1):
            hosts = [answer.target.to_text(omit_final_dot=True)
                     for answer in self._query(".".join(labels[start:]), "NS")]
            found = {ip_address(answer.address)
                     for host in hosts for kind in ("A", "AAAA") for answer in self._query(host, kind)}
            if found:
                return found
        return set()

    def _query(self, name: str, kind: str, servers: list[str] | None = None) -> list:
        resolver = self._resolver
        if servers is not None:
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = servers
            resolver.lifetime = self._timeout
        try:
            return list(resolver.resolve(name, kind))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as error:
            raise LookupFailed(f"The {kind} lookup for {name} failed: {error}") from None


def resolve_to_this_server(resolver: Resolver, name: str, server_ips: set[IPAddress]) -> set[IPAddress]:
    """The name's addresses, when every one of them is this server's: Let's Encrypt and visitors may use any of
    them, so one foreign address is enough to fail a certificate request. Raises NotPointingHere otherwise, and
    LookupFailed when the lookup itself fails."""
    addresses = resolver.addresses(name)
    if not addresses:
        raise NotPointingHere(f"{name} has no A or AAAA record.")
    foreign = sorted(addresses - server_ips, key=lambda ip: (ip.version, ip))
    if foreign:
        also = " also" if len(foreign) < len(addresses) else ""
        raise NotPointingHere(f"{name}{also} points to {', '.join(map(str, foreign))}, which isn't this server.")
    return addresses


def address_records(name: str, ips: set[IPAddress]) -> tuple[DnsRecord, ...]:
    """The A and AAAA records that point the name at the addresses."""
    return tuple(DnsRecord("A" if ip.version == 4 else "AAAA", name, str(ip))
                 for ip in sorted(ips, key=lambda ip: (ip.version, ip)))
