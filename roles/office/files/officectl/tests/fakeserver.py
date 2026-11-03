"""A pretend server: what occ, MariaDB, systemctl and Nextcloud's download server would do.

Nothing here runs as root, reaches the network or touches a database. Every command officectl runs comes
through run(), which answers the way the real program would and remembers that it was asked.
"""

import json
from contextlib import contextmanager
from pathlib import Path

from officectl.core import nextcloud
from serverctl.errors import CtlError

VERSION = "32.0.1"


class FakeServer:
    def __init__(self, config):
        self.config = config
        self.databases: set[str] = set()
        self.db_users: set[str] = set()
        self.installed: set[str] = set()  # the Linux users whose Nextcloud is set up
        self.system: dict[str, dict] = {}  # per user: Nextcloud's own settings
        self.apps: dict[str, dict[str, str]] = {}  # per user: app, enabled or disabled
        self.app_settings: dict[str, dict[str, dict[str, str]]] = {}
        self.maintenance: set[str] = set()
        self.upgraded: list[str] = []
        self.last_cron: dict[str, int] = {}
        self.service_state = "active"
        self.calls: list[tuple[str, ...]] = []
        self.occ_calls: list[tuple[str, ...]] = []
        self.failing: str | None = None  # an occ command that fails, to see what officectl does then

    # --- what system.run() answers ---------------------------------------------------------------------------

    def run(self, *args, stdin=None):
        self.calls.append(args)
        if args[0] == "mariadb":
            return self._sql(stdin or "")
        if args[0] == "systemctl":
            return self.service_state
        if args[0] == "coolwsd":
            return "coolwsd 26.04.5.2\n"
        if args[0] == "runuser":
            return self._occ(args)
        return ""

    def run_fed(self, *args, source):
        self.calls.append(args)
        return None

    # --- occ ------------------------------------------------------------------------------------------------

    def _occ(self, args):
        user = self._user_of(args)
        command = tuple(word for word in args[args.index("occ") + 1:] if word != "--no-warnings")
        self.occ_calls.append((user, *command))
        if self.failing and self.failing in command:
            raise CtlError(f"occ {' '.join(command)} failed")
        return self._answer(user, command)

    def _answer(self, user, command):
        settings = self.system.setdefault(user, {})
        match command:
            case ("status", *_):
                return json.dumps({"installed": user in self.installed, "version": f"{VERSION}.1",
                                   "versionstring": VERSION, "maintenance": user in self.maintenance,
                                   "needsDbUpgrade": False})
            case ("maintenance:install", *options):
                self.installed.add(user)
                self.system.setdefault(user, {})["dbname"] = _option(options, "--database-name")
                return ""
            case ("config:system:set", *rest):
                keys = [word for word in rest if not word.startswith("--")]
                _put(settings, keys, _typed(_option(rest, "--value"), _option(rest, "--type")))
                return ""
            case ("config:list", "system", *_):
                return json.dumps({"system": settings})
            case ("config:list", app, *_):
                return json.dumps({"apps": {app: self.app_settings.get(user, {}).get(app, {})}})
            case ("app:list", *_):
                present = self.apps.get(user, {})
                return json.dumps({"enabled": {name: VERSION for name, state in present.items()
                                               if state == "enabled"},
                                   "disabled": {name: VERSION for name, state in present.items()
                                                if state != "enabled"}})
            case ("app:install" | "app:enable", name):
                self.apps.setdefault(user, {})[name] = "enabled"
                return ""
            case ("config:app:set", app, key, *rest):
                self.app_settings.setdefault(user, {}).setdefault(app, {})[key] = _option(rest, "--value")
                return ""
            case ("config:app:get", "core", "lastcron"):
                return f"{self.last_cron.get(user, 0)}\n"
            case ("maintenance:mode", flag):
                self.maintenance.add(user) if flag == "--on" else self.maintenance.discard(user)
                return ""
            case ("upgrade",):
                self.upgraded.append(user)
                return ""
        return ""

    def _user_of(self, args):
        """Which Nextcloud this occ is for: the directory it runs in is inside that user's home."""
        where = next(word for word in args if word.startswith("--chdir="))
        return Path(where.partition("=")[2]).relative_to(self.config.home_root).parts[0]

    # --- MariaDB --------------------------------------------------------------------------------------------

    def _sql(self, statements):
        if statements.startswith("SELECT"):
            name = statements.split("'")[1]
            if "mysql.user" in statements and "SCHEMATA" not in statements:
                return f"{int(name in self.db_users)}\n"
            return f"{int(name in self.databases)}\n{int(name in self.db_users)}\n"
        for line in statements.splitlines():
            if line.startswith("CREATE DATABASE"):
                self.databases.add(line.split("`")[1])
            if line.startswith("DROP DATABASE"):
                self.databases.discard(line.split("`")[1])
            if line.startswith("CREATE USER"):
                self.db_users.add(line.split("'")[1])
            if line.startswith("DROP USER"):
                self.db_users.discard(line.split("'")[1])
        return ""


def _option(words, name):
    return next((word.partition("=")[2] for word in words if word.startswith(f"{name}=")), "")


def _typed(value, kind):
    if kind == "integer":
        return int(value)
    if kind == "boolean":
        return value == "true"
    return value


def _put(settings, keys, value):
    """Puts the value where occ would put it: under a number, in a list."""
    if len(keys) == 1:
        settings[keys[0]] = value
        return
    first, rest = keys[0], keys[1:]
    if rest[0].isdigit():
        holder = settings.setdefault(first, [])
        while len(holder) <= int(rest[0]):
            holder.append(None)
        holder[int(rest[0])] = value
        return
    _put(settings.setdefault(first, {}), rest, value)


def release(where: Path, version: str = VERSION) -> Path:
    """A Nextcloud release, unpacked: what the download would have left behind."""
    tree = where / f"release-{version}"
    (tree / "config").mkdir(parents=True, exist_ok=True)
    (tree / "apps" / "files").mkdir(parents=True, exist_ok=True)
    (tree / "occ").write_text("<?php // occ\n")
    (tree / "index.php").write_text("<?php\n")
    (tree / "version.php").write_text(f"<?php\n$OC_VersionString = '{version}';\n")
    (tree / "config" / "config.sample.php").write_text("<?php\n")
    (tree / "apps" / "files" / "appinfo.xml").write_text("<info/>\n")
    return tree


def downloads(monkeypatch, tree: Path) -> None:
    """Hands officectl that release instead of downloading one."""
    @contextmanager
    def fetched(config, tenant):
        yield tree

    monkeypatch.setattr(nextcloud, "_fetched", fetched)
