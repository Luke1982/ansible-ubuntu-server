"""Whether a domain's DNS delivers its mail to this server, vouches for mail sent from it and tells mail programs
where to connect, and whether the server's own name and addresses point to each other."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Protocol

import dns.exception
import dns.name
import dns.resolver
import dns.reversename

from . import dkim

IPAddress = IPv4Address | IPv6Address
SPF_LOOKUP_LIMIT = 10
# Valid SPF mechanisms that depend on the sender or on reverse DNS, so they can't be judged here.
_UNEVALUABLE_MECHANISMS = ("exists", "ptr")
_A_OR_MX = re.compile(r"(?P<kind>a|mx)(?::(?P<host>[^/]+))?(?:/(?P<ipv4>\d+))?(?://(?P<ipv6>\d+))?")
# The services mail programs look up (RFC 6186 and 8314) as (name, priority, port). Implicit TLS comes first.
MAIL_SERVICES = (("_imaps._tcp", 0, 993), ("_imap._tcp", 10, 143), ("_submissions._tcp", 0, 465), ("_submission._tcp", 10, 587))
# What calendar and contact apps look up to find the webmail site from an email address alone (RFC 6764).
DAV_SERVICES = ("_caldavs._tcp", "_carddavs._tcp")
DAV_PORT = 443
WEBMAIL_PREFIX = "webmail."
AUTODISCOVER_PREFIX = "autodiscover."


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


def resolve_to_this_server(resolver: "Resolver", name: str, server_ips: set[IPAddress]) -> set[IPAddress]:
    """The name's addresses, when every one of them is this server's: Let's Encrypt and visitors may use any of
    them. Raises NotPointingHere otherwise, and LookupFailed when the lookup fails."""
    addresses = resolver.addresses(name)
    if not addresses:
        raise NotPointingHere(f"{name} has no A or AAAA record.")
    foreign = sorted(addresses - server_ips, key=lambda ip: (ip.version, ip))
    if foreign:
        also = " also" if len(foreign) < len(addresses) else ""
        raise NotPointingHere(f"{name}{also} points to {', '.join(map(str, foreign))}, which isn't this server.")
    return addresses


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
        """Where a name points, as other servers on the internet see it.

        A server has its own hostname in /etc/hosts, on 127.0.1.1, and systemd-resolved answers from that file, so
        the machine's own resolver says the hostname points at the loopback address. That says nothing about what
        anyone else gets, so a loopback answer is put aside and the name's own nameservers are asked instead.
        """
        found = {ip_address(answer.address) for kind in ("A", "AAAA") for answer in self._query(name, kind)}
        elsewhere = {ip for ip in found if not ip.is_loopback}
        if elsewhere or not found:
            return elsewhere
        return {ip for ip in self._at_nameservers(name) if not ip.is_loopback}

    def srv(self, name: str) -> list[Srv]:
        return [
            Srv(answer.priority, answer.weight, answer.port,
                "." if answer.target == dns.name.root else answer.target.to_text(omit_final_dot=True))
            for answer in self._query(name, "SRV")
        ]

    def ptr(self, address: IPAddress) -> list[str]:
        name = dns.reversename.from_address(str(address)).to_text()
        return [answer.target.to_text(omit_final_dot=True) for answer in self._query(name, "PTR")]

    def _at_nameservers(self, name: str) -> set[IPAddress]:
        """The addresses the name's own nameservers give for it, asked directly."""
        servers = [str(ip) for ip in self._nameservers_of(name)]
        if not servers:
            return set()
        return {ip_address(answer.address) for kind in ("A", "AAAA") for answer in self._query(name, kind, servers)}

    def _nameservers_of(self, name: str) -> set[IPAddress]:
        """The addresses of the nameservers of the closest zone the name is in."""
        labels = name.split(".")
        for start in range(len(labels) - 1):
            hosts = [answer.target.to_text(omit_final_dot=True) for answer in self._query(".".join(labels[start:]), "NS")]
            # Straight from this machine's resolver: a nameserver's own name isn't the one in /etc/hosts.
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


