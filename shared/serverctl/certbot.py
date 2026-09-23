"""Let's Encrypt certificates: reading what one covers, and running certbot to get, renew and delete them."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import system
from .errors import CtlError

# Certbot renews 30 days before the end, so less than this means renewing fails.
EXPIRY_WARNING_DAYS = 14


@dataclass(frozen=True)
class Certificate:
    names: tuple[str, ...]
    expires: datetime


def live(letsencrypt_dir: Path, cert_name: str) -> Path:
    """Where certbot keeps the current certificate of that name."""
    return letsencrypt_dir / "live" / cert_name


def exists(letsencrypt_dir: Path, cert_name: str) -> bool:
    directory = live(letsencrypt_dir, cert_name)
    return all((directory / file).is_file() for file in ("fullchain.pem", "privkey.pem"))


def read(path: Path) -> Certificate:
    try:
        empty = path.stat().st_size == 0
    except FileNotFoundError:
        raise CtlError(f"There is no certificate at {path}.") from None
    if empty:
        # Ansible creates empty files so a daemon can start before certbot has run.
        raise CtlError(f"{path} is empty: this server has no certificate yet.")
    return parse(system.run("openssl", "x509", "-in", str(path), "-noout", "-enddate", "-ext", "subjectAltName"))


def parse(output: str) -> Certificate:
    """Reads the output of 'openssl x509 -noout -enddate -ext subjectAltName'."""
    end = re.search(r"^notAfter=(.+)$", output, re.MULTILINE)
    try:
        expires = datetime.strptime(end.group(1).strip(), "%b %d %H:%M:%S %Y GMT").replace(tzinfo=UTC)
    except (AttributeError, ValueError):
        raise CtlError("openssl didn't show when the certificate expires.") from None
    return Certificate(tuple(name.lower() for name in re.findall(r"DNS:([^\s,]+)", output)), expires)


def covers(certificate: Certificate, host: str) -> bool:
    """Whether the certificate is valid for the host name, itself or through a wildcard like *.example.nl."""
    host = host.lower()
    parent = host.partition(".")[2]
    return any(name == host or (name.startswith("*.") and name[2:] == parent) for name in certificate.names)


def problem(path: Path, names: Iterable[str], now: datetime) -> str | None:
    """Why the certificate at the path can't be used for the names, or None when it's valid for all of them."""
    try:
        found = read(path)
    except CtlError as failure:
        return failure.message
    if found.expires <= now:
        return f"The certificate at {path} expired on {found.expires:%Y-%m-%d}."
    missing = [name for name in names if not covers(found, name)]
    if missing:
        return f"The certificate at {path} doesn't include {', '.join(missing)}."
    return None


def run(letsencrypt_dir: Path, email: str, *args: str) -> None:
    """certbot, with this server's Let's Encrypt account and the directory the certificates are looked for in."""
    account = ["--email", email] if email else ["--register-unsafely-without-email"]
    system.run("certbot", *args, "--agree-tos", *account, "--non-interactive", "--config-dir", str(letsencrypt_dir))


def obtain(letsencrypt_dir: Path, email: str, cert_name: str, webroot: Path, names: Sequence[str],
           dry_run: bool = False) -> None:
    """Gets or extends a certificate, proving each name with a file under the web root. Raises CtlError with what
    Let's Encrypt said when it refuses."""
    domains = [argument for name in names for argument in ("--domains", name)]
    # --cert-name fixes the directory under live/, so it stays the same when the first name changes.
    arguments = ["certonly", "--webroot", "--webroot-path", str(webroot), "--cert-name", cert_name, *domains,
                 "--keep-until-expiring", "--expand"]
    try:
        run(letsencrypt_dir, email, *arguments, *(["--dry-run"] if dry_run else []))
    except CtlError as failure:
        attempt = "The certificate test run" if dry_run else "No certificate"
        raise CtlError(f"{attempt} for {', '.join(names)}: {reason(failure.message)}") from None


def delete(letsencrypt_dir: Path, cert_name: str) -> bool:
    """Deletes the certificate and its renewal settings. Returns whether there was one."""
    if not exists(letsencrypt_dir, cert_name):
        return False
    run(letsencrypt_dir, "", "delete", "--cert-name", cert_name)
    return True


def reason(message: str) -> str:
    """What Let's Encrypt reported (certbot's "Detail:" lines), or else all of certbot's error."""
    details = [line.partition("Detail:")[2].strip() for line in message.splitlines() if "Detail:" in line]
    return " ".join(details) or message
