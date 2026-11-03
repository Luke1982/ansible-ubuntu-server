"""A Nextcloud of its own: the virtual host it is served by, and which ones this server has.

Each Nextcloud is a virtual host named after its Linux user, like the web sites are, and the listeners give it
one name: SLUG.office.example.nl. Its settings file is written here, where WebAdmin keeps a site's settings, so
WebAdmin shows it like any other site; the note in the virtual host says officectl made it.

Which Nextclouds exist is read back from OpenLiteSpeed's own config, so there is one answer to that question and
no list of its own to fall out of step with it.
"""

from dataclasses import dataclass
from pathlib import Path

from serverctl import certbot, files, names, openlitespeed, serving, system
from serverctl.errors import CtlError
from serverctl.openlitespeed import VirtualHost

from ..config import TOOL, Config
from . import php

NOTE = "Managed by officectl: a Nextcloud (officectl add)"
HEADER = "# Written by officectl. Changes are overwritten.\n"
# A Nextcloud runs PHP, keeps its logs inside its own home directory, and is served by OpenLiteSpeed's own
# user, like the web sites are: it has a Linux user of its own, but nothing logs in as it.
SETTINGS = (("allowSymbolLink", "1"), ("enableScript", "1"), ("restrained", "1"), ("setUIDMode", "0"))
# What the slug may be: it becomes a name in DNS and the end of a Linux user name, so it is one label of
# letters, digits and hyphens, short enough to leave room for the prefix.
MAX_SLUG_LENGTH = 24


@dataclass(frozen=True)
class Tenant:
    slug: str  # what it is called: acme
    user: str  # its Linux user: ncacme
    host: str  # where it is served: acme.office.example.nl

    @property
    def url(self) -> str:
        return f"https://{self.host}"


def slug(value: str, config: Config) -> str:
    """The slug, checked. It ends up in DNS and in a user name, so both have to accept it."""
    given = value.strip().lower()
    if len(given) > MAX_SLUG_LENGTH:
        raise CtlError(f"'{value.strip()}' is too long: a name can have at most {MAX_SLUG_LENGTH} characters.")
    if "." in given:
        raise CtlError(f"'{value.strip()}' has a dot in it, and a name is one word.",
                       hint=f"Use the first part only: for acme.{config.office_host}, that is acme.")
    if not names.valid_user_name(given) or not names.valid_domain(f"{given}.{config.office_host}",
                                                                  names.MAX_DNS_NAME_LENGTH):
        raise CtlError(f"'{value.strip()}' isn't a valid name.",
                       hint="Use letters, digits and hyphens, starting with a letter or digit, like acme.")
    names.user_name(config.user_of(given))  # the prefix and the slug together are a Linux user name
    return given


def of(config: Config, name: str) -> Tenant:
    """The Nextcloud a slug stands for, whether or not it is on this server yet."""
    return Tenant(name, config.user_of(name), config.host_of(name))


def read(config: Config) -> list[str]:
    return openlitespeed.read(config.ols_root)


def list_tenants(config: Config, lines: list[str] | None = None) -> list[Tenant]:
    """The Nextclouds on this server: the virtual hosts officectl made, with the name each is served under."""
    lines = read(config) if lines is None else lines
    served = _served_names(lines)
    found = []
    for name, note in openlitespeed.virtual_hosts(lines).items():
        if note != NOTE:
            continue
        slug_of_it = name[len(config.user_prefix):] if name.startswith(config.user_prefix) else name
        found.append(Tenant(slug_of_it, name, served.get(name) or config.host_of(slug_of_it)))
    return sorted(found, key=lambda tenant: tenant.slug)


def find(config: Config, name: str, lines: list[str] | None = None) -> Tenant | None:
    """The Nextcloud with this slug, or with this name, or None."""
    found = list_tenants(config, lines)
    return next((tenant for tenant in found if name in (tenant.slug, tenant.user, tenant.host)), None)


def elsewhere(config: Config, tenant: Tenant, lines: list[str]) -> str | None:
    """What else on this server already answers to the Nextcloud's name, or to its own, or None."""
    hosts = openlitespeed.virtual_hosts(lines)
    if tenant.user in hosts and hosts[tenant.user] != NOTE:
        return f"OpenLiteSpeed has a virtual host of its own named {tenant.user}, which officectl leaves alone."
    http, https = openlitespeed.listeners(lines)
    for listener in (*http, *https):
        for host, served in openlitespeed.maps(lines, listener).items():
            if host != tenant.user and tenant.host in served:
                return f"{tenant.host} is already served by {host}."
    return None


def save(config: Config, tenant: Tenant, with_certificate: bool) -> bool:
    """Writes the Nextcloud's settings and puts its virtual host in OpenLiteSpeed's config, on port 80 and, once
    there is a certificate, on 443. Returns whether anything changed."""
    with openlitespeed.locked():
        lines = read(config)
        clash = elsewhere(config, tenant, lines)
        if clash:
            raise CtlError(clash, hint="Remove it first, or choose another name.")
        http, https = openlitespeed.listeners(lines)
        if not http:
            raise CtlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a name.",
                           hint="Add one in WebAdmin.")
        with system.as_user(config.ols_user):
            site_file(config, tenant.user).parent.mkdir(parents=True, exist_ok=True)
            changed = files.replace(site_file(config, tenant.user), settings(config, tenant, with_certificate))
        site = VirtualHost(tenant.user, f"{config.home(tenant.user)}/",
                           f"$SERVER_ROOT/conf/vhosts/{tenant.user}/vhconf.conf", NOTE, SETTINGS)
        updated = openlitespeed.with_virtual_host(lines, site)
        for listener in http:
            updated = openlitespeed.with_map(updated, listener, tenant.user, tenant.host)
        for listener in https:
            if with_certificate:
                updated = openlitespeed.with_map(updated, listener, tenant.user, tenant.host)
            else:
                updated = openlitespeed.without_map(updated, listener, tenant.user)
        if updated != lines:
            openlitespeed.write(config.ols_root, updated, TOOL)
            changed = True
    return changed


