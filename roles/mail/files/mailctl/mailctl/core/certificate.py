"""The certificate Postfix and Dovecot present: one for the hostname and every mail.DOMAIN."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import system
from .dns_check import Check, Status, mail_host
from .errors import MailctlError

# Certbot renews 30 days before the end, so less than this means renewing fails.
EXPIRY_WARNING_DAYS = 14


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


def covers(certificate: Certificate, host: str) -> bool:
    """Whether the certificate is valid for the host name, itself or through a wildcard like *.example.nl."""
    host = host.lower()
    parent = host.partition(".")[2]
    return any(name == host or (name.startswith("*.") and name[2:] == parent) for name in certificate.names)


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
