"""Let's Encrypt certificates: reading what one covers, and running certbot to get, renew and delete them."""

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import system
from .errors import CtlError

# Certbot renews 30 days before the end, so less than this means renewing fails.
EXPIRY_WARNING_DAYS = 14
_FILES = ("fullchain.pem", "privkey.pem")
# A certificate domainctl made itself, so a site can be served before its domain points here. The marker is
# what tells it apart from a real one, so certbot replaces it instead of refusing the directory.
_OURS = ".made-here"


@dataclass(frozen=True)
class Certificate:
    names: tuple[str, ...]
    expires: datetime


def live(letsencrypt_dir: Path, cert_name: str) -> Path:
    """Where certbot keeps the current certificate of that name."""
    return letsencrypt_dir / "live" / cert_name


def exists(letsencrypt_dir: Path, cert_name: str) -> bool:
    directory = live(letsencrypt_dir, cert_name)
    return all((directory / file).is_file() for file in _FILES)


def is_placeholder(letsencrypt_dir: Path, cert_name: str) -> bool:
    """Whether what is there is standing in for a real certificate: the empty files the mail role puts there so
    Dovecot starts, or one domainctl signed itself so a site could be served before its domain pointed here.
    Certbot's own are links, and neither of these is."""
    paths = [live(letsencrypt_dir, cert_name) / file for file in _FILES]
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        return False
    return all(path.stat().st_size == 0 for path in paths) or made_here(letsencrypt_dir, cert_name)


def made_here(letsencrypt_dir: Path, cert_name: str) -> bool:
    """Whether the certificate there is one we signed ourselves, waiting for the real one."""
    return (live(letsencrypt_dir, cert_name) / _OURS).is_file()


def sign_here(letsencrypt_dir: Path, cert_name: str, names: Sequence[str]) -> None:
    """Signs a certificate for those names here, so OpenLiteSpeed has files to load and the site is served
    while its domain still points somewhere else. A browser will not trust it; certbot replaces it with the
    real one as soon as the domain points here."""
    directory = live(letsencrypt_dir, cert_name)
    directory.mkdir(parents=True, exist_ok=True)
    alt = ",".join("DNS:%s" % name for name in names)
    system.run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-sha256",
               "-keyout", str(directory / "privkey.pem"), "-out", str(directory / "fullchain.pem"),
               "-subj", "/CN=%s" % names[0], "-addext", "subjectAltName=%s" % alt)
    (directory / "privkey.pem").chmod(0o600)
    (directory / _OURS).write_text("Signed by domainctl, waiting for the DNS. Certbot replaces it.\n")


@contextmanager
def placeholders_aside(letsencrypt_dir: Path, cert_name: str) -> Iterator[None]:
    """Takes the placeholder files away while certbot makes the first certificate of that name, since it refuses a
    live directory with anything in it. When it makes none, they are put back, so Dovecot still starts."""
    if not is_placeholder(letsencrypt_dir, cert_name):
        yield
        return
    ours = made_here(letsencrypt_dir, cert_name)
    paths = [live(letsencrypt_dir, cert_name) / file for file in _FILES]
    kept = [(path, path.read_bytes()) for path in paths] if ours else []
    for path in paths:
        path.unlink()
    if ours:
        (live(letsencrypt_dir, cert_name) / _OURS).unlink()
    try:
        yield
    finally:
        if not exists(letsencrypt_dir, cert_name):
            # Certbot made none, so what was standing in goes back: a site that was being served keeps being
            # served, and Dovecot still starts.
            for path, kept_bytes in kept:
                path.write_bytes(kept_bytes)
            if ours:
                (live(letsencrypt_dir, cert_name) / _OURS).write_text(
                    "Signed by domainctl, waiting for the DNS. Certbot replaces it.\n")
                (live(letsencrypt_dir, cert_name) / "privkey.pem").chmod(0o600)
            else:
                for path in paths:
                    path.touch()


def webroots(letsencrypt_dir: Path, cert_name: str) -> dict[str, Path]:
    """The web root certbot proves each name of the certificate under when it renews it, from its renewal file."""
    try:
        text = (letsencrypt_dir / "renewal" / f"{cert_name}.conf").read_text()
    except FileNotFoundError:
        return {}
    found: dict[str, Path] = {}
    in_map = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_map = line == "[[webroot_map]]"
        elif in_map and "=" in line:
            name, _, root = line.partition("=")
            found[name.strip()] = Path(root.strip())
    return found


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


def obtain_each(letsencrypt_dir: Path, email: str, cert_name: str, roots: Mapping[str, Path], key_type: str,
                dry_run: bool = False) -> None:
    """Gets a new certificate with these names, each proved with a file under a web root of its own, which certbot
    keeps for renewing it. A new one even when the names are the same, so certbot takes the web roots over."""
    names = list(roots)
    proofs = [argument for name, root in roots.items()
              for argument in ("--webroot-path", str(root), "--domains", name)]
    arguments = ["certonly", "--webroot", "--cert-name", cert_name, *proofs, "--key-type", key_type,
                 "--force-renewal"]
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
