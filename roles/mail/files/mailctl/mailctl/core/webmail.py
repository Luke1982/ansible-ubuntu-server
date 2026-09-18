"""Webmail: a site at webmail.DOMAIN in front of SOGo, for every mail domain whose webmail name points to this server.

A site is an OpenLiteSpeed virtual host with a Let's Encrypt certificate of its own, which certbot gets through the
site itself. Until the certificate exists, the site only answers Let's Encrypt's challenges, so SOGo is never
offered without encryption. The generated files in the webmail directory are the only record of which sites exist.
"""

import http.client
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import files, system
from .config import Config
from .dns_check import IPAddress, LookupFailed, Resolver
from .errors import MailctlError

PREFIX = "webmail."
SERVE_TIMEOUT = 10  # seconds OpenLiteSpeed gets to serve a new site after a restart
_HEADER = "# Written by mailctl webmail sync. Changes are overwritten.\n"
_CHALLENGES = ".well-known/acme-challenge"


class State(Enum):
    LIVE = "live"
    NEW = "new"  # live, with a certificate obtained just now
    WAITING = "waiting"  # the webmail name doesn't point to this server (yet)
    FAILED = "failed"  # no certificate
    REMOVED = "removed"
    UNCHECKED = "unchecked"  # the DNS lookup failed, so the site was left as it was


@dataclass(frozen=True)
class Outcome:
    host: str
    state: State
    detail: str = ""


@dataclass(frozen=True)
class Result:
    outcomes: list[Outcome]
    changed: bool


class NotPointingHere(Exception):
    """The name has no address, or an address that isn't this server's."""


def host(domain: str) -> str:
    return PREFIX + domain


def sites(config: Config) -> list[str]:
    return sorted(path.stem for path in _directory(config).glob(f"{PREFIX}*.conf"))


def has_certificate(config: Config, name: str) -> bool:
    return all((_live(config, name) / file).is_file() for file in ("fullchain.pem", "privkey.pem"))


def points_here(resolver: Resolver, name: str, server_ips: set[IPAddress]) -> set[IPAddress]:
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


def sync(config: Config, domains: Iterable[str], server_ips: set[IPAddress], resolver: Resolver) -> Result:
    """Gives every domain whose webmail name points here a site with a certificate, and removes the other sites."""
    wanted = {host(domain) for domain in domains}
    existing = set(sites(config))
    outcomes: dict[str, Outcome] = {}
    kept: set[str] = set()
    uncertified: dict[str, set[IPAddress]] = {}  # the sites to get a certificate for, with their addresses
    for name in sorted(wanted | existing):
        if name not in wanted:
            outcomes[name] = Outcome(name, State.REMOVED, "Its domain is no longer on this server.")
            continue
        try:
            addresses = points_here(resolver, name, server_ips)
        except NotPointingHere as problem:
            state = State.REMOVED if name in existing else State.WAITING
            outcomes[name] = Outcome(name, state, str(problem))
            continue
        except LookupFailed as problem:
            outcomes[name] = Outcome(name, State.UNCHECKED, str(problem))
            if name in existing:
                kept.add(name)
            continue
        kept.add(name)
        outcomes[name] = Outcome(name, State.LIVE)
        if not has_certificate(config, name):
            uncertified[name] = addresses

    changed = _publish(config, kept)
    for name in sorted(existing - kept):
        changed |= _delete_certificate(config, name)
    for name, addresses in uncertified.items():
        try:
            _wait_until_served(config, name, addresses)
            _request_certificate(config, name)
        except MailctlError as problem:
            outcomes[name] = Outcome(name, State.FAILED, " ".join(filter(None, (problem.message, problem.hint))))
        else:
            outcomes[name] = Outcome(name, State.NEW)
            changed = True
    _publish(config, kept)
    return Result([outcomes[name] for name in sorted(outcomes)], changed)


def remove(config: Config, domain: str) -> bool:
    """Removes the domain's site and certificate. Returns whether it had a site."""
    name = host(domain)
    if name not in sites(config):
        return False
    _publish(config, set(sites(config)) - {name})
    _delete_certificate(config, name)
    return True


def _publish(config: Config, names: set[str]) -> bool:
    """Writes the sites, and has OpenLiteSpeed read them if anything changed. Returns whether it did. A site is on
    the HTTPS listeners once its certificate exists."""
    names = sorted(names)
    certified = [name for name in names if has_certificate(config, name)]
    directory = _directory(config)
    changes = [files.replace(directory / f"{name}.conf", _site(config, name, name in certified)) for name in names]
    changes += [files.remove(directory / f"{name}.conf") for name in sites(config) if name not in names]
    changes += [
        files.replace(directory / "vhosts.conf", _HEADER + "".join(_virtual_host(config, name) for name in names)),
        files.replace(directory / "http-maps.conf", _HEADER + "".join(_map(name) for name in names)),
        files.replace(directory / "https-maps.conf", _HEADER + "".join(_map(name) for name in certified)),
    ]
    if not any(changes):
        return False
    system.run(str(config.ols_root / "bin" / "lswsctrl"), "restart")
    return True


