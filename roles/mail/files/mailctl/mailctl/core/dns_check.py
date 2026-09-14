"""Whether a domain's DNS delivers its mail to this server and vouches for mail sent from it."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Protocol

import dns.exception
import dns.name
import dns.resolver

from . import dkim

IPAddress = IPv4Address | IPv6Address
SPF_LOOKUP_LIMIT = 10
# Valid SPF mechanisms that depend on the sender or on reverse DNS, so they can't be judged here.
_UNEVALUABLE_MECHANISMS = ("exists", "ptr")
_A_OR_MX = re.compile(r"(?P<kind>a|mx)(?::(?P<host>[^/]+))?(?:/(?P<ipv4>\d+))?(?://(?P<ipv6>\d+))?")


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
    fix: DnsRecord | None = None  # the record to publish when that would solve the problem


class LookupFailed(Exception):
    """A lookup failed for another reason than the record not existing."""


class Resolver(Protocol):
    def txt(self, name: str) -> list[str]: ...

    def mx(self, name: str) -> list[str]: ...

    def addresses(self, name: str) -> set[IPAddress]: ...


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

    def _query(self, name: str, kind: str) -> list:
        try:
            return list(self._resolver.resolve(name, kind))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as error:
            raise LookupFailed(f"The {kind} lookup for {name} failed: {error}") from None


def recommended_records(domain: str, hostname: str, dkim_value: str | None) -> list[DnsRecord]:
    """The records a domain on this server needs. The DKIM record is left out while the domain has no key."""
    dkim_records = [dkim_record(domain, dkim_value)] if dkim_value else []
    return [_mx_record(domain, hostname), _spf_record(domain), *dkim_records, _dmarc_record(domain)]


def dkim_record(domain: str, value: str) -> DnsRecord:
    return DnsRecord("TXT", dkim.record_name(domain), value)


@dataclass(frozen=True)
class _Domain:
    name: str
    hostname: str
    server_ips: set[IPAddress]
    dkim_value: str | None
    resolver: Resolver


# What a check finds: a status, the explanation, and the record to publish if that would solve the problem.
_Finding = tuple[Status, str, DnsRecord | None]


def check_domain(
    domain: str, *, hostname: str, server_ips: set[IPAddress], dkim_value: str | None, resolver: Resolver
) -> list[Check]:
    target = _Domain(domain, hostname, server_ips, dkim_value, resolver)
    checks: list[tuple[str, Callable[[_Domain], _Finding]]] = [
        ("MX", _check_mx), ("SPF", _check_spf), ("DKIM", _check_dkim), ("DMARC", _check_dmarc),
    ]
    results = []
    for name, check in checks:
        try:
            status, detail, fix = check(target)
        except LookupFailed as error:
            status, detail, fix = Status.WARN, str(error), None
        results.append(Check(name, status, detail, fix))
    return results


def _mx_record(domain: str, hostname: str) -> DnsRecord:
    return DnsRecord("MX", domain, f"10 {hostname}")


def _spf_record(domain: str) -> DnsRecord:
    return DnsRecord("TXT", domain, "v=spf1 mx ~all")


def _dmarc_record(domain: str) -> DnsRecord:
    return DnsRecord("TXT", f"_dmarc.{domain}", "v=DMARC1; p=quarantine")


def _check_mx(domain: _Domain) -> _Finding:
    hosts = domain.resolver.mx(domain.name)
    fix = _mx_record(domain.name, domain.hostname)
    if not hosts:
        return Status.FAIL, "There is no MX record, so mail for the domain can't be delivered.", fix
    if any(domain.resolver.addresses(host) & domain.server_ips for host in hosts):
        return Status.OK, f"Mail is delivered to {', '.join(hosts)}.", None
    return Status.FAIL, f"Mail is delivered to {', '.join(hosts)}, which isn't this server.", fix


def _check_spf(domain: _Domain) -> _Finding:
    fix = _spf_record(domain.name)
    records = _spf_records(domain.name, domain.resolver)
    if len(records) != 1:
        problem = "There is no SPF record" if not records else f"There are {len(records)} SPF records instead of one"
        return Status.FAIL, f"{problem}, so receiving servers can't verify mail from this server.", fix
    # Private addresses never reach other mail servers.
    ips = {ip for ip in domain.server_ips if ip.is_global} or domain.server_ips
    try:
        refused = sorted((ip for ip in ips if not _SpfEvaluation(domain.resolver).allows(domain.name, ip)), key=str)
    except _Undecided as undecided:
        return undecided.status, str(undecided), None
    if refused:
        return Status.FAIL, f"The SPF record doesn't allow this server's address {', '.join(map(str, refused))}.", fix
    return Status.OK, "The SPF record allows this server to send mail for the domain.", None


def _check_dkim(domain: _Domain) -> _Finding:
    if domain.dkim_value is None:
        detail = f"This server has no DKIM key for the domain. Create one with: mailctl dkim create {domain.name}"
        return Status.FAIL, detail, None
    fix = dkim_record(domain.name, domain.dkim_value)
    published = [_public_key(record) for record in domain.resolver.txt(fix.name)]
    if _public_key(domain.dkim_value) in published:
        return Status.OK, "The published key matches this server's key.", None
    if not any(published):
        return Status.FAIL, f"There is no DKIM record at {fix.name}.", fix
    return Status.FAIL, f"The DKIM record at {fix.name} has another key than this server.", fix


def _check_dmarc(domain: _Domain) -> _Finding:
    fix = _dmarc_record(domain.name)
    # A subdomain without a DMARC record of its own follows the policy of its parent domain.
    labels = domain.name.split(".")
    for name in (".".join(labels[start:]) for start in range(len(labels) - 1)):
        records = [record for record in domain.resolver.txt(f"_dmarc.{name}") if _is_dmarc(record)]
        if len(records) > 1:
            return Status.FAIL, f"There are {len(records)} DMARC records for {name}, so receiving servers ignore them.", fix
        if records:
            source = "" if name == domain.name else f", set for {name}"
            return Status.OK, f"Policy: {_tag(records[0], 'p') or 'none'}{source}.", None
    return Status.WARN, "There is no DMARC record. Mail still arrives, but some providers trust it less.", fix


def _spf_records(domain: str, resolver: Resolver) -> list[str]:
    return [record for record in resolver.txt(domain) if record.lower().split()[:1] == ["v=spf1"]]


def _is_dmarc(record: str) -> bool:
    """Whether the record starts with the v=DMARC1 tag, as a DMARC record must."""
    return "".join(record.split(";", 1)[0].split()).upper() == "V=DMARC1"


def _tag(record: str, name: str) -> str:
    """A tag's value in a semicolon-separated record, like DKIM's p= or DMARC's p=."""
    for tag in record.split(";"):
        key, _, value = tag.partition("=")
        if key.strip().lower() == name:
            return value.strip()
    return ""


