"""Webmail: a site at webmail.DOMAIN in front of SOGo, for every mail domain whose webmail name points to this server.

A site is an OpenLiteSpeed virtual host with a Let's Encrypt certificate of its own, which certbot gets through the
site itself. Until the certificate exists, the site only answers Let's Encrypt's challenges, so SOGo is never
offered without encryption.

The sites are virtual hosts of their own in OpenLiteSpeed's config, marked with mailctl's note, and not members of a
template: SOGo needs a request header with the site's own name, and OpenLiteSpeed doesn't fill in variables in request
headers. Their settings are in webmail_sites, a directory of root's: OpenLiteSpeed's own conf directory belongs to
lsadm, and a link planted there could make mailctl, running as root, write or delete any file. The note is the only
record of which sites exist; mailctl leaves every other virtual host alone.
"""

import http.client
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import files, names, openlitespeed, system
from .config import Config
from .dns_check import IPAddress, LookupFailed, Resolver
from .errors import MailctlError
from .openlitespeed import VirtualHost

PREFIX = "webmail."
SERVE_TIMEOUT = 10  # seconds OpenLiteSpeed gets to serve a new site after a restart
_HEADER = "# Written by mailctl webmail sync. Changes are overwritten.\n"
_NOTE = "Managed by mailctl: webmail (mailctl webmail sync)"
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

    @property
    def domain(self) -> str:
        return self.host.removeprefix(PREFIX)


@dataclass(frozen=True)
class Result:
    outcomes: tuple[Outcome, ...]
    changed: bool


@dataclass
class _Plan:
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    kept: set[str] = field(default_factory=set)  # the sites that stay or come
    removed: set[str] = field(default_factory=set)  # the sites that go, with their certificates
    uncertified: dict[str, set[IPAddress]] = field(default_factory=dict)  # sites to certify, with their addresses

    def keep(self, outcome: Outcome) -> None:
        self.outcomes[outcome.host] = outcome
        self.kept.add(outcome.host)

    def drop(self, name: str, reason: str) -> None:
        self.outcomes[name] = Outcome(name, State.REMOVED, reason)
        self.removed.add(name)

    def wait(self, name: str, reason: str) -> None:
        self.outcomes[name] = Outcome(name, State.WAITING, reason)


class NotPointingHere(Exception):
    """The name has no address, or an address that isn't this server's."""


def host(domain: str) -> str:
    return PREFIX + domain


def sites(config: Config) -> list[str]:
    return _ours(openlitespeed.read(config.ols_root))


def has_certificate(config: Config, name: str) -> bool:
    return all((_live(config, name) / file).is_file() for file in ("fullchain.pem", "privkey.pem"))


def resolve_to_this_server(resolver: Resolver, name: str, server_ips: set[IPAddress]) -> set[IPAddress]:
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
    lines = openlitespeed.read(config.ols_root)
    http, https = openlitespeed.listeners(lines)
    if not http:
        raise MailctlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a site.",
                           hint="Add one in WebAdmin.")
    if not https:
        raise MailctlError("OpenLiteSpeed has no HTTPS listener on port 443, and webmail is only offered over HTTPS.",
                           hint="Add one in WebAdmin.")
    plan = _plan(config, lines, domains, server_ips, resolver)
    changed = _publish(config, plan.kept)
    for name in sorted(plan.removed):
        changed |= _delete_certificate(config, name)
    for name, addresses in plan.uncertified.items():
        try:
            _wait_until_served(config, name, addresses)
            _request_certificate(config, name)
        except MailctlError as problem:
            plan.outcomes[name] = Outcome(name, State.FAILED, " ".join(filter(None, (problem.message, problem.hint))))
        else:
            plan.outcomes[name] = Outcome(name, State.NEW)
            changed = True
    # Moves the sites that got their certificate to the HTTPS listeners.
    _publish(config, plan.kept)
    return Result(tuple(plan.outcomes[name] for name in sorted(plan.outcomes)), changed)


def _plan(config: Config, lines: list[str], domains: Iterable[str], server_ips: set[IPAddress],
          resolver: Resolver) -> _Plan:
    """What sync() does for each site, from the domains, their webmail names' DNS and OpenLiteSpeed's config."""
    # The old helper script stored domains as it was given them, capitals and all. What isn't a domain name can't
    # have a site, and must never reach OpenLiteSpeed's configuration.
    wanted = {name for name in (host(domain.lower()) for domain in domains) if names.valid_domain(name)}
    existing = set(_ours(lines))
    plan = _Plan()
    for name in sorted(wanted | existing):
        if name not in wanted:
            plan.drop(name, "Its domain is no longer on this server.")
            continue
        try:
            addresses = resolve_to_this_server(resolver, name, server_ips)
        except NotPointingHere as problem:
            if name in existing:
                plan.drop(name, str(problem))
            else:
                plan.wait(name, str(problem))
        except LookupFailed as problem:
            # A site stays as it is; one that doesn't exist yet waits for the next run.
            if name in existing:
                plan.keep(Outcome(name, State.UNCHECKED, str(problem)))
            else:
                plan.wait(name, str(problem))
        else:
            if name not in existing and _taken(lines, name):
                plan.outcomes[name] = Outcome(name, State.FAILED, (
                    f"OpenLiteSpeed already has a site for {name}, which mailctl leaves alone."
                    f" Remove it in WebAdmin to get webmail for {name.removeprefix(PREFIX)}."
                ))
                continue
            plan.keep(Outcome(name, State.LIVE))
            if not has_certificate(config, name):
                plan.uncertified[name] = addresses
    return plan


