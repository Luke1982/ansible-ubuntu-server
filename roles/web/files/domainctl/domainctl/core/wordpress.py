"""WordPress on a site: its database, wp-cli run as the site's user, and the WP fail2ban plugin.

wp-cli always runs as the site's own user, never as root: WordPress's files stay that user's, and a plugin on the
site can't do anything its user couldn't. OpenLiteSpeed only reads the site, so the one directory WordPress writes
in, its uploads, is opened up to it.

Each site gets a database and a MariaDB user named after its Linux user, reached over the local socket. Root
makes them, logging in to MariaDB through its own socket as the server's root does.

WP fail2ban sends failed logins to syslog with the site's name in the tag, so the jails in
/etc/fail2ban/jail.d/wordpress.conf cover every site that has it active. See roles/web/files/jail-wordpress.conf.
"""

import json
import re
import secrets
from enum import Enum
from pathlib import Path
from typing import IO

from serverctl import system
from serverctl.errors import CtlError

from ..config import Config
from . import acl

WP = "/usr/local/bin/wp"
PLUGIN = "wp-fail2ban"
# The socket named explicitly: OpenLiteSpeed's lsphp may be built to look for it elsewhere than the CLI PHP does.
DB_HOST = "localhost:/run/mysqld/mysqld.sock"
# What a site's own files may be next to, in a web root WordPress is put in.
PLACEHOLDER_FILES = {"index.html", ".well-known"}


