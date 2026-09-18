"""The changes that put a domain's mail records into its DNS zone at TransIP.

Entries use TransIP's notation: names relative to the zone, "@" for the zone's domain itself, and host names in
content either relative or absolute with a final dot. Records mailctl doesn't manage are left as they are.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from .dns_check import DnsRecord, IPAddress, is_dmarc, mail_host

EXPIRE = 3600  # seconds, for new entries
# Where Thunderbird and Outlook look for mail settings before the SRV records. Records a previous mail provider left
# there would send mail programs to it, so they're removed, unless the domain's own site is published there.
STALE_AUTODETECT = (("autoconfig", ("A", "AAAA", "CNAME")), ("autodiscover", ("A", "AAAA", "CNAME")),
                    ("_autodiscover._tcp", ("SRV", "CNAME")))


@dataclass(frozen=True)
class Entry:
    name: str
    expire: int
    type: str
    content: str


@dataclass(frozen=True)
class Plan:
    remove: tuple[Entry, ...]
    add: tuple[Entry, ...]
    unchanged: int  # wanted records that are already there
    result: tuple[Entry, ...]  # the whole zone after the changes

    @property
    def changes(self) -> bool:
        return bool(self.remove or self.add)


@dataclass(frozen=True)
class _Group:
    """Records that together replace some of the entries at their name."""
    replaces: Callable[[Entry], bool]
    # The entries to have, given the ones the group replaces.
    wanted: Callable[[list[Entry]], list[Entry]]


def plan(domain: str, current: list[Entry], records: list[DnsRecord], zone: str | None = None) -> Plan:
    """The entries to remove and add, so the zone holds the domain's records and not the entries they replace.
    The zone is the domain's own, or a parent domain's for a domain like shop.example.nl."""
    zone = zone or domain
    remove: list[Entry] = []
    add: list[Entry] = []
    unchanged = 0
    for group in _groups(domain, zone, records):
        existing = [entry for entry in current if group.replaces(entry)]
        wanted = group.wanted(existing)
        kept = [entry for entry in existing if any(_same(entry, other, zone) for other in wanted)]
        new = [entry for entry in wanted if not any(_same(entry, other, zone) for other in kept)]
        remove += [entry for entry in existing if entry not in kept]
        add += new
        unchanged += len(wanted) - len(new)
    result = tuple(entry for entry in current if entry not in remove) + tuple(add)
    return Plan(tuple(remove), tuple(add), unchanged, result)


def absolute(zone: str, name: str) -> str:
    """A name in the zone written out in full, like mail.example.nl for "mail"."""
    return zone if name == "@" else f"{name}.{zone}"


def _relative(zone: str, name: str) -> str:
    return "@" if name == zone else name.removesuffix(f".{zone}")


def _groups(domain: str, zone: str, records: list[DnsRecord]) -> list[_Group]:
    mail_ips = {ip_address(record.value) for record in records
                if record.type in ("A", "AAAA") and record.name == mail_host(domain)}
    grouped: dict[tuple[str, str], list[Entry]] = {}
    for record in records:
        entry = _entry(zone, record)
        grouped.setdefault((entry.name.lower(), _kind(entry)), []).append(entry)
    groups = [_group(name, kind, entries, mail_ips) for (name, kind), entries in grouped.items()]
    published = {name for name, _ in grouped}
    stale = ((_relative(zone, f"{name}.{domain}").lower(), types) for name, types in STALE_AUTODETECT)
    return groups + [_removed(name, types) for name, types in stale if name not in published]


def _kind(entry: Entry) -> str:
    if entry.type in ("A", "AAAA"):
        return "address"
    if entry.type == "TXT" and _is_spf(entry.content):
        return "spf"
    if entry.type == "TXT" and _is_dmarc(entry.content):
        return "dmarc"
    return entry.type


def _group(name: str, kind: str, records: list[Entry], mail_ips: set[IPAddress]) -> _Group:
    def at_name(*types: str, only: Callable[[Entry], bool] = lambda entry: True) -> Callable[[Entry], bool]:
        return lambda entry: entry.name.lower() == name and entry.type in types and only(entry)

    def own(existing: list[Entry]) -> list[Entry]:
        return records

    if kind == "address":
        return _Group(at_name("A", "AAAA", "CNAME"), own)
    if kind == "spf":
        # Other TXT records, like site verifications, stay.
        return _Group(at_name("TXT", only=lambda entry: _is_spf(entry.content)),
                      lambda existing: _spf(existing, records, mail_ips))
    if kind == "dmarc":
        # The owner's policy stays. Several records are invalid, so they're replaced.
        return _Group(at_name("TXT", "CNAME", only=lambda entry: entry.type == "CNAME" or _is_dmarc(entry.content)),
                      lambda existing: existing if len(existing) == 1 else records)
    if kind == "MX":
        return _Group(at_name("MX"), own)
    return _Group(at_name(kind, "CNAME"), own)


def _removed(name: str, types: tuple[str, ...]) -> _Group:
    """Entries that go without anything in their place."""
    return _Group(lambda entry: entry.name.lower() == name and entry.type in types, lambda existing: [])


def _spf(existing: list[Entry], records: list[Entry], mail_ips: set[IPAddress]) -> list[Entry]:
    """The SPF record to have. One that doesn't allow the mail host yet gets its missing addresses, so the senders it
    already allows can still send. Addresses take no DNS lookups, so the record stays within its limit of 10."""
    if len(existing) != 1:
        return records
    entry = existing[0]
    version, *terms = _text(entry.content).split()
    missing = _not_allowed(terms, mail_ips)
    if not missing:
        return existing
    added = [f"ip{ip.version}:{ip}" for ip in sorted(missing, key=lambda ip: (ip.version, ip))]
    return [Entry(entry.name, EXPIRE, "TXT", " ".join([version, *added, *terms]))]


def _not_allowed(terms: list[str], mail_ips: set[IPAddress]) -> set[IPAddress]:
    """The mail host's addresses the SPF terms don't allow before 'all' decides. 'mx' allows all of them, since the
    MX record points to the mail host."""
    missing = set(mail_ips)
    for term in (term.lower() for term in terms):
        qualifier, mechanism = (term[0], term[1:]) if term[0] in "+-~?" else ("+", term)
        if mechanism == "all":
            break
        if qualifier != "+":
            continue
        if mechanism == "mx":
            return set()
        kind, _, network = mechanism.partition(":")
        if kind in ("ip4", "ip6"):
            try:
                missing = {ip for ip in missing if ip not in ip_network(network, strict=False)}
            except ValueError:
                pass
    return missing


def _entry(zone: str, record: DnsRecord) -> Entry:
    # Host names in content are written in full, with the final dot.
    content = f"{record.value}." if record.type in ("MX", "SRV", "CNAME") else record.value
    return Entry(_relative(zone, record.name), EXPIRE, record.type, content)


def _same(entry: Entry, other: Entry, zone: str) -> bool:
    """Whether two entries at the same name hold the same record, however it's written."""
    return entry.type == other.type and _meaning(entry, zone) == _meaning(other, zone)


