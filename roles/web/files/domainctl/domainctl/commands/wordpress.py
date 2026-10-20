"""domainctl wp: WordPress on a site, installed with wp-cli, and the WP fail2ban plugin on every WordPress site."""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.text import Text

from serverctl import names, ui
from serverctl.dns import Status
from serverctl.errors import CtlError

from ..core import certificates, layout, sites, users, wordpress
from ..core.wordpress import Plugin
from ..session import open_session
from .shared import User, UserFilter, Yes, ask_user, find_site, group

app = group("WordPress on the sites: install it, and keep the WP fail2ban plugin on every one.")

Title = Annotated[Optional[str], typer.Option(
    "--title", help="The site's title. The domain when left out.", show_default=False)]
AdminUser = Annotated[Optional[str], typer.Option(
    "--admin-user", help="The name of WordPress's first administrator. Asked for when left out.", show_default=False)]
AdminEmail = Annotated[Optional[str], typer.Option(
    "--admin-email", help="That administrator's e-mail address. Asked for when left out.", show_default=False)]
Locale = Annotated[str, typer.Option("--locale", help="WordPress's language, like nl_NL.")]
Fix = Annotated[bool, typer.Option(
    "--fix", help="Install and activate WP fail2ban on the WordPress sites that don't have it active.")]

Database = Annotated[Optional[str], typer.Option(
    "--database", help="The database's name, which the MariaDB user shares. The site's name when left out.",
    show_default=False)]
Dump = Annotated[Optional[str], typer.Option(
    "--dump", help="The database dump to read in. Use - for standard input. Nothing is read in when left out.",
    show_default=False)]
Replace = Annotated[bool, typer.Option(
    "--replace", help="Empty and use a database that is already there, instead of stopping.")]

_STATUS = {Plugin.ACTIVE: (Status.OK, "active"), Plugin.INACTIVE: (Status.FAIL, "installed, not active"),
           Plugin.MISSING: (Status.FAIL, "not installed")}


@app.command()
def install(user: User = None, title: Title = None, admin_user: AdminUser = None, admin_email: AdminEmail = None,
            locale: Locale = "en_US") -> None:
    """Install WordPress on a site, with its own database and the WP fail2ban plugin.

    WordPress goes in the site's web root, which may hold nothing but domainctl's placeholder page. It gets a
    MariaDB database and user named after the site, with a password only wp-config.php knows. The administrator's
    password is made up and shown once, at the end.

    WP fail2ban sends every failed login to the server's log, where fail2ban bans the address it came from.
    OpenLiteSpeed may write in wp-content/uploads and nowhere else, so WordPress can't update itself or install
    plugins from its dashboard: do that with wp-cli, as the site's user.

    Run it again when it stopped halfway: it picks up where it left off.

    [dim]Example:[/] domainctl wp install example --admin-user jan --admin-email jan@example.nl
    [dim]In Dutch:[/] domainctl wp install example --locale nl_NL
    """
    with open_session() as session:
        config = session.config
        site = find_site(config, ask_user(user))
        docroot = config.docroot_of(site.user)
        foreign = wordpress.foreign_files(config, site.user)
        if foreign:
            shown = ", ".join(foreign[:5]) + (", …" if len(foreign) > 5 else "")
            raise CtlError(f"{docroot} already holds files that aren't WordPress: {shown}.",
                           hint="WordPress goes in an empty web root. Move them aside and run this again.")
        # Asked before anything changes, so a missing answer doesn't leave half a site behind.
        installed = wordpress.is_configured(config, site.user) and wordpress.is_installed(config, site.user)
        if not installed:
            admin = names.user_name(ui.ask("WordPress administrator", admin_user, "--admin-user"))
            email = names.address(ui.ask("The administrator's e-mail address", admin_email, "--admin-email"))

        if not wordpress.is_wordpress(config, site.user):
            with ui.console.status("Downloading WordPress…"):
                wordpress.download(config, site.user, locale)
            ui.success(f"Downloaded WordPress into {docroot}.")
        if not wordpress.is_configured(config, site.user):
            _make_database(config, site.user)
        if not installed:
            password = wordpress.new_password()
            with ui.console.status("Installing WordPress…"):
                wordpress.install(config, site.user, f"https://{site.domain}", title or site.domain, admin, email,
                                  password)
            ui.success(f"Installed WordPress for https://{site.domain}.")
        _secure(config, site.user)
        if wordpress.open_uploads(config, site.user):
            ui.success(f"{config.web_user} may write in {docroot / 'wp-content' / 'uploads'}.")
        if layout.remove_placeholder(config, site.user):
            ui.success("Took the placeholder page away.")
        if not certificates.exists(config, site.user):
            ui.warn(f"{site.domain} has no certificate yet, and WordPress is set up for https://{site.domain}.")
            ui.note(f"Get one with: domainctl repair {site.domain}")
        if not installed:
            ui.heading(f"Log in at https://{site.domain}/wp-login.php")
            ui.line(f"User:     {admin}", indent=1)
            ui.line(f"Password: {password}", indent=1)
            ui.note("The password isn't stored anywhere else. Change it after logging in, if you like.")


