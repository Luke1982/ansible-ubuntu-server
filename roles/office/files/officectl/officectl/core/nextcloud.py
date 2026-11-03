"""Nextcloud itself: its files, its own occ command, and the settings one needs on this server.

occ runs as the user PHP runs as, on the same PHP with the same settings as the pages, so what it does to the
cache the pages see is what they see. It is never run as root: a Nextcloud's code is root's and is only read, so
nothing Nextcloud does can change the program it is.

The files come from Nextcloud's own download server, checked against the checksum published next to them. They
are put in place by root and stay root's; the three places Nextcloud writes are set up by core/layout.py.
"""

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from serverctl import files, system
from serverctl.errors import CtlError

from ..config import Config
from . import layout, php
from .tenants import Tenant

# Nextcloud reaches MariaDB over the local socket, named here because PHP looks for it elsewhere than the
# command line client does.
DB_HOST = "localhost:/run/mysqld/mysqld.sock"
APP = "richdocuments"  # the app that puts Collabora's editor in Nextcloud's pages
DOWNLOAD_TIMEOUT = 600  # seconds; the archive is a few hundred megabytes
ARCHIVE_DIRECTORY = "nextcloud"  # what the archive holds it all in
_VERSION = re.compile(r"\$OC_VersionString\s*=\s*'([^']+)'")
# What is kept when the files are replaced by a newer release: the settings, and anything else somebody put
# next to them.
SETTINGS_FILE = "config.php"
CONFIG = "config"
APPS = "apps"


@dataclass(frozen=True)
class Setting:
    """One of Nextcloud's own settings, as occ reads and writes it."""
    keys: tuple[str, ...]
    value: str
    kind: str = "string"  # what occ calls it: string, integer or boolean

    @property
    def typed(self):
        """The value as occ gives it back, so what is there can be compared with what is wanted."""
        if self.kind == "integer":
            return int(self.value)
        if self.kind == "boolean":
            return self.value == "true"
        return self.value


def code_here(config: Config, user: str) -> bool:
    """Whether Nextcloud's files are in the web root."""
    return (config.docroot_of(user) / "occ").is_file()


def version(config: Config, user: str) -> str:
    """Which Nextcloud the files are, read from the files themselves: no PHP needed, and it answers for a
    Nextcloud that is too broken to run."""
    return version_in(config.docroot_of(user))


def version_in(tree: Path) -> str:
    """The same for a release that is unpacked but not in place yet."""
    found = _VERSION.search(files.read(tree / "version.php") or "")
    return found.group(1) if found else ""


# --- occ -----------------------------------------------------------------------------------------------------

def occ(config: Config, tenant: Tenant, *args: str) -> str:
    """Nextcloud's own command, run as the user its PHP runs as, from its own directory.

    runuser keeps nothing of root's environment, so the PHP settings of this Nextcloud and a home directory it
    may write in are given here: a program that finds no home directory writes its temporary files where it
    happens to stand.
    """
    user = tenant.user
    return system.run("runuser", "-u", config.web_user, "--", "env",
                      f"--chdir={config.docroot_of(user)}", f"HOME={config.tmp_of(user)}",
                      f"PHP_INI_SCAN_DIR={php.scan_dir(config, user)}",
                      str(config.php()), "occ", "--no-warnings", *args)


def status(config: Config, tenant: Tenant) -> dict:
    """What occ says about this Nextcloud, or nothing at all when it can't answer."""
    try:
        return _json(occ(config, tenant, "status", "--output=json"))
    except CtlError:
        return {}


def installed(config: Config, tenant: Tenant) -> bool:
    return bool(status(config, tenant).get("installed"))


def install(config: Config, tenant: Tenant, database: str, db_password: str, admin: str, password: str) -> None:
    """Sets a fresh Nextcloud up in its database, with the first administrator.

    The two passwords are given as options, the way Nextcloud's own documentation does it: occ has no way to be
    handed them that doesn't show them, and both are in its settings file a moment later anyway.
    """
    occ(config, tenant, "maintenance:install", "--database=mysql", f"--database-name={database}",
        f"--database-host={DB_HOST}", f"--database-user={database}", f"--database-pass={db_password}",
        f"--admin-user={admin}", f"--admin-pass={password}", f"--data-dir={config.data_of(tenant.user)}")


