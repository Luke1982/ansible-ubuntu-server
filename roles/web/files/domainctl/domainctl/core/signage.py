"""piSignage servers: one per customer, each at a name of its own like andre.milocast.nl, over HTTPS.

Each is a piSignage server of its own (github.com/colloqi/pisignage-server, a Node.js app) under a Linux user of its
own: the code in ~/pisignage-server, the media its customer uploads beside it in ~/media, where piSignage looks for
them, and its data in a MongoDB database named after the user. systemd runs it as pisignage@USER, the unit Ansible
installs (roles/web/templates/pisignage@.service.j2), on a port of its own: 3000 for the first, 3001 for the next.
The firewall keeps those ports closed, so OpenLiteSpeed is the only way in.

Its settings, /etc/pisignage/USER.env, are the record of which servers there are. systemd hands them to the app:
PORT and MONGOLAB_URI are what piSignage reads, DOMAIN is for domainctl.

OpenLiteSpeed sits in front of each, in a virtual host of its own named after the user: a template gives all its
members the same settings, and each of these sends its requests to another port. On port 80 it answers Let's
Encrypt and sends everyone else to HTTPS; on 443, once the certificate is there, it passes every request on to the
app, the players' WebSocket connections included. The certificate is named after the user, like a site's.
"""

import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from serverctl import certbot, files, openlitespeed, system
from serverctl.errors import CtlError
from serverctl.openlitespeed import VirtualHost

from ..config import TOOL, Config
from . import sites
from .serving import CHALLENGES

NOTE = "Managed by domainctl: a piSignage server (domainctl signage)"
FIRST_PORT = 3000
CODE = "pisignage-server"
MEDIA = "media"  # beside the code: piSignage keeps its uploads in ../media
UNIT_FILE = Path("/etc/systemd/system/pisignage@.service")  # Ansible installs it with MongoDB and Node.js
MONGODB = "mongodb://127.0.0.1:27017"
START_TIMEOUT = 60  # seconds piSignage gets to answer on its port once it is started
POLL = 0.5  # seconds between tries
_HEADER = "# Written by domainctl signage. Changes are overwritten.\n"
_DATABASE_NAME = re.compile(r"[A-Za-z0-9_-]{1,63}")


@dataclass(frozen=True)
class Signage:
    user: str  # the Linux user, the virtual host, and the name of the certificate
    domain: str
    port: int

    @property
    def names(self) -> tuple[str, ...]:
        """The names the certificate covers."""
        return (self.domain,)

    @property
    def unit(self) -> str:
        return unit(self.user)

    @property
    def database(self) -> str:
        return database(self.user)


def unit(user: str) -> str:
    return f"pisignage@{user}.service"


def database(user: str) -> str:
    return f"pisignage-{user}"


def code_dir(config: Config, user: str) -> Path:
    return config.home(user) / CODE


def media_dir(config: Config, user: str) -> Path:
    return config.home(user) / MEDIA


def settings_file(config: Config, user: str) -> Path:
    return config.signage_settings / f"{user}.env"


def root(config: Config, user: str) -> Path:
    """The virtual host's own directory: it only holds Let's Encrypt's challenges, since piSignage serves the rest."""
    return config.ols_root / "signage" / user


def webroot(config: Config, user: str) -> Path:
    return root(config, user) / "html"


def site_file(config: Config, user: str) -> Path:
    """Where WebAdmin keeps a virtual host's settings."""
    return config.ols_root / "conf" / "vhosts" / user / "vhconf.conf"


def installed() -> bool:
    """Whether the playbook installed what piSignage runs on: MongoDB, Node.js and the unit."""
    return UNIT_FILE.is_file()


def made_by_domainctl(config: Config, user: str) -> bool:
    """Whether the Linux user is one domainctl made for a piSignage server: its home holds piSignage. A server that
    was deleted without --purge still has it, with its media and its database, and adding it again takes them up."""
    return code_dir(config, user).is_dir()


