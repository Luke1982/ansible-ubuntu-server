"""domainctl database: a MariaDB database for a site that is not WordPress.

wp adopt does this for a WordPress site and writes the password into its wp-config.php. A site that is not
WordPress keeps its settings somewhere only the person who built it knows, so this makes the database and the
user and then says the password out loud, once: it is the only time anybody can read it.
"""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from serverctl import names, ui
from serverctl.errors import CtlError

from ..core import layout, sites, users, wordpress
from ..session import open_session
from .shared import User, Yes, ask_user, find_site, group

app = group("Databases for sites that are not WordPress: make one, with a user of its own, and read a dump in.")

Database = Annotated[Optional[str], typer.Option(
    "--database", help="The database's name, which the MariaDB user shares. The site's name when left out.",
    show_default=False)]
Dump = Annotated[Optional[str], typer.Option(
    "--dump", help="A database dump to read in. Use - for standard input. Nothing is read in when left out.",
    show_default=False)]
Password = Annotated[Optional[str], typer.Option(
    "--password", help="The database user's password. One is made up when left out.", show_default=False)]
DbUser = Annotated[Optional[str], typer.Option(
    "--db-user", help="The MariaDB user to make for it. The database's own name when left out. Give the name "
                      "the site's settings already hold, and its own configuration still fits.",
    show_default=False)]
Replace = Annotated[bool, typer.Option(
    "--replace", help="Empty and use a database that is already there, instead of stopping.")]
Keep = Annotated[bool, typer.Option(
    "--keep-dump", help="Leave the dump where it is. Without this, a dump inside the site's web root is moved "
                        "out of it: everything there can be downloaded by anyone.")]


@app.command()
def add(user: User = None, database: Database = None, dump: Dump = None, password: Password = None,
        db_user: DbUser = None, replace: Replace = False, keep_dump: Keep = False, yes: Yes = False) -> None:
    """Make a database and a MariaDB user of the same name for a site, and read a dump into it.

    The site's own settings are not touched: this says the password, and you put it where that site keeps its
    database settings. It is not stored anywhere else, so write it down before the screen scrolls.

    A dump that lies inside the site's web root is moved into the site's home directory afterwards, where the web
    server does not serve it, and --keep-dump leaves it where it is.

    [dim]Example:[/] domainctl database add example --dump /home/example/public_html/www/example.sql
    [dim]From the old server:[/] ssh old mysqldump example | domainctl database add example --dump -
    """
    with open_session() as session:
        config = session.config
        owner = _site_user(config, ask_user(user))
        name = names.user_name(database or owner)
        account = names.user_name(db_user) if db_user else name
        path = _dump_path(dump)
        # Only the database decides whether anything is replaced. A MariaDB user of another name may be the
        # login of this customer's other sites, and that one is never emptied or taken away.
        taken = wordpress.database_taken(name) if account == name else _database_here(name)
        shared = account != name and wordpress.user_taken(account)
        if taken and not replace:
            raise CtlError(f"There is already {taken} in MariaDB.",
                           hint="Pick another name with --database, or empty it and use it with --replace.")
        if taken:
            ui.confirm(f"{taken[0].upper()}{taken[1:]} will be emptied and removed, with everything in it. Go on?",
                       yes)
            wordpress.drop_database(name, account)
        secret = password or wordpress.new_password()
        wordpress.create_database(name, secret, account)
        if dump is not None:
            try:
                with ui.console.status("Reading the dump in…"):
                    _read_dump(name, dump)
            except CtlError:
                wordpress.drop_database(name, account)  # half a database is worse than none
                raise
        made = "Replaced" if taken else "Made"
        if account == name:
            ui.success(f"{made} the database {name}, with a MariaDB user of the same name.")
        elif shared:
            ui.success(f"{made} the database {name}, and let the MariaDB user {account} use it. That user was "
                       f"already here, so its password is the one it had.")
        else:
            ui.success(f"{made} the database {name}, with the MariaDB user {account}.")
        if dump is not None:
            ui.success("Read the dump into it.")
        moved = None if keep_dump else _out_of_the_web_root(config, owner, path)
        if moved:
            ui.note(f"Moved the dump to {moved}: inside the web root, anybody could have downloaded it.")
        ui.console.print(_settings(name, account, secret))
        if password:
            ui.note("Those are the ones you gave, so the site's own settings already fit them.")
        else:
            ui.note("Put these in the site's own settings file. The password is not stored anywhere else and "
                    "this is the only time it is shown; a new one needs this command again with --replace.")


def _database_here(name: str) -> str | None:
    """What has this database name, ignoring a MariaDB user that happens to share it."""
    found = wordpress.database_taken(name)
    return found if found and found.startswith("a database") else None


def _site_user(config, user: str) -> str:
    """The site's Linux user. A site whose domain still points at the old server has its user and its directories
    but no virtual host yet, and that is the state it is in while its files and database are moved over."""
    if sites.find(config, user):
        return user
    if users.exists(user) and layout.made_by_domainctl(config, user):
        ui.note(f"The virtual host of {user} is still waiting for the DNS; its files and database can be put in "
                f"place meanwhile.")
        return user
    find_site(config, user)  # says there is no such site, and which there are
    return user


def _dump_path(dump: str | None) -> Path | None:
    if dump in (None, "-"):
        return None
    path = Path(dump)
    if not path.is_file():
        raise CtlError(f"There is no dump at {dump}.")
    return path


def _read_dump(database: str, dump: str) -> None:
    if dump == "-":
        wordpress.import_dump(database, sys.stdin.buffer)
        return
    with open(dump, "rb") as file:
        wordpress.import_dump(database, file)


def _out_of_the_web_root(config, user: str, path: Path | None) -> Path | None:
    """A dump that lies in the web root goes into the site's home directory, where nothing serves it. Returns
    where it went, or None when it was never in the web root."""
    if path is None:
        return None
    docroot = config.docroot_of(user)
    try:
        path.relative_to(docroot)
    except ValueError:
        return None
    target = users.home_of(user) / path.name
    path.replace(target)
    target.chmod(0o600)
    return target


def _settings(database: str, account: str, password: str) -> str:
    return (f"\n  database  {database}\n  user      {account}\n  password  {password}\n"
            f"  host      localhost\n")