# --- Its settings --------------------------------------------------------------------------------------------

def settings(config: Config, tenant: Tenant) -> list[Setting]:
    """The settings officectl keeps: where this Nextcloud is reached, and what it caches with.

    Every Nextcloud on this server may share one Redis database, because the keys it writes all start with its
    own instance id.
    """
    return [
        Setting(("trusted_domains", "0"), tenant.host),
        Setting(("overwrite.cli.url",), tenant.url),
        # Behind OpenLiteSpeed, PHP sees a plain HTTP request; without this Nextcloud builds the addresses in
        # its pages with http:// in them.
        Setting(("overwriteprotocol",), "https"),
        Setting(("memcache.local",), "\\OC\\Memcache\\APCu"),
        Setting(("memcache.distributed",), "\\OC\\Memcache\\Redis"),
        # The lock that keeps two requests from writing one file at once. It has to be somewhere both see, so
        # it is Redis and not APCu.
        Setting(("memcache.locking",), "\\OC\\Memcache\\Redis"),
        Setting(("redis", "host"), config.redis_host),
        Setting(("redis", "port"), str(config.redis_port), "integer"),
        Setting(("redis", "dbindex"), str(config.redis_database), "integer"),
        Setting(("default_phone_region",), config.phone_region),
        # Nextcloud does its nightly work in the four hours after this one, in UTC.
        Setting(("maintenance_window_start",), "1", "integer"),
    ]


def mail_settings(config: Config) -> list[Setting]:
    """How a new Nextcloud sends its mail: over SMTP to this server's own Postfix. Only set when it is
    installed, so it can be changed in Nextcloud itself afterwards."""
    sender = config.sender()
    return [
        Setting(("mail_smtpmode",), "smtp"),
        Setting(("mail_smtphost",), config.mail_host),
        Setting(("mail_smtpport",), str(config.mail_port), "integer"),
        Setting(("mail_from_address",), sender.partition("@")[0]),
        Setting(("mail_domain",), sender.partition("@")[2]),
    ]


def current_settings(config: Config, tenant: Tenant) -> dict:
    """Every setting this Nextcloud has now."""
    return _json(occ(config, tenant, "config:list", "system", "--private", "--output=json")).get("system", {})


def settings_to_change(wanted: list[Setting], current: dict) -> list[Setting]:
    """The settings that aren't what they should be, so a run that changes nothing says so."""
    return [setting for setting in wanted if _value_at(current, setting.keys) != setting.typed]


def apply_settings(config: Config, tenant: Tenant, to_change: list[Setting]) -> None:
    for setting in to_change:
        occ(config, tenant, "config:system:set", *setting.keys, f"--type={setting.kind}",
            f"--value={setting.value}")


# --- Collabora in Nextcloud ----------------------------------------------------------------------------------

def apps(config: Config, tenant: Tenant) -> dict[str, str]:
    """Every app of this Nextcloud, with "enabled" or "disabled" against it."""
    found = _json(occ(config, tenant, "app:list", "--output=json"))
    return {**{name: "disabled" for name in found.get("disabled", {})},
            **{name: "enabled" for name in found.get("enabled", {})}}


def add_app(config: Config, tenant: Tenant, name: str, present: dict[str, str]) -> bool:
    """Installs the app, or switches on one that is there but off. Returns whether anything changed."""
    if present.get(name) == "enabled":
        return False
    occ(config, tenant, "app:enable" if name in present else "app:install", name)
    return True


def collabora_settings(config: Config, addresses: list[str]) -> dict[str, str]:
    """What the app needs to know: where Collabora is, and which addresses may call back into Nextcloud as
    Collabora. Collabora reaches it over the internet, so its own public address is one of them."""
    return {
        "wopi_url": f"https://{config.office_host}",
        "public_wopi_url": f"https://{config.office_host}",
        "wopi_allowlist": ",".join(addresses),
    }


def app_settings(config: Config, tenant: Tenant, name: str) -> dict[str, str]:
    found = _json(occ(config, tenant, "config:list", name, "--private", "--output=json"))
    return found.get("apps", {}).get(name, {})