def all_servers(config: Config) -> list[Signage]:
    found = [find(config, path.stem) for path in sorted(config.signage_settings.glob("*.env"))]
    return [server for server in found if server]


def find(config: Config, user: str) -> Signage | None:
    text = files.read(settings_file(config, user))
    if text is None:
        return None
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    try:
        return Signage(user, values.get("DOMAIN", "").strip(), int(values.get("PORT", "")))
    except ValueError:
        raise CtlError(f"{settings_file(config, user)} has no valid PORT.",
                       hint=f"Write it again with: domainctl signage add {values.get('DOMAIN', 'DOMAIN').strip()}"
                            f" --user {user} --port PORT") from None


def find_domain(config: Config, domain: str) -> Signage | None:
    return next((server for server in all_servers(config) if server.domain == domain), None)


def taken(config: Config, domain: str, user: str, lines: list[str]) -> str | None:
    """What already answers for the domain on this server, other than this piSignage server itself."""
    for site in sites.list_sites(config, lines):
        if domain in site.names:
            return f"{domain} is already served by the site {site.user}."
    for other in all_servers(config):
        if other.user != user and other.domain == domain:
            return f"{domain} is already the piSignage server {other.user}."
    http, https = openlitespeed.listeners(lines)
    for listener in http + https:
        for host, names in openlitespeed.maps(lines, listener).items():
            if host != user and domain in names:
                return f"OpenLiteSpeed's virtual host {host} already answers for {domain}."
    return None


def serving(config: Config, names: tuple[str, ...]) -> str | None:
    """A name that a piSignage server already has, so "domainctl add" doesn't make a site for it too."""
    for server in all_servers(config):
        if server.domain in names:
            return f"{server.domain} is already the piSignage server {server.user}."
    return None


def port_problem(config: Config, user: str, port: int) -> str | None:
    """Why the port can't be this server's: another one has it, or something else listens on it."""
    other = next((server for server in all_servers(config) if server.port == port and server.user != user), None)
    if other:
        return f"Port {port} is the piSignage server {other.user}'s."
    mine = find(config, user)
    if (not mine or mine.port != port) and listening(port):
        return f"Something on this server listens on port {port} already."
    return None


def free_port(config: Config) -> int:
    """The first port from 3000 up that no other piSignage server has and nothing listens on."""
    taken_ports = {server.port for server in all_servers(config)}
    port = FIRST_PORT
    while port in taken_ports or listening(port):
        port += 1
    return port


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def save(config: Config, server: Signage) -> bool:
    """Writes the server's settings. Returns whether they changed, which needs a restart to take effect."""
    return files.replace(settings_file(config, server.user), settings(server))


def settings(server: Signage) -> str:
    return (f"{_HEADER}# The piSignage server of {server.domain}. systemd hands these to it as {server.unit}.\n"
            f"DOMAIN={server.domain}\n"
            f"PORT={server.port}\n"
            f"MONGOLAB_URI={MONGODB}/{server.database}\n")


def install(config: Config, user: str) -> list[str]:
    """Fetches piSignage and the packages it needs, as the user, unless they are there. Returns what it did.

    npm writes node_modules/.package-lock.json when it has installed everything, so an install that stopped halfway
    is done again."""
    done = []
    code = code_dir(config, user)
    if not code.is_dir():
        _as_user(config, user, "git", "clone", "--quiet", "--depth", "1", "--branch", config.signage_version,
                 config.signage_repo, str(code))
        done.append(f"Fetched piSignage ({config.signage_version}) into {code}.")
    if not (code / "node_modules" / ".package-lock.json").is_file():
        _as_user(config, user, "npm", "install", "--omit=dev", "--no-audit", "--no-fund", "--prefix", str(code))
        done.append("Installed the packages piSignage needs.")
    if not media_dir(config, user).is_dir():
        _as_user(config, user, "mkdir", str(media_dir(config, user)))
    return done


