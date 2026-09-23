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

    def _query(self, name: str, kind: str) -> list:
        try:
            return list(self._resolver.resolve(name, kind))
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
