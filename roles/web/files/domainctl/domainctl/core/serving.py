"""Checking that OpenLiteSpeed really serves a site's challenge folder before certbot asks Let's Encrypt to.

A new member points at a certificate that doesn't exist yet, so OpenLiteSpeed may log an error for it. Whether it
then still serves the site over plain HTTP is what decides if certbot can succeed, and it is cheaper to find out
with one request of our own than with a failed validation: those count against Let's Encrypt's limits.

The check asks each of the server's own addresses, with the site's name in the Host header, because Let's
Encrypt may use any address the name resolves to.
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


def challenge_dir(docroot: Path) -> Path:
    return docroot / CHALLENGES


def wait_until_served(docroot: Path, name: str, addresses: set[IPAddress], timeout: float = TIMEOUT) -> None:
    """Waits until a file placed in the challenge folder comes back over HTTP at each address, under the name.

    Raises CtlError when it doesn't, with what to look at. The file is removed again either way.
    """
    token = secrets.token_hex(16)
    probe = challenge_dir(docroot) / f"domainctl-{token}"
    files.replace(probe, token)
    try:
        deadline = time.monotonic() + timeout
        for address in sorted(addresses, key=lambda ip: (ip.version, ip)):
            while not serves(address, name, probe.name, token):
                if time.monotonic() >= deadline:
                    raise CtlError(
                        f"OpenLiteSpeed doesn't serve {name} on port 80 at {address}, so Let's Encrypt can't "
                        f"check it either.",
                        hint=f"Look in {docroot.parent.parent / 'logs' / 'error.log'} and OpenLiteSpeed's own "
                             f"error log for why the site didn't start.",
                    )
                time.sleep(POLL)
    finally:
        files.remove(probe)


def serves(address: IPAddress, name: str, file_name: str, token: str) -> bool:
    """Whether the server at the address gives back exactly the token for that name and file."""
    host = f"[{address}]" if address.version == 6 else str(address)
    connection = http.client.HTTPConnection(host, 80, timeout=_REQUEST_TIMEOUT)
    try:
        connection.request("GET", f"/{CHALLENGES}/{file_name}", headers={"Host": name})
        response = connection.getresponse()
        return response.status == 200 and response.read(len(token) + 2).decode(errors="replace").strip() == token
    except OSError:
        return False
    finally:
        connection.close()