def set_app_settings(config: Config, tenant: Tenant, name: str, wanted: dict[str, str]) -> bool:
    """Brings the app's settings up to date and tells Collabora's editor to read them again. Returns whether
    anything changed."""
    present = app_settings(config, tenant, name)
    to_change = {key: value for key, value in wanted.items() if present.get(key) != value}
    for key, value in to_change.items():
        occ(config, tenant, "config:app:set", name, key, f"--value={value}")
    if to_change:
        # It asks Collabora what it can do and keeps the answer; without this the editor stays as it was.
        occ(config, tenant, f"{name}:activate-config")
    return bool(to_change)


# --- Its background jobs -------------------------------------------------------------------------------------

def cron_path(config: Config, tenant: Tenant) -> Path:
    return config.cron_dir / f"nextcloud-{tenant.user}"


def cron(config: Config, tenant: Tenant) -> str:
    """The cron file of this Nextcloud's background jobs. Nextcloud expects them every five minutes: it takes
    the time of the last run as a sign that it is alive, and says so on its own settings page when it isn't."""
    user = tenant.user
    return f"""# Written by officectl. Changes are overwritten.
# The background jobs of the Nextcloud at {tenant.host}, on its own PHP and its own settings, as the user its
# pages run as.
PHP_INI_SCAN_DIR={php.scan_dir(config, user)}
*/{config.cron_minutes} * * * * {config.web_user} {config.php()} -f {config.docroot_of(user)}/cron.php >/dev/null 2>&1
"""


def last_cron(config: Config, tenant: Tenant) -> int:
    """When the background jobs last ran, as a number of seconds since 1970, or 0 when they never have."""
    try:
        return int(occ(config, tenant, "config:app:get", "core", "lastcron").strip())
    except (CtlError, ValueError):
        return 0


def write_cron(config: Config, tenant: Tenant) -> bool:
    return files.replace(cron_path(config, tenant), cron(config, tenant))


def remove_cron(config: Config, tenant: Tenant) -> bool:
    return files.remove(cron_path(config, tenant))


# --- The files themselves ------------------------------------------------------------------------------------

def put_code_in_place(config: Config, tenant: Tenant) -> str:
    """Fetches Nextcloud and puts it in the web root, next to what is already there. Returns the version.

    For a Nextcloud that has none yet: the web root holds nothing but the challenge folder, and the files are
    simply added to it.
    """
    with _fetched(config, tenant) as tree:
        shutil.copytree(tree, config.docroot_of(tenant.user), dirs_exist_ok=True)
    return version(config, tenant.user)


def replace_code(config: Config, tenant: Tenant) -> tuple[str, Path | None]:
    """Puts a newer Nextcloud in the place of the one that is there. Returns the version downloaded, and where
    the old files were kept -- or nothing, when that version is the one already in place.

    Nextcloud's own updater rewrites the files it runs from, which it can't here: the code is root's and
    Nextcloud may only read it. So the new release is laid out beside the old one with this Nextcloud's own
    settings and its own apps in it, and then the two are swapped. The old files stay behind, so a release that
    turns out to be wrong can be put back by hand.
    """
    docroot = config.docroot_of(tenant.user)
    with _fetched(config, tenant) as tree:
        release = version_in(tree)
        if release == version(config, tenant.user):
            return release, None  # the newest release is the one already in place, so nothing is swapped
        _carry_over(docroot, tree)
        kept = config.home(tenant.user) / f"previous-{version(config, tenant.user) or 'nextcloud'}"
        system.remove_tree(kept)
        docroot.rename(kept)
        try:
            tree.rename(docroot)
        except OSError as error:
            kept.rename(docroot)  # the old Nextcloud goes back: a server without one is worse than an old one
            raise CtlError(f"Can't put the new Nextcloud in {docroot}: {error.strerror}.") from None
    layout.create(config, tenant.user)
    return release, kept


def needs_upgrade(config: Config, tenant: Tenant) -> bool:
    """Whether the files are newer than what is in the database, so Nextcloud won't serve a page until occ has
    brought the database up to date."""
    found = status(config, tenant)
    return bool(found.get("needsDbUpgrade") or found.get("maintenance"))


def upgrade(config: Config, tenant: Tenant) -> None:
    """Brings the database up to the version the files are, and switches the maintenance page off again."""
    occ(config, tenant, "upgrade")
    occ(config, tenant, "maintenance:mode", "--off")
    # A release may bring indices and columns of its own, which the upgrade leaves to be added while it runs.
    for missing in ("db:add-missing-columns", "db:add-missing-indices", "db:add-missing-primary-keys"):
        occ(config, tenant, missing)