def _as_user(config: Config, user: str, *args: str) -> None:
    """Runs the command as the user, with its own home, so everything it makes is the user's."""
    system.run("runuser", "-u", user, "--", "env", f"HOME={config.home(user)}", *args)


def start(server: Signage, restart: bool) -> None:
    """Starts the server, now and at every boot. A restart makes it read settings that changed."""
    system.run("systemctl", "enable", "--quiet", server.unit)
    system.run("systemctl", "restart" if restart else "start", server.unit)


def stop(user: str) -> None:
    system.run("systemctl", "disable", "--now", "--quiet", unit(user))


def running(user: str) -> bool:
    try:
        system.run("systemctl", "is-active", "--quiet", unit(user))
    except CtlError:
        return False
    return True


def wait_until_answering(port: int, timeout: float = START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while not listening(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True


def drop_database(user: str) -> None:
    system.run("mongosh", "--quiet", "--norc", f"{MONGODB}/{database(user)}", "--eval", "db.dropDatabase()")


def valid_database(name: str) -> bool:
    """A database name that is safe in mongorestore's patterns, where * and $ mean something."""
    return _DATABASE_NAME.fullmatch(name) is not None


def restore(config: Config, server: Signage, from_database: str, source: BinaryIO) -> None:
    """Replaces the server's database with the one in the archive (mongodump --archive --gzip), and gives the user
    the media and data copied into its home, which arrive as root's. piSignage is stopped meanwhile, so it never
    sees half a database, and started again however it ends."""
    if not valid_database(from_database):
        raise CtlError(f"'{from_database}' isn't a database name this can read in.",
                       hint="Use letters, digits, - and _.")
    system.run("systemctl", "stop", server.unit)
    try:
        system.run_fed("mongorestore", "--quiet", f"--uri={MONGODB}", "--archive", "--gzip", "--drop",
                       f"--nsInclude={from_database}.*", f"--nsFrom={from_database}.*",
                       f"--nsTo={server.database}.*", source=source)
        for path in (media_dir(config, server.user), code_dir(config, server.user) / "data"):
            if path.is_dir():
                # -h: a link among the files changes owner itself, and never what it points at.
                system.run("chown", "-hR", f"{server.user}:{server.user}", str(path))
    finally:
        start(server, restart=True)


def certified(config: Config, server: Signage) -> bool:
    """Whether the server has a certificate from Let's Encrypt for its name."""
    if not certbot.exists(config.letsencrypt_dir, server.user) or \
            certbot.made_here(config.letsencrypt_dir, server.user):
        return False
    return certbot.covers(certbot.read(certbot.live(config.letsencrypt_dir, server.user) / "fullchain.pem"),
                          server.domain)


def obtain(config: Config, server: Signage) -> None:
    """Gets the certificate, after a test run: a failed validation counts against Let's Encrypt's limits, a test
    run's doesn't. Raises CtlError with what Let's Encrypt said when it refuses."""
    for dry_run in (True, False):
        certbot.obtain(config.letsencrypt_dir, config.letsencrypt_email, server.user, webroot(config, server.user),
                       server.names, dry_run=dry_run)


def publish(config: Config, server: Signage, with_certificate: bool) -> bool:
    """Puts the server's virtual host in OpenLiteSpeed's config, on port 80, and on 443 once the certificate is
    there. Returns whether anything changed; OpenLiteSpeed reads it when it restarts."""
    _check_ours(config, openlitespeed.read(config.ols_root), server.user)  # before its settings file is written
    (webroot(config, server.user) / CHALLENGES).mkdir(parents=True, exist_ok=True)
    with system.as_user(config.ols_user):
        changed = files.replace(site_file(config, server.user), _settings(config, server, with_certificate))
    with openlitespeed.locked():
        lines = openlitespeed.read(config.ols_root)
        _check_ours(config, lines, server.user)
        http, https = openlitespeed.listeners(lines)
        if not http:
            raise CtlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a name.",
                           hint="Add one in WebAdmin.")
        if not https:
            raise CtlError("OpenLiteSpeed has no HTTPS listener on port 443, so piSignage can't be served over HTTPS.",
                           hint="Add one in WebAdmin.")
        host = VirtualHost(server.user, f"{root(config, server.user)}/",
                           f"$SERVER_ROOT/conf/vhosts/{server.user}/vhconf.conf", NOTE)
        updated = openlitespeed.with_virtual_host(lines, host)
        for listener in http:
            updated = openlitespeed.with_map(updated, listener, server.user, server.domain)
        for listener in https:
            if with_certificate:
                updated = openlitespeed.with_map(updated, listener, server.user, server.domain)
            else:
                updated = openlitespeed.without_map(updated, listener, server.user)
        if updated != lines:
            openlitespeed.write(config.ols_root, updated, TOOL)
            changed = True
    return changed


def unpublish(config: Config, user: str) -> bool:
    """Takes the server's virtual host out of OpenLiteSpeed's config, with its settings and its directory. Returns
    whether OpenLiteSpeed's config changed."""
    with openlitespeed.locked():
        lines = openlitespeed.read(config.ols_root)
        _check_ours(config, lines, user)
        http, https = openlitespeed.listeners(lines)
        updated = openlitespeed.without_virtual_host(lines, user)
        for listener in http + https:
            updated = openlitespeed.without_map(updated, listener, user)
        changed = updated != lines
        if changed:
            openlitespeed.write(config.ols_root, updated, TOOL)
    with system.as_user(config.ols_user):
        files.remove(site_file(config, user))
        try:
            site_file(config, user).parent.rmdir()
        except OSError:
            pass  # not there, or something else is in it: either way it isn't ours to empty
    system.remove_tree(root(config, user))
    return changed


def _check_ours(config: Config, lines: list[str], user: str) -> None:
    hosts = openlitespeed.virtual_hosts(lines)
    if user in hosts and hosts[user] != NOTE:
        raise CtlError(f"OpenLiteSpeed already has a virtual host named {user}, which domainctl didn't make.",
                       hint="Choose another name with --user.")


def _settings(config: Config, server: Signage, with_certificate: bool) -> str:
    live = certbot.live(config.letsencrypt_dir, server.user)
    address = f"127.0.0.1:{server.port}"
    processor = f"pisignage-{server.user}"
    ssl = f"""
vhssl {{
  keyFile                 {live}/privkey.pem
  certFile                {live}/fullchain.pem
  certChain               1
}}
""" if with_certificate else ""
    return f"""{_HEADER}# The piSignage server of {server.domain}: {server.unit}, on {address}.
docRoot                 $VH_ROOT/html/
enableGzip              1
{ssl}
# piSignage answers an upload once it has looked the file over, which takes a while for a long video. The
# processor is named after the user: OpenLiteSpeed keeps external processors in one list for the whole server,
# so two virtual hosts that call theirs the same thing both end up at whichever was read first.
extprocessor {processor} {{
  type                    proxy
  address                 {address}
  maxConns                100
  initTimeout             600
  retryTimeout            0
  respBuffer              0
}}

# The players keep a WebSocket open to the server. Only a request for one comes here; everything else goes to the
# context below.
websocket / {{
  address                 {address}
}}

rewrite {{
  enable                  1
  rules                   <<<END_rules
RewriteCond %{{HTTPS}} !on
RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
RewriteRule ^ https://{server.domain}%{{REQUEST_URI}} [R=301,L]
  END_rules
}}

context /{CHALLENGES}/ {{
  location                $VH_ROOT/html/{CHALLENGES}/
  allowBrowse             1
}}

context / {{
  type                    proxy
  handler                 {processor}
  addDefaultCharset       off
}}
"""
