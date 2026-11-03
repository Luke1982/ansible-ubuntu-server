"""Collabora's own site: the name people's browsers open documents at, and the only way in to coolwsd.

coolwsd speaks plain HTTP on the loopback, so this site holds the certificate and hands everything through to
it: the pages, and the websocket a document is edited over. It serves one thing itself, Let's Encrypt's
challenge folder, so it can have that certificate.

It is a virtual host of its own named after the name it answers to, with its settings where WebAdmin keeps a
site's settings, so WebAdmin shows it like any other. Its note says officectl made it; a virtual host of that
name without the note is somebody else's and is left alone.
"""

from datetime import datetime, timedelta
from pathlib import Path

from serverctl import certbot, files, openlitespeed, serving, system
from serverctl.errors import CtlError
from serverctl.openlitespeed import VirtualHost

from ..config import TOOL, Config

NOTE = "Managed by officectl: Collabora Online (officectl endpoint)"
# Certbot renews 30 days before the end, so one closer than this isn't being renewed.
RENEW_BEFORE = timedelta(days=30)
HEADER = "# Written by officectl. Changes are overwritten.\n"
# The settings that make this a virtual host serving almost nothing of its own: no scripts, no links out of its
# directory, and OpenLiteSpeed's own user, since the site has no user of its own.
SETTINGS = (("allowSymbolLink", "0"), ("enableScript", "0"), ("restrained", "1"), ("setUIDMode", "0"))


def webroot(config: Config) -> Path:
    return config.office_root


def site_file(config: Config) -> Path:
    """Where WebAdmin keeps a virtual host's settings."""
    return config.ols_root / "conf" / "vhosts" / config.office_host / "vhconf.conf"


def certificate_path(config: Config) -> Path:
    return certbot.live(config.letsencrypt_dir, config.office_host) / "fullchain.pem"


def certified(config: Config) -> bool:
    return certbot.exists(config.letsencrypt_dir, config.office_host) and \
        not certbot.is_placeholder(config.letsencrypt_dir, config.office_host)


def publish(config: Config, with_certificate: bool) -> bool:
    """Puts the site in OpenLiteSpeed's config, on port 80, and on 443 once the certificate is there. Returns
    whether anything changed; OpenLiteSpeed reads it when it restarts."""
    host = config.office_host
    (webroot(config) / serving.CHALLENGES).mkdir(parents=True, exist_ok=True)
    with openlitespeed.locked():
        lines = openlitespeed.read(config.ols_root)
        _check_free(config, lines)
        http, https = openlitespeed.listeners(lines)
        if not http:
            raise CtlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a name.",
                           hint="Add one in WebAdmin.")
        with system.as_user(config.ols_user):
            site_file(config).parent.mkdir(parents=True, exist_ok=True)
            changed = files.replace(site_file(config), settings(config, with_certificate))
        site = VirtualHost(host, f"{webroot(config)}/", f"$SERVER_ROOT/conf/vhosts/{host}/vhconf.conf", NOTE,
                           SETTINGS)
        updated = openlitespeed.with_virtual_host(lines, site)
        for listener in http:
            updated = openlitespeed.with_map(updated, listener, host, host)
        for listener in https:
            # Without a certificate of its own the site would be served with the one on the listener, which no
            # browser trusts, and Nextcloud would refuse to talk to it at all.
            if with_certificate:
                updated = openlitespeed.with_map(updated, listener, host, host)
            else:
                updated = openlitespeed.without_map(updated, listener, host)
        if updated != lines:
            openlitespeed.write(config.ols_root, updated, TOOL)
            changed = True
    return changed


def _check_free(config: Config, lines: list[str]) -> None:
    """Stops when something else on this server already answers to the name."""
    hosts = openlitespeed.virtual_hosts(lines)
    if config.office_host in hosts and hosts[config.office_host] != NOTE:
        raise _not_ours(config)
    http, https = openlitespeed.listeners(lines)
    for listener in http + https:
        for host, names in openlitespeed.maps(lines, listener).items():
            if host != config.office_host and config.office_host in names:
                raise CtlError(f"The virtual host {host} already answers for {config.office_host}.",
                               hint=f"Take {config.office_host} off its domains in WebAdmin and run this again.")


def _not_ours(config: Config) -> CtlError:
    return CtlError(f"OpenLiteSpeed already has a virtual host named {config.office_host}, which officectl "
                    f"didn't make.",
                    hint="If it is a Collabora site made by hand, delete it in WebAdmin and run this again.")


def needs_certificate(config: Config, now: datetime) -> str:
    """Why the site needs a new certificate, or "" when the one there will do."""
    if not certified(config):
        return f"{config.office_host} has no certificate yet."
    found = certbot.read(certificate_path(config))
    if not certbot.covers(found, config.office_host):
        return f"The certificate doesn't include {config.office_host}."
    if found.expires - now <= RENEW_BEFORE:
        return f"The certificate expires on {found.expires:%Y-%m-%d}, which certbot's own renewal should have done."
    return ""


def obtain(config: Config) -> None:
    """Gets the site's certificate, after a test run: a failed validation counts against Let's Encrypt's limits,
    a test run's doesn't. Raises CtlError with what Let's Encrypt said when it refuses."""
    for dry_run in (True, False):
        certbot.obtain(config.letsencrypt_dir, config.letsencrypt_email, config.office_host, webroot(config),
                       [config.office_host], dry_run=dry_run)


def settings(config: Config, with_certificate: bool) -> str:
    """The site's own settings file, as WebAdmin would write it."""
    live = certbot.live(config.letsencrypt_dir, config.office_host)
    where = f"{config.coolwsd_address}:{config.coolwsd_port}"
    ssl = f"""
# The site's own certificate, which certbot got through the site itself.
vhssl  {{
  keyFile                 {live}/privkey.pem
  certFile                {live}/fullchain.pem
  certChain               1
}}
""" if with_certificate else ""
    return f"""{HEADER}# Collabora Online at {config.office_host}. coolwsd itself listens on {where} without
# TLS (ssl.termination true in its settings): this site holds the certificate and hands everything to it.
docRoot                   {webroot(config)}/
# Collabora compresses what it sends itself, and its websocket is not compressed at all.
enableGzip                0
{ssl}
errorlog  {{
  useServer               1
}}

extprocessor coolwsd  {{
  type                    proxy
  address                 {where}
  maxConns                {config.coolwsd_connections}
  initTimeout             60
  retryTimeout            0
  respBuffer              0
}}

# A document is edited over a websocket (/cool/<document>/ws), which is not an ordinary request and needs this
# line of its own.
websocket /cool/  {{
  address                 {where}
}}

rewrite  {{
  enable                  1
  rules                   <<<END_rules
RewriteCond %{{HTTPS}} !on
RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
RewriteRule ^ https://{config.office_host}%{{REQUEST_URI}} [R=301,L]
  END_rules
}}

# Let's Encrypt's challenges, for the site's own certificate. They are the only thing this site answers itself,
# and the only thing it answers over plain HTTP.
context /{serving.CHALLENGES}/  {{
  location                {webroot(config)}/{serving.CHALLENGES}/
  allowBrowse             1
}}

context /  {{
  type                    proxy
  handler                 coolwsd
  addDefaultCharset       off
}}

# LiteSpeed's page cache stays off: one person's document is never served to another.
module cache  {{
  ls_enabled              0
}}
"""