def _carry_over(docroot: Path, tree: Path) -> None:
    """Puts this Nextcloud's own settings and its own apps in the new release, so the swap keeps them."""
    for settings_file in sorted((docroot / CONFIG).glob("*.php")):
        if settings_file.name != "config.sample.php":
            shutil.copy2(settings_file, tree / CONFIG / settings_file.name)
    for app in sorted((docroot / APPS).iterdir()) if (docroot / APPS).is_dir() else []:
        # An app that the release brings itself is the release's own; one that isn't in it was installed here.
        if app.is_dir() and not (tree / APPS / app.name).exists():
            shutil.copytree(app, tree / APPS / app.name)
    # Let's Encrypt's challenge folder is served from the web root, so it goes along.
    for extra in (".well-known",):
        if (docroot / extra).is_dir():
            shutil.copytree(docroot / extra, tree / extra, dirs_exist_ok=True)


class _fetched:
    """Nextcloud's files, unpacked in a directory of their own next to the web root, cleaned up afterwards.

    Next to the web root, and not in /tmp: it is the same filesystem, so putting a release in place is a rename
    and not a copy of a few hundred megabytes.
    """

    def __init__(self, config: Config, tenant: Tenant) -> None:
        self._config = config
        self._home = config.home(tenant.user)

    def __enter__(self) -> Path:
        self._workspace = Path(tempfile.mkdtemp(dir=self._home, prefix=".nextcloud-"))
        try:
            return _unpack(_download(self._config.nextcloud_url, self._workspace), self._workspace)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, kind, value, traceback) -> bool:
        system.remove_tree(self._workspace)
        return False


def _download(url: str, into: Path) -> Path:
    """The archive, checked against the checksum published next to it: a download that was cut short or
    tampered with is not unpacked."""
    archive = into / url.rsplit("/", 1)[-1]
    _fetch(url, archive)
    published = _fetch_text(f"{url}.sha256").split()
    if not published:
        raise CtlError(f"{url}.sha256 holds no checksum.")
    found = _sha256(archive)
    if found != published[0]:
        raise CtlError(f"The download from {url} isn't what it should be: its checksum is {found} and "
                       f"Nextcloud publishes {published[0]}.",
                       hint="Try again; if it happens twice, something is interfering with the download.")
    return archive


def _unpack(archive: Path, into: Path) -> Path:
    """Unpacks the archive and returns the directory Nextcloud is in. Nothing is let out of that directory: the
    archive is read with Python's own "data" rules, which refuse a name or a link that points outside it."""
    try:
        with tarfile.open(archive) as unpacked:
            unpacked.extractall(into, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise CtlError(f"Can't unpack {archive.name}: {error}.") from None
    tree = into / ARCHIVE_DIRECTORY
    if not (tree / "occ").is_file():
        raise CtlError(f"{archive.name} doesn't hold a Nextcloud: there is no {ARCHIVE_DIRECTORY}/occ in it.")
    archive.unlink()
    return tree


def _fetch(url: str, path: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as answer, path.open("wb") as file:
            shutil.copyfileobj(answer, file)
    except (OSError, urllib.error.URLError) as error:
        raise CtlError(f"Can't download {url}: {getattr(error, 'reason', error)}.") from None


def _fetch_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as answer:
            return answer.read(4096).decode(errors="replace")
    except (OSError, urllib.error.URLError) as error:
        raise CtlError(f"Can't download {url}: {getattr(error, 'reason', error)}.") from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(text: str) -> dict:
    """The JSON in what occ printed. A warning or a notice in front of it is stepped over, since one line of
    PHP's own output would otherwise make everything unreadable."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        found = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _value_at(values: dict, keys: tuple[str, ...]):
    """The value these keys lead to, or None when the path isn't there.

    A number is a place in a list as well as a key in an object: occ takes "trusted_domains 0" for the first of
    the trusted domains, and gives that setting back as a list.
    """
    found = values
    for key in keys:
        if isinstance(found, dict) and key in found:
            found = found[key]
        elif isinstance(found, list) and key.isdigit() and int(key) < len(found):
            found = found[int(key)]
        else:
            return None
    return found