def recommended_records(domain: str, server_ips: set[IPAddress], dkim_value: str | None) -> list[DnsRecord]:
    """The records a domain on this server needs. The DKIM record is left out while the domain has no key, and the
    mail host's addresses while this server's addresses are unknown."""
    dkim_records = [dkim_record(domain, dkim_value)] if dkim_value else []
    return [
        _mx_record(domain),
        *_address_records(domain, server_ips),
        _spf_record(domain),
        *dkim_records,
        _dmarc_record(domain),
        *srv_records(domain),
        *webmail_records(domain, server_ips),
    ]


def srv_records(domain: str) -> list[DnsRecord]:
    """The records that tell mail programs to use the mail host for IMAP and for sending."""
    return [
        DnsRecord("SRV", f"{service}.{domain}", f"{priority} 1 {port} {mail_host(domain)}.")
        for service, priority, port in MAIL_SERVICES
    ]


def webmail_records(domain: str, server_ips: set[IPAddress]) -> list[DnsRecord]:
    """The records for the webmail site: its addresses, and what calendar and contact apps look up to find it."""
    return [
        *_host_records(webmail_host(domain), _reachable(server_ips)),
        *(DnsRecord("SRV", f"{service}.{domain}", f"0 1 {DAV_PORT} {webmail_host(domain)}.")
          for service in DAV_SERVICES),
    ]


def autodetect_records(domain: str, server_ips: set[IPAddress]) -> list[DnsRecord]:
    """The records for the site that hands Thunderbird (autoconfig) and Outlook (autodiscover) their settings.

    The SRV record is what Outlook looks up when the addresses don't answer it, which includes the step it takes
    first: a POST to the domain itself, where most domains have their website and no mail settings.
    """
    return [
        *(record for name in ("autoconfig", "autodiscover")
          for record in _host_records(f"{name}.{domain}", _reachable(server_ips))),
        DnsRecord("SRV", f"_autodiscover._tcp.{domain}", f"0 1 443 {AUTODISCOVER_PREFIX}{domain}."),
    ]


def mail_host(domain: str) -> str:
    """The name mail for the domain is delivered to, and mail programs connect to."""
    return f"mail.{domain}"


def webmail_host(domain: str) -> str:
    """The name of the domain's webmail site, which calendar and contact apps sync with too."""
    return f"{WEBMAIL_PREFIX}{domain}"


def dkim_record(domain: str, value: str) -> DnsRecord:
    return DnsRecord("TXT", dkim.record_name(domain), value)


@dataclass(frozen=True)
class _Domain:
    name: str
    server_ips: set[IPAddress]
    dkim_value: str | None
    resolver: Resolver


# What a check finds: a status, the explanation, and the records to publish if that would solve the problem.
_Finding = tuple[Status, str, tuple[DnsRecord, ...]]


def check_domain(domain: str, *, server_ips: set[IPAddress], dkim_value: str | None, resolver: Resolver) -> list[Check]:
    target = _Domain(domain, server_ips, dkim_value, resolver)
    checks: list[tuple[str, Callable[[_Domain], _Finding]]] = [
        ("MX", _check_mx), ("SPF", _check_spf), ("DKIM", _check_dkim), ("DMARC", _check_dmarc), ("SRV", _check_srv),
    ]
    return _run_checks(checks, target)


@dataclass(frozen=True)
class _Server:
    hostname: str
    ips: set[IPAddress]  # the addresses other servers reach this server on
    resolver: Resolver


def check_server(hostname: str, *, server_ips: set[IPAddress], resolver: Resolver) -> list[Check]:
    """Whether the name this server sends mail as and its addresses point to each other, as receiving servers check."""
    target = _Server(hostname.lower(), _reachable(server_ips), resolver)
    checks: list[tuple[str, Callable[[_Server], _Finding]]] = [("Hostname", _check_hostname), ("Reverse DNS", _check_ptr)]
    return _run_checks(checks, target)


