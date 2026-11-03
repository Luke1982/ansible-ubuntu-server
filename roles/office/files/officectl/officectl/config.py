"""Settings from /etc/officectl/config.json, written by Ansible, plus system paths.

One setting has no default and comes from the inventory: office_host, the name Collabora is served under. Every
Nextcloud on this server gets a name under it, so that one name decides what the others are called.
"""

import json
import os
from dataclasses import MISSING, dataclass, fields
from pathlib import Path

from serverctl.errors import CtlError
from serverctl.transip import Access

TOOL = "officectl"
DEFAULT_PATH = Path("/etc/officectl/config.json")


@dataclass(frozen=True)
class Config:
    # The server's own name, used to label the API tokens officectl asks TransIP for.
    hostname: str
    # Where Collabora is served: office.example.nl. Each Nextcloud is at SLUG.office.example.nl.
    office_host: str
    # The web root of Collabora's own site. It holds nothing but Let's Encrypt's challenges: everything else is
    # handed to coolwsd.
    office_root: Path = Path("/var/www/office")
    # Collabora's own daemon: its settings, the service to restart, and where it listens for OpenLiteSpeed.
    coolwsd_config: Path = Path("/etc/coolwsd/coolwsd.xml")
    coolwsd_service: str = "coolwsd"
    coolwsd_address: str = "127.0.0.1"
    coolwsd_port: int = 9980
    # "loopback" or "any": coolwsd is reached through OpenLiteSpeed, so it listens on the loopback only.
    coolwsd_listen: str = "loopback"
    coolwsd_protocol: str = "IPv4"  # "IPv4", "IPv6" or "all"
    coolwsd_connections: int = 200  # how many requests OpenLiteSpeed may have open to it at once
    # A Nextcloud's Linux user is this and its slug: nc for the slug "acme" makes the user "ncacme". It keeps
    # the users of the Nextclouds apart from the web sites', which are named after their domain.
    user_prefix: str = "nc"
    home_root: Path = Path("/home")
    user_shell: str = "/bin/bash"  # so "su - USER" works; sshd's AllowUsers decides who may log in
    # The directories in a Nextcloud's home directory. The first is served, the second holds its files, which
    # are deliberately not under the web root, and the others its logs and temporary files.
    docroot: str = "public_html/www"
    data: str = "data"
    logs: str = "logs"
    tmp: str = "tmp"
    # OpenLiteSpeed
    ols_root: Path = Path("/usr/local/lsws")
    ols_user: str = "lsadm"  # owns OpenLiteSpeed's config directory, and WebAdmin runs as it
    web_user: str = "nobody"  # the user OpenLiteSpeed serves and runs PHP as, so Nextcloud runs as it too
    web_group: str = "nogroup"
    # LiteSpeed's PHP that runs Nextcloud: a version of its own, newer than the one the web sites share.
    php_package: str = "lsphp85"
    php_version: str = "8.5"
    php_children: int = 10  # pages one Nextcloud may serve at once
    # What that PHP is given per Nextcloud. Uploads are resumed in chunks, so the limit is only reached by a
    # browser that doesn't; the time limits are for the long jobs occ and cron.php run.
    php_memory_mb: int = 512
    php_upload_gb: int = 16
    php_time_limit: int = 3600
    php_opcache_mb: int = 256
    php_opcache_files: int = 20000
    php_apcu_mb: int = 128
    # Let's Encrypt. Collabora's site and every Nextcloud get a certificate of their own, named after the name
    # it is served under.
    letsencrypt_dir: Path = Path("/etc/letsencrypt")
    letsencrypt_email: str = ""  # for a new Let's Encrypt account; without it, one is made without an address
    # The TransIP login and key, shared with domainctl and mailctl so they're entered once per server.
    transip_settings: Path = Path("/etc/transip/transip.json")
    transip_key: Path = Path("/etc/transip/transip.key")
    # Nextclouds on this server that officectl didn't make: one set up by hand before, at a name of its own.
    # Collabora has to keep letting them in, or nobody there can open a document any more. Their names,
    # separated by commas, because Ansible writes every value in config.json as text.
    other_nextclouds: str = ""
    # Where Nextcloud is fetched from, with the checksum next to it, and how often its background jobs run.
    nextcloud_url: str = "https://download.nextcloud.com/server/releases/latest.tar.bz2"
    cron_dir: Path = Path("/etc/cron.d")
    cron_minutes: int = 5
    # Redis, installed by the web role: Nextcloud's cache and its file locking. Every Nextcloud may share one
    # database, because the keys it writes start with its own instance id.
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_database: int = 0
    # What a new Nextcloud is set up with. The address it sends from and the region it reads phone numbers in;
    # both are only set when it is installed, so they can be changed in Nextcloud itself afterwards.
    mail_host: str = "localhost"
    mail_port: int = 25
    mail_from: str = "nextcloud"
    mail_domain: str = ""  # the part after the @; the office host's own domain when left out
    phone_region: str = "NL"

    def user_of(self, slug: str) -> str:
        return f"{self.user_prefix}{slug}"

    def host_of(self, slug: str) -> str:
        return f"{slug}.{self.office_host}"

    def home(self, user: str) -> Path:
        return self.home_root / user

    def docroot_of(self, user: str) -> Path:
        return self.home(user) / self.docroot

    def data_of(self, user: str) -> Path:
        return self.home(user) / self.data

    def logs_of(self, user: str) -> Path:
        return self.home(user) / self.logs

    def tmp_of(self, user: str) -> Path:
        return self.home(user) / self.tmp

    def php_root(self) -> Path:
        return self.ols_root / self.php_package

    def lsphp(self) -> Path:
        """The PHP OpenLiteSpeed runs as an external application."""
        return self.php_root() / "bin" / "lsphp"

    def php(self) -> Path:
        """The same PHP on the command line, for occ and for Nextcloud's background jobs."""
        return self.php_root() / "bin" / "php"

    def php_ini_dir(self) -> Path:
        return self.php_root() / "etc" / "php" / self.php_version

    def php_settings_dir(self, user: str) -> Path:
        """Where the settings of one Nextcloud's PHP go. PHP reads every .ini in it."""
        return self.php_ini_dir() / f"{user}.d"

    def other_hosts(self) -> list[str]:
        """The Nextclouds here that officectl didn't make."""
        return [name.strip() for name in self.other_nextclouds.split(",") if name.strip()]

    def sender(self) -> str:
        return f"{self.mail_from}@{self.mail_domain or self.office_host.partition('.')[2] or self.office_host}"

    def transip_access(self) -> Access:
        return Access(self.transip_settings, self.transip_key, self.hostname, TOOL)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("OFFICECTL_CONFIG", DEFAULT_PATH))
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise CtlError(f"Can't read {path}: {error.strerror}.",
                       hint="Run the Ansible playbook for this server.") from None
    except ValueError as error:  # invalid JSON or invalid UTF-8
        raise CtlError(f"{path} isn't valid JSON: {error}.") from None
    if not isinstance(data, dict):
        raise CtlError(f"{path} should hold a JSON object.")

    settings = {setting.name: setting for setting in fields(Config)}
    unknown = sorted(data.keys() - settings.keys())
    if unknown:
        raise CtlError(f"Unknown setting in {path}: {', '.join(unknown)}.")
    missing = [name for name, setting in settings.items()
               if setting.default is MISSING and setting.default_factory is MISSING and name not in data]
    if missing:
        raise CtlError(f"{path} is missing: {', '.join(missing)}.")

    values = {}
    for name, value in data.items():
        try:
            values[name] = _as(settings[name].default, value)
        except (TypeError, ValueError):
            raise CtlError(f"Invalid value for {name} in {path}.") from None
    return Config(**values)


def _as(default, value):
    """The value in the type the setting's default has; a setting without one is text."""
    if isinstance(default, Path):
        return Path(_text(value))
    if isinstance(default, bool):  # before int: in Python a bool is one
        return _flag(value)
    if isinstance(default, int):
        return _number(value)
    return _text(value)


def _text(value) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value


def _flag(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError(value)
    return value


def _number(value) -> int:
    """A whole number, given as a number or as text: Ansible writes every value in config.json as text unless
    it is told otherwise, so a port read from there is "9980" and not 9980."""
    if isinstance(value, bool):
        raise TypeError(value)
    if isinstance(value, int):
        return value
    return int(_text(value).strip())
