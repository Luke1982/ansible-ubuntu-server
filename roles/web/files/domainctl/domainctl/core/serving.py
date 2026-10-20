"""Checking that OpenLiteSpeed really serves a site's challenge folder before certbot asks Let's Encrypt to.

A new member points at a certificate that doesn't exist yet, so OpenLiteSpeed may log an error for it. Whether it
then still serves the site over plain HTTP is what decides if certbot can succeed, and it is cheaper to find out
with one request of our own than with a failed validation: those count against Let's Encrypt's limits.

The check asks each of the server's own addresses, with the site's name in the Host header, because Let's
Encrypt may use any address the name resolves to. An address where nothing answers at all is no reason to stop:
Let's Encrypt then tries the name's other address. That is what an IPv6 address does while OpenLiteSpeed listens
on IPv4 only, as its listeners on *:80 do.
"""

import http.client
import secrets
import time
from pathlib import Path

from serverctl import files
from serverctl.dns import IPAddress
from serverctl.errors import CtlError

CHALLENGES = ".well-known/acme-challenge"
TIMEOUT = 15  # seconds OpenLiteSpeed gets to serve a new site after a restart
POLL = 0.5  # seconds between tries
_REQUEST_TIMEOUT = 2  # seconds
# What an address answers when asked for the challenge.
SERVED = "served"  # the site's challenge folder, under the name
NO_ANSWER = "no answer"  # nothing accepted the connection: Let's Encrypt tries the name's other address
ANOTHER_SITE = "another site"  # something answered, but not with the challenge: Let's Encrypt fails on that


def challenge_dir(docroot: Path) -> Path:
    return docroot / CHALLENGES


def wait_until_served(docroot: Path, name: str, addresses: set[IPAddress], timeout: float = TIMEOUT) -> list[str]:
    """Waits until a file placed in the challenge folder comes back over HTTP under the name, at each address.

    Returns a line about each address where nothing answers, since Let's Encrypt uses another one then. Raises
    CtlError, with what to look at, when an address answers with something else, or none serves the site. The file
    is removed again either way.
    """
    token = secrets.token_hex(16)
    probe = challenge_dir(docroot) / f"domainctl-{token}"
    files.replace(probe, token)
    answers = {}
    try:
        deadline = time.monotonic() + timeout
        for address in sorted(addresses, key=lambda ip: (ip.version, ip)):
            answer = serves(address, name, probe.name, token)
            # Once one address serves the site, OpenLiteSpeed is up, and an address it doesn't listen on stays silent.
            while answer != SERVED and time.monotonic() < deadline and \
                    not (answer == NO_ANSWER and SERVED in answers.values()):
                time.sleep(POLL)
                answer = serves(address, name, probe.name, token)
            answers[address] = answer
    finally:
        files.remove(probe)
    elsewhere = [address for address, answer in answers.items() if answer == ANOTHER_SITE]
    if elsewhere:
        raise CtlError(f"Something else answers for {name} on port 80 at {_listed(elsewhere)}, so Let's Encrypt "
                       f"would be given that instead of the challenge.",
                       hint=f"Look in WebAdmin for another site or listener that answers for {name}.")
    if answers and SERVED not in answers.values():
        raise CtlError(f"OpenLiteSpeed doesn't serve {name} on port 80 at {_listed(answers)}, so Let's Encrypt "
                       f"can't check it either.",
                       hint=f"Look in {docroot.parent.parent / 'logs' / 'error.log'} and OpenLiteSpeed's own "
                            f"error log for why the site didn't start.")
    return [f"Nothing answers on port 80 at {address}, where {name} points too, because OpenLiteSpeed doesn't "
            f"listen there. Let's Encrypt uses the other address."
            for address, answer in answers.items() if answer == NO_ANSWER]


def _listed(addresses) -> str:
    return ", ".join(str(address) for address in addresses)


def serves(address: IPAddress, name: str, file_name: str, token: str) -> str:
    """What the server at the address answers when asked for the file under the name: SERVED when it gives back
    exactly the token."""
    host = f"[{address}]" if address.version == 6 else str(address)
    connection = http.client.HTTPConnection(host, 80, timeout=_REQUEST_TIMEOUT)
    try:
        connection.request("GET", f"/{CHALLENGES}/{file_name}", headers={"Host": name})
        response = connection.getresponse()
        if response.status == 200 and response.read(len(token) + 2).decode(errors="replace").strip() == token:
            return SERVED
        return ANOTHER_SITE
    except OSError:
        return NO_ANSWER
    finally:
        connection.close()