def _public_key(record: str) -> str:
    """The DKIM key without the spaces DNS control panels sometimes add."""
    return "".join(_tag(record, "p").split())


class _Undecided(Exception):
    """The SPF record can't be evaluated to a yes or no; the status says how bad that is."""

    def __init__(self, status: Status, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class _SpfEvaluation:
    """Evaluates SPF for one address the way receiving servers do, for the mechanisms that don't depend on the sender."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._lookups = 0

    def allows(self, domain: str, ip: IPAddress) -> bool:
        records = _spf_records(domain, self._resolver)
        if len(records) != 1:
            count = f"{len(records)} SPF records" if records else "no SPF record"
            raise _Undecided(Status.FAIL, f"The SPF record refers to {domain}, which has {count}.")
        redirect = None
        for term in records[0].lower().split()[1:]:
            if term.startswith("redirect="):
                redirect = _without_macros(term.removeprefix("redirect="))
            elif "=" not in term:  # other modifiers, like exp=, don't decide anything
                qualifier, mechanism = (term[0], term[1:]) if term[0] in "+-~?" else ("+", term)
                if self._matches(_without_macros(mechanism), domain, ip):
                    return qualifier == "+"
        if redirect:
            self._count_lookup()
            return self.allows(redirect, ip)
        return False

    def _matches(self, mechanism: str, domain: str, ip: IPAddress) -> bool:
        kind, _, argument = mechanism.partition(":")
        if kind == "all":
            return True
        if kind in ("ip4", "ip6"):
            network = _network(argument)
            return network.version == ip.version and ip in network
        if kind == "include":
            self._count_lookup()
            return self.allows(argument, ip)
        a_or_mx = _A_OR_MX.fullmatch(mechanism)
        if a_or_mx:
            self._count_lookup()
            host = a_or_mx["host"] or domain
            hosts = [host] if a_or_mx["kind"] == "a" else self._resolver.mx(host)
            prefix = a_or_mx["ipv4"] if ip.version == 4 else a_or_mx["ipv6"]
            return any(
                ip in _network(f"{address}/{prefix or address.max_prefixlen}")
                for mail_host in hosts
                for address in self._resolver.addresses(mail_host)
                if address.version == ip.version
            )
        name = re.split(r"[:/]", mechanism, maxsplit=1)[0]
        if name in _UNEVALUABLE_MECHANISMS:
            raise _Undecided(Status.WARN, f"The SPF record uses '{name}', which mailctl can't evaluate.")
        raise _Undecided(Status.FAIL,
                         f"The SPF record contains '{mechanism}', which isn't valid SPF, so receiving servers reject it.")

    def _count_lookup(self) -> None:
        self._lookups += 1
        if self._lookups > SPF_LOOKUP_LIMIT:
            raise _Undecided(Status.FAIL,
                             f"The SPF record needs more than {SPF_LOOKUP_LIMIT} DNS lookups, so receiving servers reject it.")


def _without_macros(term: str) -> str:
    if "%" in term:
        raise _Undecided(Status.WARN, "The SPF record uses macros, which mailctl can't evaluate.")
    return term


def _network(text: str):
    try:
        return ip_network(text, strict=False)
    except ValueError:
        raise _Undecided(Status.FAIL, f"The SPF record contains an invalid address: {text}.") from None
