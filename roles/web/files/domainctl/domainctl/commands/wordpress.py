"""domainctl wp: WordPress on a site, installed with wp-cli, and the WP fail2ban plugin on every WordPress site."""

from typing import Annotated, Optional

import typer
from rich.text import Text

from serverctl import names, ui
from serverctl.dns import Status
from serverctl.errors import CtlError

from ..core import certificates, layout, sites, wordpress
from ..core.wordpress import Plugin
from ..session import open_session
from .shared import User, UserFilter, ask_user, find_site, group

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