def _run_checks[T](checks: list[tuple[str, Callable[[T], _Finding]]], target: T) -> list[Check]:
    results = []
    for name, check in checks:
        try:
            status, detail, fixes = check(target)
        except LookupFailed as error:
            status, detail, fixes = Status.WARN, str(error), ()
        results.append(Check(name, status, detail, fixes))
    return results


def _mx_record(domain: str) -> DnsRecord:
    # With the final dot: without it, DNS control panels like TransIP's add the domain again.
    return DnsRecord("MX", domain, f"10 {mail_host(domain)}.")


def _address_records(domain: str, server_ips: set[IPAddress]) -> tuple[DnsRecord, ...]:
    """The mail host's A and AAAA records, pointing to this server."""
    return _host_records(mail_host(domain), _reachable(server_ips))


def _host_records(host: str, ips: set[IPAddress]) -> tuple[DnsRecord, ...]:
    """The address records for a name on this server: its IPv4 address.

    A name is published so that mail programs and browsers reach it, and an address they can't reach costs every
    one of them a wait before it falls back to the other: a server whose web listeners are on IPv4 only, which is
    how OpenLiteSpeed comes, makes an AAAA record a delay on every first connection. A server with no IPv4 address
    publishes its IPv6 one, since then that is the only way to it.
    """
    published = {ip for ip in ips if ip.version == 4} or ips
    return tuple(DnsRecord("A" if ip.version == 4 else "AAAA", host, str(ip)) for ip in _sorted(published))


def _sorted(ips: set[IPAddress]) -> list[IPAddress]:
    return sorted(ips, key=lambda ip: (ip.version, ip))


def _reachable(server_ips: set[IPAddress]) -> set[IPAddress]:
    """The addresses other mail servers reach this server on: the public ones, or all of them when it has none."""
    return {ip for ip in server_ips if ip.is_global} or server_ips


def _spf_record(domain: str) -> DnsRecord:
    return DnsRecord("TXT", domain, "v=spf1 mx ~all")


def _dmarc_record(domain: str) -> DnsRecord:
    return DnsRecord("TXT", f"_dmarc.{domain}", "v=DMARC1; p=quarantine")


def _check_mx(domain: _Domain) -> _Finding:
    host = mail_host(domain.name)
    hosts = domain.resolver.mx(domain.name)
    to_this_server = [host for host in hosts if domain.resolver.addresses(host) & domain.server_ips]
    if host in (name.lower() for name in to_this_server):
        return Status.OK, f"Mail is delivered to {', '.join(hosts)}.", ()
    fixes = _address_records(domain.name, domain.server_ips)
    if host not in (name.lower() for name in hosts):
        fixes = (_mx_record(domain.name), *fixes)
    if not hosts:
        return Status.FAIL, "There is no MX record, so mail for the domain can't be delivered.", fixes
    if to_this_server:
        return Status.WARN, f"Mail is delivered to this server as {', '.join(to_this_server)}, not as {host}.", fixes
    return Status.FAIL, f"Mail is delivered to {', '.join(hosts)}, which isn't this server.", fixes


def _check_spf(domain: _Domain) -> _Finding:
    fixes = (_spf_record(domain.name),)
    records = _spf_records(domain.name, domain.resolver)
    if len(records) != 1:
        problem = "There is no SPF record" if not records else f"There are {len(records)} SPF records instead of one"
        return Status.FAIL, f"{problem}, so receiving servers can't verify mail from this server.", fixes
    ips = _reachable(domain.server_ips)
    try:
        refused = sorted((ip for ip in ips if not _SpfEvaluation(domain.resolver).allows(domain.name, ip)), key=str)
    except _Undecided as undecided:
        return undecided.status, str(undecided), ()
    if refused:
        return Status.FAIL, f"The SPF record doesn't allow this server's address {', '.join(map(str, refused))}.", fixes
    return Status.OK, "The SPF record allows this server to send mail for the domain.", ()


