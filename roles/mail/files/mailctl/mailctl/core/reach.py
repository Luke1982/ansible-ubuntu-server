"""Whether the addresses a name points to answer on the port that name is for.

DNS says where a name points, not whether anything is there. A record for an address where nothing listens costs
every mail program and browser the wait before it falls back to the other one, and Outlook, which asks the domain
itself first, gives up on the settings altogether. Both are invisible in the records themselves.
"""

import socket
from collections.abc import Callable
from typing import TypeAlias

from .dns_check import Check, IPAddress, LookupFailed, Resolver, Status

TIMEOUT = 2.0  # seconds; this is a check, not a wait for a server that is busy
IMAP_PORT = 993
HTTPS_PORT = 443
SMTP_PORT = 25

Probe: TypeAlias = Callable[[IPAddress, int], bool]


def answers(address: IPAddress, port: int, timeout: float = TIMEOUT) -> bool:
    """Whether something accepts a connection there. Nothing is sent, and nothing is read."""
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            probe.connect((str(address), port))
        except OSError:
            return False
    return True


def check(names: dict[str, int], resolver: Resolver, probe: Probe | None = None,
          advice: dict[str, str] | None = None) -> Check | None:
    """Each name's addresses on the port it is for. None when there is nothing to look at.

    The probe is looked up when this runs, not when it is defined, so the tests reach nothing on the network.
    """
    probe, advice = probe or answers, advice or {}
    silent, looked_at, how = [], 0, []
    for name, port in names.items():
        try:
            addresses = resolver.addresses(name)
        except LookupFailed:
            continue  # the records themselves are another check's to report
        for address in sorted(addresses, key=lambda ip: (ip.version, ip)):
            looked_at += 1
            if not probe(address, port):
                silent.append(f"{name} at {address} doesn't answer on port {port}")
                if name in advice and advice[name] not in how:
                    how.append(advice[name])
    if not looked_at:
        return None
    if not silent:
        return Check("Reachable", Status.OK, "Everything these names point to answers.")
    return Check("Reachable", Status.WARN, (
        f"{'; '.join(silent)}. Mail programs and browsers wait for that before trying this server's other address, "
        f"and Outlook gives up on a name that doesn't answer. {' '.join(how) if how else 'Take the record away, or '
        'let the server answer there.'}"
    ))