def remove(config: Config, tenant: Tenant) -> bool:
    """Takes the Nextcloud's virtual host out of OpenLiteSpeed's config. Returns whether anything changed."""
    with openlitespeed.locked():
        lines = read(config)
        updated = openlitespeed.without_virtual_host(lines, tenant.user)
        http, https = openlitespeed.listeners(lines)
        for listener in (*http, *https):
            updated = openlitespeed.without_map(updated, listener, tenant.user)
        if updated == lines:
            return False
        openlitespeed.write(config.ols_root, updated, TOOL)
        return True


def site_file(config: Config, user: str) -> Path:
    """Where WebAdmin keeps a virtual host's settings."""
    return config.ols_root / "conf" / "vhosts" / user / "vhconf.conf"


def certificate_path(config: Config, tenant: Tenant) -> Path:
    return certbot.live(config.letsencrypt_dir, tenant.host) / "fullchain.pem"


def certified(config: Config, tenant: Tenant) -> bool:
    return certbot.exists(config.letsencrypt_dir, tenant.host) and \
        not certbot.is_placeholder(config.letsencrypt_dir, tenant.host)


def obtain(config: Config, tenant: Tenant) -> None:
    """Gets the Nextcloud's certificate, after a test run: a failed validation counts against Let's Encrypt's
    limits, a test run's doesn't. Raises CtlError with what Let's Encrypt said when it refuses."""
    for dry_run in (True, False):
        certbot.obtain(config.letsencrypt_dir, config.letsencrypt_email, tenant.host,
                       config.docroot_of(tenant.user), [tenant.host], dry_run=dry_run)


def _served_names(lines: list[str]) -> dict[str, str]:
    """The first name each virtual host is given on a listener."""
    http, https = openlitespeed.listeners(lines)
    found: dict[str, str] = {}
    for listener in (*http, *https):
        for host, served in openlitespeed.maps(lines, listener).items():
            if served and host not in found:
                found[host] = served[0]
    return found


def settings(config: Config, tenant: Tenant, with_certificate: bool) -> str:
    """The Nextcloud's own settings file, as WebAdmin would write it."""
    user, docroot = tenant.user, config.docroot_of(tenant.user)
    live = certbot.live(config.letsencrypt_dir, tenant.host)
    # The address on OpenLiteSpeed's own error pages. Left out when this server has none, rather than written
    # as an empty setting.
    admin = f"adminEmails               {config.letsencrypt_email}\n" if config.letsencrypt_email else ""
    ssl = f"""
# The Nextcloud's own certificate, which certbot got through this site.
vhssl  {{
  keyFile                 {live}/privkey.pem
  certFile                {live}/fullchain.pem
  certChain               1
}}
""" if with_certificate else ""
    return f"""{HEADER}# The Nextcloud at {tenant.host}. A virtual host of its own, not a member of the web
# sites' template: it needs a newer PHP than the sites share, more of it, and settings of its own.
docRoot                   {docroot}/
{admin}enableGzip                1
enableBr                  1
enableIpGeo               0

errorlog {config.logs_of(user)}/error.log  {{
  useServer               0
  logLevel                ERROR
  rollingSize             1M
  keepDays                30
  compressArchive         1
}}

accesslog {config.logs_of(user)}/access.log  {{
  useServer               0
}}

index  {{
  useServer               0
  autoIndex               0
  indexFiles              index.php
}}

# The PHP of this Nextcloud alone: a pool only this site can reach, with its own settings
# ({php.settings_path(config, user)}).
extprocessor {php.processor(user)}  {{
  type                    lsapi
  address                 {php.socket(user)}
  maxConns                {config.php_children}
  env                     PHP_LSAPI_CHILDREN={config.php_children}
  env                     PHP_INI_SCAN_DIR={php.scan_dir(config, user)}
  env                     LSAPI_AVOID_FORK=200M
  initTimeout             60
  retryTimeout            0
  persistConn             1
  respBuffer              0
  autoStart               1
  path                    {config.lsphp()}
  backlog                 100
  instances               1
  memSoftLimit            0
  memHardLimit            0
  procSoftLimit           400
  procHardLimit           500
}}

scripthandler  {{
  add                     lsapi:{php.processor(user)} php
}}
{ssl}
# Let's Encrypt asks for its challenge over plain HTTP. Serving it from a context of its own means the folder is
# reachable whatever Nextcloud's own .htaccess does with the rest.
context /{serving.CHALLENGES}/  {{
  location                {docroot}/{serving.CHALLENGES}/
  allowBrowse             1
}}

rewrite  {{
  enable                  1
  autoLoadHtaccess        1
  rules                   <<<END_rules
RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
RewriteCond %{{HTTPS}} off
RewriteRule .* https://%{{HTTP_HOST}}%{{REQUEST_URI}} [L,R=301]
  END_rules
}}

context /  {{
  location                {docroot}/
  allowBrowse             1
  extraHeaders            <<<END_extraHeaders
Strict-Transport-Security: max-age=15552000; includeSubDomains
  END_extraHeaders
}}

# LiteSpeed's page cache stays off: one person's pages are never served to another.
module cache  {{
  ls_enabled              0
}}
"""
