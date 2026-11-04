"""A site with a web root of its own: a virtual host of its own instead of a member of the shared template.

Every member of an OpenLiteSpeed template is served from the same place inside its home directory
($VH_ROOT/public_html/www). A member can be given its own domain, its own aliases and its own root directory,
and nothing else -- not its own document root. So a site whose pages live deeper, like the public folder of a
Laravel or Symfony application, cannot be a member. WebAdmin calls moving one out of its template
"instantiating" it: the site becomes a virtual host of its own, with its settings in
conf/vhosts/<user>/vhconf.conf. That is what this module writes.

The settings are the template's, with the site's own document root: the same logs, the same .htaccess, the same
redirect to HTTPS, and Let's Encrypt's challenges from a context of their own, so certbot reaches them before
the redirect. Everything else that makes it a domainctl site stays as it is -- the Linux user, the home
directory, the permissions, the certificate named after the user -- so only the virtual host is different.

Which web root the site has is written in the note on the virtual host: WebAdmin shows it there, domainctl
reads it back from there, and the settings file is written from it.
"""

from pathlib import Path

from serverctl import files, openlitespeed, serving, system
from serverctl.openlitespeed import VirtualHost

from ..config import Config

HEADER = "# Written by domainctl. Changes made here are overwritten; the site's own settings belong in its\n" \
         "# .htaccess, which OpenLiteSpeed reads on every request.\n"
# The template serves files, runs PHP, and follows a link inside the site's own home directory: the same three
# answers for a site that has its virtual host to itself.
SETTINGS = (("allowSymbolLink", "1"), ("enableScript", "1"), ("restrained", "1"))


def site_file(config: Config, user: str) -> Path:
    """Where WebAdmin keeps a virtual host's settings."""
    return config.ols_root / "conf" / "vhosts" / user / "vhconf.conf"


def config_file(user: str) -> str:
    """The same path as OpenLiteSpeed writes it, relative to its own directory."""
    return f"$SERVER_ROOT/conf/vhosts/{user}/vhconf.conf"


def virtual_host(config: Config, user: str, note: str) -> VirtualHost:
    return VirtualHost(user, f"{config.home(user)}/", config_file(user), note, SETTINGS)


def write_settings(config: Config, user: str, domain: str, webroot: str) -> bool:
    """Writes the site's settings file, as OpenLiteSpeed's own user. Returns whether it changed."""
    path = site_file(config, user)
    with system.as_user(config.ols_user):
        path.parent.mkdir(parents=True, exist_ok=True)
        return files.replace(path, settings(config, user, domain, webroot))


def remove_settings(config: Config, user: str) -> bool:
    """Takes the settings file away again, with the directory OpenLiteSpeed keeps it in."""
    path = site_file(config, user)
    with system.as_user(config.ols_user):
        gone = files.remove(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass  # something else is in there: it is not domainctl's to empty
    return gone


def settings(config: Config, user: str, domain: str, webroot: str) -> str:
    """The virtual host's settings file: the shared template's, with this site's document root."""
    docroot = f"$VH_ROOT/{webroot}"
    admin = f"adminEmails             {config.admin_email}\n" if config.admin_email else ""
    live = config.letsencrypt_dir / "live" / user
    return f"""{HEADER}# The site {domain}, served from {config.docroot_of(user, webroot)}.
docRoot                 {docroot}
{admin}enableGzip              1
enableBr                1
enableIpGeo             0

errorlog $VH_ROOT/{config.logs}/error.log {{
  useServer             0
  logLevel              {config.log_level}
  rollingSize           1M
  keepDays              30
  compressArchive       1
}}

accesslog $VH_ROOT/{config.logs}/access.log {{
  useServer             0
}}

# Let's Encrypt asks for its challenge over plain HTTP. Serving it from a context of its own means the folder
# is reachable whatever the site's own .htaccess does with the rest, and the redirect below lets it through.
context /{serving.CHALLENGES}/ {{
  location              {docroot}/{serving.CHALLENGES}/
  allowBrowse           1
}}

rewrite  {{
  enable                1
  autoLoadHtaccess      1
RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
RewriteCond %{{HTTPS}} off
RewriteRule .* https://%{{HTTP_HOST}}%{{REQUEST_URI}} [L,R=301]
}}

vhssl  {{
  keyFile               {live}/privkey.pem
  certFile              {live}/fullchain.pem
  certChain             1
}}
"""


def publish(config: Config, lines: list[str], user: str, names: tuple[str, ...], note: str) -> list[str]:
    """The config with the site's own virtual host on every listener, answering for its names."""
    updated = openlitespeed.with_virtual_host(lines, virtual_host(config, user, note))
    http, https = openlitespeed.listeners(lines)
    for listener in (*http, *https):
        updated = openlitespeed.with_map(updated, listener, user, *names)
    return updated


def unpublish(lines: list[str], user: str) -> list[str]:
    """The config without the site's own virtual host and without the names the listeners gave it."""
    http, https = openlitespeed.listeners(lines)
    for listener in (*http, *https):
        lines = openlitespeed.without_map(lines, listener, user)
    return openlitespeed.without_virtual_host(lines, user)
