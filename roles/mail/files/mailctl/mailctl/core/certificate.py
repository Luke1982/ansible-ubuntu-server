"""The certificate Postfix and Dovecot present: one for the hostname and every mail.DOMAIN."""

import http.client
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import files, system
from .config import Config
from .dns_check import Check, IPAddress, Status, mail_host
from .errors import MailctlError

# Certbot renews 30 days before the end, so less than this means renewing fails.
EXPIRY_WARNING_DAYS = 14
# Where Let's Encrypt asks for its challenge, under a site's web root.
CHALLENGES = ".well-known/acme-challenge"
# What an address answers when asked for a challenge.
SERVED = "served"  # the challenge folder is served for the name at that address
NO_ANSWER = "no answer"  # nothing accepted the connection: Let's Encrypt falls back to the other family
ANOTHER_SITE = "another site"  # something answered, but not this site's challenge


@dataclass(frozen=True)
class Certificate:
    names: tuple[str, ...]
    expires: datetime


def read(path: Path) -> Certificate:
    try:
        empty = path.stat().st_size == 0
    except FileNotFoundError:
        raise MailctlError(f"There is no certificate at {path}.") from None
    if empty:
        # Ansible creates empty files so Dovecot can start before certbot has run.
        raise MailctlError(f"{path} is empty: this server has no certificate yet.")
    return parse(system.run("openssl", "x509", "-in", str(path), "-noout", "-enddate", "-ext", "subjectAltName"))


def parse(output: str) -> Certificate:
    """Reads the output of 'openssl x509 -noout -enddate -ext subjectAltName'."""
    end = re.search(r"^notAfter=(.+)$", output, re.MULTILINE)
    try:
        expires = datetime.strptime(end.group(1).strip(), "%b %d %H:%M:%S %Y GMT").replace(tzinfo=UTC)
    except (AttributeError, ValueError):
        raise MailctlError("openssl didn't show when the certificate expires.") from None
    return Certificate(tuple(name.lower() for name in re.findall(r"DNS:([^\s,]+)", output)), expires)


def run_certbot(config: Config, *args: str) -> None:
    """certbot, with this server's Let's Encrypt account and the directory the certificates are looked for in."""
    email = config.letsencrypt_email
    account = ["--email", email] if email else ["--register-unsafely-without-email"]
    system.run("certbot", *args, "--agree-tos", *account, "--non-interactive",
               "--config-dir", str(config.letsencrypt_dir))


def wait_until_served(root: Path, name: str, addresses: set[IPAddress], hint: str, timeout: float) -> list[str]:
    """Waits until OpenLiteSpeed serves a test file from the challenge folder under the name. Failed validations
    count against Let's Encrypt's limits, so this is checked first.

    Let's Encrypt asks at one address and falls back to the other family when nothing answers there at all, so an
    address that refuses the connection is reported, not an error; a returned line says so. An address that
    answers with something else is another site, and Let's Encrypt fails on that instead of falling back.
    """
    token = secrets.token_hex(16)
    probe = root / CHALLENGES / f"mailctl-{token}"
    files.replace(probe, token)
    try:
        deadline = time.monotonic() + timeout
        answers = {}
        for address in sorted(addresses, key=lambda ip: (ip.version, ip)):
            answer = serves(address, name, probe.name, token)
            while answer != SERVED and time.monotonic() < deadline:
                time.sleep(0.5)
                answer = serves(address, name, probe.name, token)
            answers[address] = answer
    finally:
        files.remove(probe)
    elsewhere = [address for address, answer in answers.items() if answer == ANOTHER_SITE]
    if elsewhere:
        raise MailctlError(f"Another site answers for {name} on port 80 at {', '.join(map(str, elsewhere))}, so "
                           f"Let's Encrypt is given that one instead of the challenge.", hint=hint)
    if not any(answer == SERVED for answer in answers.values()):
        raise MailctlError(f"OpenLiteSpeed doesn't serve {name} on port 80 at "
                           f"{', '.join(str(address) for address in answers)}.", hint=hint)
    return [f"Nothing answers for {name} on port 80 at {address}, so Let's Encrypt uses this server's other "
            f"address. Add a listener for it in WebAdmin, or take the record away."
            for address, answer in answers.items() if answer == NO_ANSWER]


def serves(address: IPAddress, name: str, file_name: str, token: str) -> str:
    connection = http.client.HTTPConnection(str(address), 80, timeout=2)
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


def reason(message: str) -> str:
    """What Let's Encrypt reported (certbot's "Detail:" lines), or else all of certbot's error."""
    details = [line.partition("Detail:")[2].strip() for line in message.splitlines() if "Detail:" in line]
    return " ".join(details) or message


def covers(certificate: Certificate, host: str) -> bool:
    """Whether the certificate is valid for the host name, itself or through a wildcard like *.example.nl."""
    host = host.lower()
    parent = host.partition(".")[2]
    return any(name == host or (name.startswith("*.") and name[2:] == parent) for name in certificate.names)


def problem(path: Path, names: Iterable[str], now: datetime) -> str | None:
    """Why the certificate at the path can't be used for the names, or None when it's valid for all of them."""
    try:
        found = read(path)
    except MailctlError as failure:
        return failure.message
    if found.expires <= now:
        return f"The certificate at {path} expired on {found.expires:%Y-%m-%d}."
    missing = [name for name in names if not covers(found, name)]
    if missing:
        return f"The certificate at {path} doesn't include {', '.join(missing)}."
    return None


def check_server(certificate: Certificate, hostname: str, now: datetime) -> Check:
    date = f"{certificate.expires:%Y-%m-%d}"
    if certificate.expires <= now:
        return Check("Certificate", Status.FAIL, f"The certificate expired on {date}, so mail programs refuse to connect.")
    if not covers(certificate, hostname):
        return Check("Certificate", Status.FAIL, f"The certificate doesn't include {hostname}.")
    days = (certificate.expires - now).days
    if days < EXPIRY_WARNING_DAYS:
        detail = f"The certificate expires on {date}, in {days} days, so renewing it seems to fail. Try: certbot renew"
        return Check("Certificate", Status.WARN, detail)
    return Check("Certificate", Status.OK, f"Valid until {date}.")


def check_domain(certificate: Certificate, domain: str) -> Check:
    host = mail_host(domain)
    if covers(certificate, host):
        return Check("Certificate", Status.OK, f"The certificate includes {host}.")
    return Check("Certificate", Status.FAIL, (
        f"The certificate doesn't include {host}, so mail programs connecting to it get a certificate warning. "
        f"Add it once {host} points to this server."
    ))
