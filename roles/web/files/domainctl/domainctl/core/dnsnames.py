"""Whether a site's names point to this server, and publishing the ones that are missing at TransIP.

Let's Encrypt looks at real DNS, not at a zone in a control panel, so real DNS decides here: a name is ready
when every address it resolves to is this server's. TransIP only comes in to fix a name that has no record at
all, and only when the domain is in the account. A name pointing somewhere else is never changed: that is
someone else's site, or a migration that isn't finished.
"""

import time
from dataclasses import dataclass
from enum import Enum

from serverctl import transip
from serverctl.dns import IPAddress, LookupFailed, Resolver
from serverctl.errors import CtlError
from serverctl.transip import Entry

EXPIRE = 3600  # seconds, for a record published here
PUBLISH_WAIT = 300  # how long to wait for TransIP's nameservers to serve a new record
PUBLISH_POLL = 10  # seconds between asking them


class State(Enum):
    HERE = "here"  # every address it resolves to is this server's
    MISSING = "missing"  # no A or AAAA record at all
    ELSEWHERE = "elsewhere"  # it resolves, but not only to this server
    UNKNOWN = "unknown"  # the lookup itself failed


@dataclass(frozen=True)
class NameCheck:
    name: str
    state: State
    detail: str = ""
    addresses: frozenset = frozenset()

    @property
    def ready(self) -> bool:
        return self.state is State.HERE


def check(resolver: Resolver, name: str, server_ips: set[IPAddress]) -> NameCheck:
    """Where the name points, from this server's point of view.

    Every address it resolves to has to be this server's: Let's Encrypt and visitors may use any of them, so one
    foreign address is enough to make a certificate request fail.
    """
    try:
        addresses = resolver.addresses(name)
    except LookupFailed as failure:
        return NameCheck(name, State.UNKNOWN, str(failure))
    if not addresses:
        return NameCheck(name, State.MISSING, f"{name} has no A or AAAA record.")
    foreign = sorted(addresses - server_ips, key=lambda ip: (ip.version, ip))
    if foreign:
        also = " also" if len(foreign) < len(addresses) else ""
        return NameCheck(name, State.ELSEWHERE,
                         f"{name}{also} points to {', '.join(map(str, foreign))}, which isn't this server.")
    return NameCheck(name, State.HERE, "", frozenset(addresses))


def publishable_ips(server_ips: set[IPAddress]) -> set[IPAddress]:
    """The addresses worth publishing: the ones reachable from the internet."""
    return {ip for ip in server_ips if ip.is_global}


def find_zone(client: transip.Client, name: str) -> tuple[str, list[Entry]]:
    """The zone at TransIP holding the name's records: its own domain, or the nearest parent in the account."""
    labels = name.split(".")
    for start in range(len(labels) - 1):
        zone = ".".join(labels[start:])
        try:
            return zone, client.dns_entries(zone)
        except transip.NotInAccount:
            continue
    raise transip.NotInAccount(f"{name} isn't in the TransIP account {client.login}.",
                               hint="Publish its A record where its DNS is managed, then run the command again.")


def relative(zone: str, name: str) -> str:
    """The name as TransIP writes it inside the zone: "@" for the zone itself."""
    return "@" if name.lower() == zone.lower() else name.lower().removesuffix(f".{zone.lower()}")


def publish(client: transip.Client, name: str, ips: set[IPAddress]) -> tuple[str, tuple[Entry, ...]]:
    """Adds A and AAAA records for the name, pointing here. Returns the zone and the entries added.

    Only a name with no address record at all is published: replacing one that points elsewhere would take
    someone else's site off the internet. The whole zone is written back in one request, so a change made in the
    control panel meanwhile is noticed rather than overwritten.
    """
    zone, entries = find_zone(client, name)
    if not transip.uses_transip_nameservers(client.nameservers(zone)):
        raise CtlError(f"{zone} doesn't use TransIP's nameservers, so publishing a record there changes nothing "
                       f"the internet can see.",
                       hint=f"Publish the A record for {name} where its DNS is managed.")
    inside = relative(zone, name)
    if any(entry.name.lower() == inside and entry.type in ("A", "AAAA", "CNAME") for entry in entries):
        raise CtlError(f"{name} already has a record at TransIP that doesn't point to this server.",
                       hint="Change it in TransIP's control panel, then run the command again.")
    added = tuple(Entry(inside, EXPIRE, "A" if ip.version == 4 else "AAAA", str(ip))
                  for ip in sorted(ips, key=lambda ip: (ip.version, ip)))
    if client.dns_entries(zone) != entries:
        raise CtlError(f"The DNS records of {zone} at TransIP changed in the meantime. Nothing was changed.",
                       hint="Run the command again to see what it does now.")
    client.replace_dns_entries(zone, (*entries, *added))
    return zone, added


def wait_until_resolving(resolver: Resolver, name: str, server_ips: set[IPAddress],
                         wait: int = PUBLISH_WAIT, poll: int = PUBLISH_POLL) -> NameCheck:
    """Asks until the name resolves to this server, or the wait runs out. A published record takes a moment to
    reach the nameservers, and certbot must not ask before it has."""
    deadline = time.monotonic() + wait
    while True:
        found = check(resolver, name, server_ips)
        if found.ready or time.monotonic() >= deadline:
            return found
        time.sleep(poll)