def _wait_until_served(config: Config, name: str, addresses: set[IPAddress]) -> None:
    """Waits until OpenLiteSpeed serves a test file from the challenge folder under the name, as Let's Encrypt will
    ask for its challenge. Failed validations count against Let's Encrypt's limits, so this is checked first."""
    token = secrets.token_hex(16)
    probe = config.webmail_root / _CHALLENGES / f"mailctl-{token}"
    address = min(addresses, key=lambda ip: (ip.version, ip))
    files.replace(probe, token)
    try:
        deadline = time.monotonic() + SERVE_TIMEOUT
        while not _serves(address, name, probe.name, token):
            if time.monotonic() >= deadline:
                raise MailctlError(
                    f"OpenLiteSpeed doesn't serve {name} on port 80.",
                    hint="Run the Ansible playbook: it adds the webmail sites to OpenLiteSpeed's configuration.",
                )
            time.sleep(0.5)
    finally:
        files.remove(probe)


def _serves(address: IPAddress, name: str, file_name: str, token: str) -> bool:
    connection = http.client.HTTPConnection(str(address), 80, timeout=2)
    try:
        connection.request("GET", f"/{_CHALLENGES}/{file_name}", headers={"Host": name})
        response = connection.getresponse()
        return response.status == 200 and response.read().decode(errors="replace").strip() == token
    except OSError:
        return False
    finally:
        connection.close()


def _request_certificate(config: Config, name: str) -> None:
    account = ["--email", config.letsencrypt_email] if config.letsencrypt_email else ["--register-unsafely-without-email"]
    try:
        _certbot(config, "certonly", "--webroot", "--webroot-path", str(config.webmail_root), "--cert-name", name,
                 "--domains", name, "--agree-tos", *account)
    except MailctlError as problem:
        raise MailctlError(f"No certificate for {name}: {_reason(problem.message)}") from None


def _delete_certificate(config: Config, name: str) -> bool:
    if not has_certificate(config, name):
        return False
    _certbot(config, "delete", "--cert-name", name)
    return True


def _certbot(config: Config, *args: str) -> None:
    # --config-dir, so certbot keeps the certificates where has_certificate() looks.
    system.run("certbot", *args, "--non-interactive", "--config-dir", str(config.letsencrypt_dir))


def _reason(message: str) -> str:
    """What Let's Encrypt reported (certbot's "Detail:" lines), or else all of certbot's error."""
    details = [line.partition("Detail:")[2].strip() for line in message.splitlines() if "Detail:" in line]
    return " ".join(details) or message


def _directory(config: Config) -> Path:
    return config.ols_root / "conf" / "webmail"


def _live(config: Config, name: str) -> Path:
    return config.letsencrypt_dir / "live" / name


def _virtual_host(config: Config, name: str) -> str:
    return f"""
virtualhost {name} {{
  vhRoot                  {config.webmail_root}/
  configFile              {_directory(config) / name}.conf
  allowSymbolLink         0
  enableScript            1
  restrained              1
}}
"""


def _map(name: str) -> str:
    return f"map {name} {name}\n"


def _site(config: Config, name: str, certified: bool) -> str:
    challenges = f"""
context /{_CHALLENGES}/ {{
  location                {config.webmail_root}/{_CHALLENGES}/
  allowBrowse             1
}}
"""
    if not certified:
        return f"""{_HEADER}# No certificate yet, so it only answers Let's Encrypt's challenges.
docRoot                 {config.webmail_root}/
{challenges}
context / {{
  allowBrowse             0
}}
"""
    # SOGo builds its links, and decides whether its cookies are secure, from these.
    headers = "\n".join(
        f"RequestHeader set x-webobjects-{key} {value}"
        for key, value in [
            ("server-protocol", "HTTP/1.0"), ("server-name", name), ("server-port", "443"),
            ("server-url", f"https://{name}"),
        ]
    )
    live = _live(config, name)
    return f"""{_HEADER}docRoot                 {config.webmail_root}/
enableGzip              1

vhssl {{
  keyFile                 {live}/privkey.pem
  certFile                {live}/fullchain.pem
  certChain               1
}}

# SOGo. ActiveSync holds requests for up to 280 seconds before it answers (sogo.conf).
extprocessor sogo {{
  type                    proxy
  address                 {config.sogo_address}
  maxConns                100
  initTimeout             330
  retryTimeout            0
  respBuffer              0
}}

rewrite {{
  enable                  1
  rules                   <<<END_rules
RewriteCond %{{HTTPS}} !on
RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
RewriteRule ^ https://{name}%{{REQUEST_URI}} [R=301,L]
RewriteRule ^/\\.well-known/(caldav|carddav)$ /SOGo/dav/ [R=301,L]
  END_rules
}}
{challenges}
context /SOGo.woa/WebServerResources/ {{
  location                {config.sogo_resources}/
  allowBrowse             1
}}

context /SOGo {{
  type                    proxy
  handler                 sogo
  addDefaultCharset       off
  extraHeaders            <<<END_extraHeaders
{headers}
  END_extraHeaders
}}

# Phones ask for /Microsoft-Server-ActiveSync, which SOGo answers under /SOGo. A
# pattern, so OpenLiteSpeed doesn't first redirect it to a folder with a "/".
context exp:^/Microsoft-Server-ActiveSync {{
  allowBrowse             1
  extraHeaders            <<<END_extraHeaders
{headers}
  END_extraHeaders
  rewrite {{
    enable                1
    rules                 <<<END_rules
RewriteRule ^ http://sogo/SOGo/Microsoft-Server-ActiveSync [P,L]
    END_rules
  }}
}}

context exp:^/$ {{
  type                    redirect
  location                /SOGo/
  externalRedirect        1
  statusCode              302
}}

# LiteSpeed's page cache stays off, so one user's mail is never served to another.
module cache {{
  ls_enabled              0
}}
"""