class Plugin(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MISSING = "missing"


def is_wordpress(config: Config, user: str) -> bool:
    return (config.docroot_of(user) / "wp-includes" / "version.php").is_file()


def is_configured(config: Config, user: str) -> bool:
    return (config.docroot_of(user) / "wp-config.php").is_file()


def foreign_files(config: Config, user: str) -> list[str]:
    """What is in the web root besides domainctl's own placeholder and challenge folder, when it isn't WordPress."""
    docroot = config.docroot_of(user)
    if is_wordpress(config, user) or not docroot.is_dir():
        return []
    return sorted(path.name for path in docroot.iterdir() if path.name not in PLACEHOLDER_FILES)


def wp(config: Config, user: str, *args: str, stdin: str | None = None) -> str:
    """wp-cli in the site's web root, as its user. runuser keeps root's environment and directory, so HOME is set
    for wp-cli's cache and the user starts in its own home. Secrets go on stdin, for wp-cli's --prompt, and never
    on the command line."""
    return system.run("runuser", "-u", user, "--", "env", f"--chdir={config.home(user)}",
                      f"HOME={config.home(user)}", WP,
                      f"--path={config.docroot_of(user)}", *args, stdin=stdin)


def download(config: Config, user: str, locale: str) -> None:
    wp(config, user, "core", "download", f"--locale={locale}")


def configure(config: Config, user: str, database: str, password: str) -> None:
    # No check: wp-cli checks with the mysql client, which reads DB_HOST differently from PHP.
    wp(config, user, "config", "create", f"--dbname={database}", f"--dbuser={database}", f"--dbhost={DB_HOST}",
       "--dbcharset=utf8mb4", "--skip-check", "--prompt=dbpass", stdin=f"{password}\n")


# A define() on a line of its own, in either quote style and with any spacing, like move-wordpress.sh finds them.
_DEFINE = re.compile(r"""^[ \t]*define[ \t]*\([ \t]*(['"])(DB_NAME|DB_USER|DB_PASSWORD|DB_HOST)\1[ \t]*,.*?(\r?)$""",
                     re.MULTILINE)


def _php_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _rewritten_config(config: Config, user: str, database: str, db_user: str, password: str) -> tuple[Path, str]:
    path = config.docroot_of(user) / "wp-config.php"
    try:
        text = path.read_text()
    except OSError as problem:
        raise CtlError(f"Can't read {path}: {problem.strerror or problem}.") from None
    values = {"DB_NAME": database, "DB_USER": db_user, "DB_PASSWORD": password, "DB_HOST": DB_HOST}
    found: set[str] = set()

    def replace(match: re.Match) -> str:
        found.add(match[2])
        return f"define( '{match[2]}', {_php_string(values[match[2]])} );{match[3]}"

    changed = _DEFINE.sub(replace, text)
    # DB_HOST may be left out: WordPress then uses localhost, which is this server.
    missing = [key for key in ("DB_NAME", "DB_USER", "DB_PASSWORD") if key not in found]
    if missing:
        raise CtlError(f"{path} has no line for {', '.join(missing)}.",
                       hint="Check that it is the site's own wp-config.php, with a define() for each database setting.")
    return path, changed


def check_config(config: Config, user: str) -> None:
    """Fails the way adopt_config would, without changing anything: so it can be known before a database is touched."""
    _rewritten_config(config, user, "", "", "")


def adopt_config(config: Config, user: str, database: str, db_user: str, password: str) -> None:
    """Points the wp-config.php that came with the site at its database here. Only the four database lines change:
    the salts, the table prefix and the site's own constants are what make it that site."""
    path, changed = _rewritten_config(config, user, database, db_user, password)
    path.write_text(changed)


def import_dump(database: str, dump: IO[bytes]) -> None:
    """Reads a dump into the database as bytes, so a big one isn't held in memory and nothing in it is re-encoded."""
    system.run_fed("mariadb", database, source=dump)


def is_installed(config: Config, user: str) -> bool:
    try:
        wp(config, user, "core", "is-installed")
    except CtlError:
        return False
    return True


def install(config: Config, user: str, url: str, title: str, admin: str, email: str, password: str) -> None:
    wp(config, user, "core", "install", f"--url={url}", f"--title={title}", f"--admin_user={admin}",
       f"--admin_email={email}", "--skip-email", "--prompt=admin_password", stdin=f"{password}\n")


def plugin_status(config: Config, user: str) -> Plugin:
    """Whether WP fail2ban is active. The site's own plugins and theme aren't loaded to find out, so a broken one
    doesn't stand in the way; wp-cli reads the list of active plugins from the database."""
    found = json.loads(wp(config, user, "plugin", "list", "--fields=name,status", "--format=json",
                          "--skip-plugins", "--skip-themes") or "[]")
    status = next((plugin["status"] for plugin in found if plugin.get("name") == PLUGIN), None)
    if status is None:
        return Plugin.MISSING
    # Network-wide on a multisite, or as a must-use plugin, it logs just the same.
    return Plugin.ACTIVE if status in ("active", "active-network", "must-use") else Plugin.INACTIVE


def add_plugin(config: Config, user: str, status: Plugin) -> None:
    """Installs WP fail2ban from wordpress.org when it isn't there, and activates it."""
    if status is Plugin.MISSING:
        wp(config, user, "plugin", "install", PLUGIN, "--activate", "--skip-plugins", "--skip-themes")
    elif status is Plugin.INACTIVE:
        wp(config, user, "plugin", "activate", PLUGIN, "--skip-plugins", "--skip-themes")


def open_uploads(config: Config, user: str) -> bool:
    """Lets OpenLiteSpeed write WordPress's uploads, as the site's user. Returns whether anything changed."""
    uploads = config.docroot_of(user) / "wp-content" / "uploads"
    if not uploads.is_dir():
        system.run("runuser", "-u", user, "--", "mkdir", "-p", str(uploads))
    return acl.apply(uploads, acl.shared_with(config.web_user, user))


def new_password() -> str:
    return secrets.token_urlsafe(24)


# --- The database -------------------------------------------------------------------------------------------------

def _sql(statements: str) -> str:
    return system.run("mariadb", "--batch", "--skip-column-names", stdin=statements)


def _string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _name(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def database_taken(name: str) -> str | None:
    """What already has the name in MariaDB, a database or a user, or None when both are free."""
    counts = _sql(f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = {_string(name)};\n"
                  f"SELECT COUNT(*) FROM mysql.user WHERE User = {_string(name)};\n").split()
    if counts and counts[0] != "0":
        return f"a database {name}"
    if len(counts) > 1 and counts[1] != "0":
        return f"a MariaDB user {name}"
    return None


def user_taken(name: str) -> bool:
    """Whether MariaDB already has this user."""
    found = _sql(f"SELECT COUNT(*) FROM mysql.user WHERE User = {_string(name)};\n").split()
    return bool(found) and found[0] != "0"


def create_database(name: str, password: str, db_user: str | None = None) -> None:
    """A database with a user that may only use it, only over the local socket. The user has the database's own
    name unless another is given: a site that came from another server keeps the user name its settings hold,
    so its own configuration still fits.

    A user that is already there keeps its password and whatever else it may use: one customer with two sites
    logs into both with one name, and taking that name away from the first to give it to the second would stop
    the first working.
    """
    user = f"{_string(db_user or name)}@'localhost'"
    _sql(f"CREATE DATABASE {_name(name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
         f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {_string(password)};\n"
         f"GRANT ALL PRIVILEGES ON {_name(name)}.* TO {user};\n")


def drop_database(name: str, db_user: str | None = None) -> None:
    """The database and the user of the same name. A user of another name is only taken off this database:
    it may be the login of other sites of the same customer, and nothing here may break those."""
    # Only the database and the user that shares its name. A user of another name is left alone altogether:
    # it may be the login of other sites of this customer, and the grant it has on this database is the one
    # that is handed out again the moment the database is made anew.
    _sql(f"DROP DATABASE IF EXISTS {_name(name)};\nDROP USER IF EXISTS {_string(name)}@'localhost';\n")