def _meaning(entry: Entry, zone: str):
    content = entry.content.strip()
    if entry.type in ("A", "AAAA"):
        try:
            return ip_address(content)
        except ValueError:
            return content.lower()
    if entry.type in ("MX", "SRV", "CNAME"):
        *numbers, host = content.lower().split() or [""]
        return (*numbers, host.removesuffix(".") if host.endswith(".") else absolute(zone, host))
    if entry.type == "TXT":
        text = _text(content)
        if entry.name.lower().endswith("._domainkey"):
            # A DKIM record is the same with other tags, or with spaces in the key.
            key = re.search(r"(?:^|;)\s*p=([^;]*)", text)
            return "".join(key.group(1).split()) if key else text
        return " ".join(text.split())
    return content


def _text(content: str) -> str:
    """A TXT value without its quotes; a value split into several quoted strings is joined."""
    if re.fullmatch(r'\s*("(?:[^"\\]|\\.)*"\s*)+', content):
        strings = re.findall(r'"((?:[^"\\]|\\.)*)"', content)
        return "".join(re.sub(r"\\(.)", r"\1", string) for string in strings).strip()
    return content.strip()


def _is_spf(content: str) -> bool:
    return _text(content).lower().split()[:1] == ["v=spf1"]


def _is_dmarc(content: str) -> bool:
    return is_dmarc(_text(content))
