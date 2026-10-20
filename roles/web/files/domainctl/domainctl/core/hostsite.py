"""The site of this server's own name: a virtual host that only answers Let's Encrypt, so the hostname can have a
certificate. Postfix and Dovecot present that certificate, from /etc/letsencrypt/live/HOSTNAME.

It is a virtual host of its own named Server, like the one made by hand in WebAdmin on the older servers, with its
settings where WebAdmin keeps them (conf/vhosts/Server/vhconf.conf). Its note says domainctl made it; a Server
without that note is someone else's and left alone.

On a mail server the same certificate holds every mail.DOMAIN as well, which mailctl proves through a template of
its own. The hostname is left to this site, and those names keep the web root certbot already proves them under.
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from serverctl import certbot, files, openlitespeed, system
from serverctl.errors import CtlError
from serverctl.openlitespeed import VirtualHost

from ..config import TOOL, Config
from .serving import CHALLENGES

NAME = "Server"
NOTE = "Managed by domainctl: the site of this server's own name (domainctl hostname)"
# mailctl's template for the names in the mail certificate. It answered for the hostname before this site did.
MAIL_NAMES = "mailnames"
# RSA, like mailctl's: Postfix and Dovecot present it to mail programs and to Microsoft's ActiveSync servers, which
# refuse a certificate they see no RSA cipher suite for.
KEY_TYPE = "rsa"
# Certbot renews 30 days before the end, so one closer than this isn't being renewed.
RENEW_BEFORE = timedelta(days=30)
_HEADER = "# Written by domainctl hostname. Changes are overwritten.\n"


def webroot(config: Config) -> Path:
    return config.ols_root / NAME / "html"


def site_file(config: Config) -> Path:
    """Where WebAdmin keeps a virtual host's settings."""
    return config.ols_root / "conf" / "vhosts" / NAME / "vhconf.conf"


def certified(config: Config) -> bool:
    """Whether the hostname has a certificate from certbot, not the mail role's empty placeholder files."""
    return certbot.exists(config.letsencrypt_dir, config.hostname) and \
        not certbot.is_placeholder(config.letsencrypt_dir, config.hostname)


def publish(config: Config, with_certificate: bool) -> bool:
    """Puts the site in OpenLiteSpeed's config, on port 80, and on 443 once the certificate is there. Returns
    whether anything changed; OpenLiteSpeed reads it when it restarts."""
    _check_free(config, openlitespeed.read(config.ols_root))  # before its settings file is written
    (webroot(config) / CHALLENGES).mkdir(parents=True, exist_ok=True)
    with system.as_user(config.ols_user):
        changed = files.replace(site_file(config), _settings(config, with_certificate))
    with openlitespeed.locked():
        lines = openlitespeed.read(config.ols_root)
        _check_free(config, lines)
        http, https = openlitespeed.listeners(lines)
        if not http:
            raise CtlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a name.",
                           hint="Add one in WebAdmin.")
        site = VirtualHost(NAME, f"{config.ols_root / NAME}/", f"$SERVER_ROOT/conf/vhosts/{NAME}/vhconf.conf", NOTE)
        updated = openlitespeed.with_virtual_host(lines, site)
        # Two sites for one name would leave it to OpenLiteSpeed which one Let's Encrypt reaches.
        updated = openlitespeed.without_member(updated, config.hostname, MAIL_NAMES)
        for listener in http:
            updated = openlitespeed.with_map(updated, listener, NAME, config.hostname)
        for listener in https:
            if with_certificate:
                updated = openlitespeed.with_map(updated, listener, NAME, config.hostname)
            else:
                updated = openlitespeed.without_map(updated, listener, NAME)
        if updated != lines:
            openlitespeed.write(config.ols_root, updated, TOOL)
            changed = True
    return changed


def _check_free(config: Config, lines: list[str]) -> None:
    hosts = openlitespeed.virtual_hosts(lines)
    if NAME in hosts and hosts[NAME] != NOTE:
        raise CtlError(f"OpenLiteSpeed already has a virtual host named {NAME}, which domainctl didn't make.",
                       hint="If it is the site of this server's name made by hand, delete it in WebAdmin and run "
                            "this again.")
    http, https = openlitespeed.listeners(lines)
    for listener in http + https:
        for host, names in openlitespeed.maps(lines, listener).items():
            if host != NAME and config.hostname in names:
                raise CtlError(f"The virtual host {host} already answers for {config.hostname}.",
                               hint=f"Take {config.hostname} off its domains in WebAdmin and run this again.")


def needs_certificate(config: Config, now: datetime) -> str:
    """Why the hostname needs a new certificate, or "" when the one there will do."""
    if not certified(config):
        return f"{config.hostname} has no certificate yet."
    found = certbot.read(certbot.live(config.letsencrypt_dir, config.hostname) / "fullchain.pem")
    if not certbot.covers(found, config.hostname):
        return f"The certificate doesn't include {config.hostname}."
    if found.expires - now <= RENEW_BEFORE:
        return f"The certificate expires on {found.expires:%Y-%m-%d}, which certbot's own renewal should have done."
    if certbot.webroots(config.letsencrypt_dir, config.hostname).get(config.hostname) != webroot(config):
        return f"Certbot proves {config.hostname} through another site, so renewing it would fail now."
    return ""


def proofs(config: Config) -> dict[str, Path]:
    """The names for the certificate, each with the web root certbot proves it under: the hostname under this site,
    and every other name it has now (mailctl's) under the one certbot already uses for it."""
    kept = certbot.webroots(config.letsencrypt_dir, config.hostname) if certified(config) else {}
    return {config.hostname: webroot(config), **{name: root for name, root in kept.items() if name != config.hostname}}


def obtain(config: Config) -> None:
    """Gets the certificate, after a test run: a failed validation counts against Let's Encrypt's limits, a test
    run's doesn't. Raises CtlError with what Let's Encrypt said when it refuses."""
    roots = proofs(config)
    with certbot.placeholders_aside(config.letsencrypt_dir, config.hostname):
        for dry_run in (True, False):
            certbot.obtain_each(config.letsencrypt_dir, config.letsencrypt_email, config.hostname, roots, KEY_TYPE,
                                dry_run=dry_run)


def reload_mail() -> list[str]:
    """Has Postfix and Dovecot read the certificate again, where they are installed, and starts one that couldn't
    start on the placeholder files. Returns the ones that did."""
    reloaded = []
    for program, service, label in (("postfix", "postfix", "Postfix"), ("doveadm", "dovecot", "Dovecot")):
        if shutil.which(program):
            system.reload(service)
            reloaded.append(label)
    return reloaded


def _settings(config: Config, with_certificate: bool) -> str:
    live = certbot.live(config.letsencrypt_dir, config.hostname)
    ssl = f"""
vhssl {{
  keyFile                 {live}/privkey.pem
  certFile                {live}/fullchain.pem
  certChain               1
}}
""" if with_certificate else ""
    return f"""{_HEADER}# The site of {config.hostname}. It serves nothing but Let's Encrypt's challenges.
docRoot                 $VH_ROOT/html/
{ssl}
context /{CHALLENGES}/ {{
  location                $VH_ROOT/html/{CHALLENGES}/
  allowBrowse             1
}}

context / {{
  allowBrowse             0
}}
"""