def _check_dkim(domain: _Domain) -> _Finding:
    if domain.dkim_value is None:
        detail = f"This server has no DKIM key for the domain. Create one with: mailctl dkim create {domain.name}"
        return Status.FAIL, detail, ()
    fix = dkim_record(domain.name, domain.dkim_value)
    published = [_public_key(record) for record in domain.resolver.txt(fix.name)]
    if _public_key(domain.dkim_value) in published:
        return Status.OK, "The published key matches this server's key.", ()
    if not any(published):
        return Status.FAIL, f"There is no DKIM record at {fix.name}.", (fix,)
    return Status.FAIL, f"The DKIM record at {fix.name} has another key than this server.", (fix,)


def _check_dmarc(domain: _Domain) -> _Finding:
    fixes = (_dmarc_record(domain.name),)
    # A subdomain without a DMARC record of its own follows the policy of its parent domain.
    labels = domain.name.split(".")
    for name in (".".join(labels[start:]) for start in range(len(labels) - 1)):
        records = [record for record in domain.resolver.txt(f"_dmarc.{name}") if is_dmarc(record)]
        if len(records) > 1:
            return Status.FAIL, f"There are {len(records)} DMARC records for {name}, so receiving servers ignore them.", fixes
        if records:
            source = "" if name == domain.name else f", set for {name}"
            return Status.OK, f"Policy: {_tag(records[0], 'p') or 'none'}{source}.", ()
    return Status.WARN, "There is no DMARC record. Mail still arrives, but some providers trust it less.", fixes


def _check_srv(domain: _Domain) -> _Finding:
    host = mail_host(domain.name)
    fixes, missing, problems = [], [], []
    for record, (_, _, port) in zip(srv_records(domain.name), MAIL_SERVICES):
        published = domain.resolver.srv(record.name)
        if not published:
            missing.append(record.name)
        elif not any(srv.target.lower() == host and srv.port == port for srv in published):
            targets = ", ".join(f"{srv.target} port {srv.port}" for srv in published)
            problems.append(f"{record.name} sends mail programs to {targets} instead of {host} port {port}.")
        else:
            continue
        fixes.append(record)
    if problems:
        return Status.FAIL, problems[0], tuple(fixes)
    if len(missing) == len(MAIL_SERVICES):
        return Status.WARN, "There are no SRV records, so mail programs can't look up the server settings.", tuple(fixes)
    if missing:
        return Status.WARN, f"Mail programs can't look up every server setting. Missing: {', '.join(missing)}.", tuple(fixes)
    return Status.OK, f"Mail programs can look up {host} for IMAP and sending.", ()


def _check_hostname(server: _Server) -> _Finding:
    missing = server.ips - server.resolver.addresses(server.hostname)
    if not missing:
        return Status.OK, f"{server.hostname} points to this server.", ()
    addresses = ", ".join(map(str, _sorted(missing)))
    detail = f"{server.hostname} doesn't point to this server's address {addresses}, so receiving servers may refuse its mail."
    return Status.FAIL, detail, _host_records(server.hostname, missing)


def _check_ptr(server: _Server) -> _Finding:
    wrong = []
    for ip in _sorted(server.ips):
        names = server.resolver.ptr(ip)
        if server.hostname not in (name.lower() for name in names):
            wrong.append(f"the reverse DNS of {ip} is {', '.join(names)}" if names else f"{ip} has no reverse DNS")
    if not wrong:
        return Status.OK, f"This server's addresses point back to {server.hostname}.", ()
    found = "; ".join(wrong)
    return Status.FAIL, (
        f"{found[0].upper()}{found[1:]}. Many receiving servers refuse mail from an address whose reverse DNS isn't "
        f"{server.hostname}. The provider of the server can set it."
    ), ()


def _spf_records(domain: str, resolver: Resolver) -> list[str]:
    return [record for record in resolver.txt(domain) if record.lower().split()[:1] == ["v=spf1"]]


def is_dmarc(record: str) -> bool:
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