@app.command()
def adopt(user: User = None, database: Database = None, dump: Dump = None, replace: Replace = False,
          yes: Yes = False) -> None:
    """Give a WordPress site that came from another server its database here.

    The site's files must already be in its web root, wp-config.php included. This makes the database and a MariaDB
    user of the same name with a new password, reads the dump into it, and changes the database lines in the site's
    wp-config.php to match. Nothing else in wp-config.php changes. To start a new WordPress instead, use wp install.

    A database that is already there stops it, unless you pass --replace: that empties it first, after asking.

    [dim]Example:[/] domainctl wp adopt example --dump example.sql
    [dim]From the old server:[/] ssh old mysqldump example | domainctl wp adopt example --dump -
    """
    with open_session() as session:
        config = session.config
        owner = _adopting_user(config, ask_user(user))
        name = names.user_name(database or owner)
        if not wordpress.is_configured(config, owner):
            raise CtlError(f"There is no wp-config.php in {config.docroot_of(owner)}.",
                           hint="Copy the site's files there first, wp-config.php included, and run this again.")
        if dump not in (None, "-") and not Path(dump).is_file():
            raise CtlError(f"There is no dump at {dump}.")
        wordpress.check_config(config, owner)  # before the old database goes, not after the dump is in
        taken = wordpress.database_taken(name)
        if taken and not replace:
            raise CtlError(f"There is already {taken} in MariaDB.",
                           hint="Pick another name with --database, or empty it and use it with --replace.")
        if taken:
            ui.confirm(f"{taken[0].upper()}{taken[1:]} will be emptied and removed, with everything in it. Go on?", yes)
            wordpress.drop_database(name)
        password = wordpress.new_password()
        wordpress.create_database(name, password)
        try:
            if dump is not None:
                with ui.console.status("Reading the dump in…"):
                    _read_dump(name, dump)
            wordpress.adopt_config(config, owner, name, name, password)
        except CtlError as problem:
            wordpress.drop_database(name)  # nothing knows its password
            if taken:
                raise CtlError(problem.message, hint=f"The old database {name} is gone. Fix this and run the same "
                                                     f"command again: it reads the dump in again.") from None
            raise
        ui.success(f"{'Replaced' if taken else 'Made'} the database {name}, with a MariaDB user of the same name.")
        if dump is not None:
            ui.success("Read the dump into it.")
        ui.success(f"{config.docroot_of(owner) / 'wp-config.php'} now points at it.")