def _ours(lines: list[str]) -> list[str]:
    return sorted(name for name, note in openlitespeed.virtual_hosts(lines).items() if note == _NOTE)


def _taken(lines: list[str], name: str) -> bool:
    """Whether a virtual host mailctl doesn't manage has the name, or a listener maps the name to one."""
    http, https = openlitespeed.listeners(lines)
    mapped = [host for listener in http + https for host, hosts_names in openlitespeed.maps(lines, listener).items()
              if name in hosts_names]
    return name in openlitespeed.virtual_hosts(lines) or any(host != name for host in mapped)


def remove(config: Config, domain: str) -> bool:
    """Removes the domain's site and certificate. Returns whether it had a site."""
    name = host(domain)
    if name not in sites(config):
        return False
    _publish(config, set(sites(config)) - {name})
    _delete_certificate(config, name)
    return True


def _publish(config: Config, hosts: set[str]) -> bool:
    """Writes the sites' settings and puts exactly these sites in OpenLiteSpeed's config, and has OpenLiteSpeed read
    them if anything changed. Returns whether it did. A site is on the HTTPS listeners once its certificate exists."""
    ordered = sorted(hosts)
    certified = {name for name in ordered if has_certificate(config, name)}
    changed = False
    for name in ordered:  # before the config refers to them
        changed |= files.replace(_site_file(config, name), _site(config, name, name in certified))
    lines = openlitespeed.read(config.ols_root)
    http, https = openlitespeed.listeners(lines)
    updated = lines
    for name in _ours(lines):
        if name not in hosts:
            updated = openlitespeed.without_virtual_host(updated, name)
            for listener in http + https:
                updated = openlitespeed.without_map(updated, listener, name)
    for name in ordered:
        site = VirtualHost(name, f"{config.webmail_root}/", str(_site_file(config, name)), _NOTE)
        updated = openlitespeed.with_virtual_host(updated, site)
        for listener in http:
            updated = openlitespeed.with_map(updated, listener, name, name)
        for listener in https:
            if name in certified:
                updated = openlitespeed.with_map(updated, listener, name, name)
            else:
                updated = openlitespeed.without_map(updated, listener, name)
    if updated != lines:
        openlitespeed.write(config.ols_root, updated)
        changed = True
    for path in _directory(config).glob("*.conf"):  # once the config no longer refers to them
        if path.stem not in hosts:
            changed |= files.remove(path)
            files.remove(path.with_name(f"{path.name}.txt"))  # OpenLiteSpeed's copy of what it read
    if changed:
        openlitespeed.restart(config.ols_root)
    return changed


def _wait_until_served(config: Config, name: str, addresses: set[IPAddress]) -> None:
    """Waits until OpenLiteSpeed serves a test file from the challenge folder under the name at each of its addresses,
    as Let's Encrypt asks for its challenge at any of them. Failed validations count against Let's Encrypt's limits,
    so this is checked first."""
    token = secrets.token_hex(16)
    probe = config.webmail_root / _CHALLENGES / f"mailctl-{token}"
    files.replace(probe, token)
    try:
        deadline = time.monotonic() + SERVE_TIMEOUT
        for address in sorted(addresses, key=lambda ip: (ip.version, ip)):
            while not _serves(address, name, probe.name, token):
                if time.monotonic() >= deadline:
                    raise MailctlError(
                        f"OpenLiteSpeed doesn't serve {name} on port 80 at {address}.",
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
        return response.status == 200 and response.read(len(token) + 2).decode(errors="replace").strip() == token
    except OSError:
        return False
    finally:
        connection.close()


def _request_certificate(config: Config, name: str) -> None:
    email = config.letsencrypt_email
    account = ["--email", email] if email else ["--register-unsafely-without-email"]
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
    return config.webmail_sites


def _site_file(config: Config, name: str) -> Path:
    return _directory(config) / f"{name}.conf"


def _live(config: Config, name: str) -> Path:
    return config.letsencrypt_dir / "live" / name


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