def _adopting_user(config, user: str) -> str:
    """The site's user. A site whose domain still points at the old server has its user and directories but no
    virtual host yet, and that is the state it is in while its files and database are moved over."""
    if sites.find(config, user):
        return user
    if users.exists(user) and layout.made_by_domainctl(config, user):
        ui.note(f"The virtual host of {user} is still waiting for the DNS; its files and database can be put in "
                f"place meanwhile.")
        return user
    find_site(config, user)  # says there is no such site, and which there are
    return user


def _read_dump(database: str, dump: str) -> None:
    if dump == "-":
        wordpress.import_dump(database, sys.stdin.buffer)
        return
    with open(dump, "rb") as file:
        wordpress.import_dump(database, file)


@app.command()
def check(user: UserFilter = None, fix: Fix = False) -> None:
    """Say which WordPress sites have WP fail2ban active, and put it on the ones that don't with --fix.

    A site without it never shows up in the server's log, so fail2ban can't ban anyone guessing its passwords.
    Only a WordPress in the site's web root is found.

    [dim]Example:[/] domainctl wp check
    [dim]Put it right:[/] domainctl wp check --fix
    """
    with open_session() as session:
        config = session.config
        found = [site for site in sites.list_sites(config) if user is None or site.user == user]
        if not found:
            ui.note("There are no sites on this server yet." if user is None else f"There is no site {user}.")
            return
        lacking = []
        table = ui.table("Site", "Domain", "WP fail2ban")
        for site in found:
            if not wordpress.is_wordpress(config, site.user):
                ui.add_row(table, site.user, site.domain, ui.dim("not WordPress"))
                continue
            try:
                status = wordpress.plugin_status(config, site.user)
            except CtlError as problem:
                ui.add_row(table, site.user, site.domain,
                           _cell(Status.WARN, f"can't tell: {problem.message}"))
                continue
            ui.add_row(table, site.user, site.domain, _cell(*_STATUS[status]))
            if status is not Plugin.ACTIVE:
                lacking.append(site)
        ui.console.print(table)
        if not lacking:
            return
        if not fix:
            ui.note(f"{ui.plural(len(lacking), 'site')} can't be protected by fail2ban. "
                    f"Put WP fail2ban on them with: domainctl wp check --fix")
            return
        for site in lacking:
            try:
                _secure(config, site.user)
            except CtlError as problem:
                ui.warn(f"{site.user}: {problem.message}")


def _cell(status: Status, label: str) -> Text:
    return Text.assemble(ui.mark(status), " ", ui.text(label))


def _make_database(config, user: str) -> None:
    """The site's database and MariaDB user, and a wp-config.php that uses them."""
    taken = wordpress.database_taken(user)
    if taken:
        raise CtlError(f"There is already {taken} in MariaDB, and no wp-config.php in {config.docroot_of(user)} to "
                       f"say it is this site's.",
                       hint="Remove it if it's left over, or put the site's wp-config.php in place, and run this "
                            "again.")
    password = wordpress.new_password()
    wordpress.create_database(user, password)
    try:
        wordpress.configure(config, user, user, password)
    except CtlError:
        wordpress.drop_database(user)  # nothing knows its password
        raise
    ui.success(f"Made the database {user}, with a MariaDB user of the same name, and wp-config.php.")


def _secure(config, user: str) -> None:
    """Puts WP fail2ban on the site, active."""
    status = wordpress.plugin_status(config, user)
    if status is Plugin.ACTIVE:
        ui.success(f"WP fail2ban is active on {user}.")
        return
    with ui.console.status(f"Putting WP fail2ban on {user}…"):
        wordpress.add_plugin(config, user, status)
    if wordpress.plugin_status(config, user) is not Plugin.ACTIVE:
        raise CtlError(f"WP fail2ban still isn't active on {user}.",
                       hint=f"Look with: su - {user} -c 'wp --path={config.docroot_of(user)} plugin list'")
    ui.success(f"WP fail2ban is active on {user}: fail2ban bans whoever guesses its passwords.")
