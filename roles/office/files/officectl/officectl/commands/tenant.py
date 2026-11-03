"""officectl's commands for the Nextclouds themselves: add, list, remove, doctor, occ and update."""

import sys
from datetime import UTC, datetime
from typing import Annotated

import typer

from serverctl import certbot, dnsnames, mariadb, openlitespeed, passwords, serving, ui, users
from serverctl.dns import Check, Status
from serverctl.dnsnames import NameCheck, State
from serverctl.errors import CtlError

from ..core import collabora, endpoint, layout, nextcloud, php, tenants
from ..core.tenants import Tenant
from ..session import Session, open_session
from .shared import (
    Name, NameFilter, NoDns, Purge, Yes, addresses, ask_name, find_tenant, no_tenant, point_here,
    require_collabora, wopi_addresses,
)

Admin = Annotated[str, typer.Option("--admin", help="The name of the first administrator.")]
# How long ago the background jobs may last have run before it is worth saying so: Nextcloud asks for them
# every five minutes, and says so itself on its settings page after an hour.
CRON_OVERDUE = 3600


def add(name: Name = None, admin: Admin = "admin", no_dns: NoDns = False, yes: Yes = False) -> None:
    """Set up a Nextcloud of its own at NAME.office.<domain>, with Collabora in it.

    It gets a Linux user and a home directory of its own, a database, a PHP pool only it uses, a virtual host
    with a Let's Encrypt certificate, and the background jobs Nextcloud needs. Collabora is told it may open
    documents for it, and Nextcloud is told where Collabora is.

    The name has to point at this server before any of this is made: Let's Encrypt checks it, and so does
    Collabora every time it opens a document. A name that has no DNS record at all is offered to TransIP.

    Run it again for the same name to pick up where it stopped: it only ever does what isn't there yet.

    [dim]Example:[/] officectl add acme
    [dim]Another administrator:[/] officectl add acme --admin guido
    """
    with open_session() as session:
        config = session.config
        require_collabora(config)
        tenant = tenants.of(config, ask_name(name, config))
        known = tenants.find(config, tenant.slug)
        clash = tenants.elsewhere(config, tenant, tenants.read(config))
        if clash and not known:
            raise CtlError(clash, hint="Remove it first, or choose another name.")
        found = point_here(session, tenant.host, no_dns, yes)
        if not found.ready:
            raise CtlError(f"{found.detail} Let's Encrypt checks the same thing, and so does Collabora every "
                           f"time it opens a document.",
                           hint=f"Point {tenant.host} at {addresses(session)} and run this again.")
        ui.success(f"{tenant.host} points to this server.")
        _make_room(config, tenant)
        password = _install(config, tenant, admin, yes)
        _serve(session, tenant, found)
        _let_collabora_in(config, tenant)
        _editor(session, tenant)
        if nextcloud.write_cron(config, tenant):
            ui.success(f"Its background jobs run every {ui.plural(config.cron_minutes, 'minute')}.")
        ui.success(f"{tenant.url} is ready.")
        if password:
            ui.console.print(f"\n  address   {tenant.url}\n  user      {admin}\n  password  {password}\n")
            ui.note("Write the password down: it is not stored anywhere else and this is the only time it is "
                    "shown. It can be changed in Nextcloud itself.")


def list_tenants(name: NameFilter = None) -> None:
    """List the Nextclouds on this server, with their version and certificate.

    [dim]Example:[/] officectl list
    """
    with open_session() as session:
        config = session.config
        found = [tenant for tenant in tenants.list_tenants(config) if name is None or tenant.slug == name]
        if not found:
            ui.note("There are no Nextclouds on this server yet." if name is None
                    else f"There is no Nextcloud called {name}.")
            ui.note(f"Add one with: officectl add NAME (it is served at NAME.{config.office_host})")
            return
        now = datetime.now(UTC)
        table = ui.table("Name", "Address", "Nextcloud", "Certificate")
        for tenant in found:
            ui.add_row(table, tenant.slug, tenant.url, nextcloud.version(config, tenant.user) or "–",
                       _certificate(config, tenant, now))
        ui.console.print(table)
        ui.note(f"They open documents through Collabora at https://{config.office_host}.")


def remove(name: Name = None, purge: Purge = False, yes: Yes = False) -> None:
    """Take a Nextcloud off this server: its virtual host, its certificate and its background jobs.

    Its files and its database are kept unless you add --purge, so what people stored in it is still there and
    the same command can set it up again.

    [dim]Example:[/] officectl remove acme
    [dim]Files and all:[/] officectl remove acme --purge
    """
    with open_session() as session:
        config = session.config
        tenant = find_tenant(config, ui.ask("Name", name, "NAME"))
        if purge:
            ui.warn(f"--purge deletes the database {tenant.user} and {config.home(tenant.user)} with "
                    f"everything in it, the files everyone keeps in this Nextcloud included. This cannot be "
                    f"undone.")
            ui.confirm_by_typing(tenant.slug, f"Delete the Nextcloud at {tenant.host} and all its files?", yes)
        else:
            ui.confirm(f"Take the Nextcloud at {tenant.host} off this server?", yes)
        if tenants.remove(config, tenant):
            openlitespeed.restart(config.ols_root)
            ui.success(f"OpenLiteSpeed no longer serves {tenant.host}.")
        if certbot.delete(config.letsencrypt_dir, tenant.host):
            ui.success(f"Removed the certificate for {tenant.host}.")
        if nextcloud.remove_cron(config, tenant):
            ui.success("Stopped its background jobs.")
        if php.remove_settings(config, tenant.user):
            ui.success("Removed the settings of its PHP.")
        _let_collabora_in(config, tenant, listed=False)
        if not purge:
            ui.note(f"{config.home(tenant.user)}, the database {tenant.user} and the Linux user are still "
                    f"there. Remove them with: officectl remove {tenant.slug} --purge")
            return
        mariadb.drop_database(tenant.user)
        ui.success(f"Removed the database {tenant.user} and its MariaDB user.")
        users.delete(tenant.user, remove_home=True)
        ui.success(f"Removed the Linux user {tenant.user} and {config.home(tenant.user)}.")


def doctor(name: NameFilter = None) -> None:
    """Say how Collabora and each Nextcloud are doing: their names, certificates, settings and jobs.

    The same question mailctl's and domainctl's doctor answer for mail and for the web sites.

    [dim]Example:[/] officectl doctor
    [dim]One of them:[/] officectl doctor acme
    """
    with open_session() as session:
        config = session.config
        found = [tenant for tenant in tenants.list_tenants(config) if name is None or tenant.slug == name]
        if name is None:
            ui.heading(f"Collabora — {config.office_host}")
            for result in _collabora_checks(session, found):
                ui.line(ui.mark(result.status), " ", result.detail, indent=1)
        if not found:
            ui.note("\nThere are no Nextclouds on this server yet." if name is None
                    else f"There is no Nextcloud called {name}.")
            return
        now = datetime.now(UTC)
        for tenant in found:
            ui.heading(f"{tenant.slug} — {tenant.host}")
            for result in _tenant_checks(session, tenant, now):
                ui.line(ui.mark(result.status), " ", result.detail, indent=1)


def run_occ(context: typer.Context, name: Name = None) -> None:
    """Run Nextcloud's own occ command for one of them, as the user its PHP runs as.

    Everything after the name is handed to occ as it is. Run it without arguments to see what occ can do.

    [dim]Example:[/] officectl occ acme status
    [dim]Its apps:[/] officectl occ acme app:list
    [dim]A new password:[/] officectl occ acme user:resetpassword admin
    """
    with open_session() as session:
        config = session.config
        tenant = find_tenant(config, ui.ask("Name", name, "NAME"))
        if not nextcloud.code_here(config, tenant.user):
            raise CtlError(f"There are no Nextcloud files in {config.docroot_of(tenant.user)}.",
                           hint=f"Set it up again with: officectl add {tenant.slug}")
        # occ's own output, as it wrote it: this is that command, not a report about it.
        sys.stdout.write(nextcloud.occ(config, tenant, *context.args))


def update(name: NameFilter = None, yes: Yes = False) -> None:
    """Put the newest Nextcloud in place and bring its database up to date.

    The files of a Nextcloud are root's and it may only read them, so it can't update itself: this fetches the
    release, lays it out with this Nextcloud's own settings and apps in it, swaps the two, and runs the upgrade.
    The old files are kept next to it, so a release that turns out to be wrong can be put back.

    Nextcloud is unreachable while the upgrade runs, which is a minute or two.

    [dim]Example:[/] officectl update acme
    [dim]All of them:[/] officectl update
    """
    with open_session() as session:
        config = session.config
        found = [tenant for tenant in tenants.list_tenants(config) if name is None or tenant.slug == name]
        if not found:
            raise no_tenant(config, name) if name else CtlError(
                "There are no Nextclouds on this server yet.", hint="Add one with: officectl add NAME")
        for tenant in found:
            ui.heading(f"{tenant.slug} — {tenant.host}")
            if not nextcloud.installed(config, tenant):
                ui.warn("It isn't installed, so there is nothing to update. Leaving it as it is.")
                continue
            here = nextcloud.version(config, tenant.user)
            ui.confirm(f"Update the Nextcloud at {tenant.host}, which is {here} now?", yes)
            nextcloud.occ(config, tenant, "maintenance:mode", "--on")
            try:
                with ui.console.status("Fetching the newest Nextcloud…"):
                    release, kept = nextcloud.replace_code(config, tenant)
                if kept is None:
                    ui.success(f"Nextcloud {release} is the newest there is, and it is the one in place.")
                    continue
                ui.success(f"Nextcloud {release} is in place; the files of {here} are in {kept}.")
                with ui.console.status("Bringing its database up to date…"):
                    nextcloud.upgrade(config, tenant)
            finally:
                nextcloud.occ(config, tenant, "maintenance:mode", "--off")
            ui.success(f"{tenant.url} is running Nextcloud {release}.")
            ui.note(f"Delete {kept} once it has been used for a few days.")


# --- The steps of "officectl add" --------------------------------------------------------------------------

def _make_room(config, tenant: Tenant) -> None:
    """The Linux user, the directories, the permissions and the PHP settings of one Nextcloud."""
    if not users.exists(tenant.user):
        users.create(tenant.user, config.home(tenant.user), config.user_shell)
        ui.success(f"Made the user {tenant.user} with {config.home(tenant.user)}, without a password.")
    layout.create(config, tenant.user)
    if layout.apply_permissions(config, tenant.user):
        ui.success(f"{config.web_user} may read {config.docroot_of(tenant.user)} and write in "
                   f"{config.logs_of(tenant.user)}.")
    if php.write_settings(config, tenant.user, tenant.host):
        ui.success(f"Wrote the settings of its own PHP in {php.settings_path(config, tenant.user)}.")


def _install(config, tenant: Tenant, admin: str, yes: bool) -> str:
    """Nextcloud's files, its database and its settings. Returns the administrator's password when it set one
    up, or "" when it was already installed."""
    if not nextcloud.code_here(config, tenant.user):
        with ui.console.status("Downloading Nextcloud…"):
            release = nextcloud.put_code_in_place(config, tenant)
        # The release brings a settings directory and the apps it ships with, which Nextcloud itself writes in.
        layout.create(config, tenant.user)
        ui.success(f"Nextcloud {release} is in {config.docroot_of(tenant.user)}.")
    password = ""
    if not nextcloud.installed(config, tenant):
        db_password = _database(config, tenant, yes)
        password = passwords.new_password()
        with ui.console.status("Setting Nextcloud up…"):
            nextcloud.install(config, tenant, tenant.user, db_password, admin, password)
        ui.success(f"Nextcloud {nextcloud.version(config, tenant.user)} is set up in the database "
                   f"{tenant.user}.")
        nextcloud.apply_settings(config, tenant, nextcloud.mail_settings(config))
        ui.success(f"It sends its mail through this server, as {config.sender()}.")
    to_change = nextcloud.settings_to_change(nextcloud.settings(config, tenant),
                                             nextcloud.current_settings(config, tenant))
    if to_change:
        nextcloud.apply_settings(config, tenant, to_change)
        ui.success(f"Brought {ui.plural(len(to_change), 'setting')} of it up to date: it is reached at "
                   f"{tenant.host} and caches in Redis.")
    return password


def _database(config, tenant: Tenant, yes: bool) -> str:
    """The database and the MariaDB user of this Nextcloud. Returns the password it was given."""
    taken = mariadb.database_taken(tenant.user)
    if taken:
        ui.warn(f"There is already {taken} in MariaDB, and no Nextcloud is set up in it.")
        ui.confirm("Empty it and set this Nextcloud up in it?", yes)
        mariadb.drop_database(tenant.user)
    password = passwords.new_password()
    mariadb.create_database(tenant.user, password)
    ui.success(f"Made the database {tenant.user}, with a MariaDB user of the same name.")
    return password


def _serve(session: Session, tenant: Tenant, found: NameCheck) -> None:
    """The virtual host and the certificate, so the Nextcloud is reached over HTTPS."""
    config = session.config
    if tenants.save(config, tenant, tenants.certified(config, tenant)):
        openlitespeed.restart(config.ols_root)
        ui.success(f"OpenLiteSpeed serves {tenant.host}.")
    if tenants.certified(config, tenant) and \
            certbot.covers(certbot.read(tenants.certificate_path(config, tenant)), tenant.host):
        ui.success(f"{tenant.host} has its certificate already.")
        return
    with ui.console.status("Checking that OpenLiteSpeed serves it…"):
        unanswered = serving.wait_until_served(config.docroot_of(tenant.user), tenant.host,
                                               set(found.addresses))
    for line in unanswered:
        ui.note(line)
    with ui.console.status("Asking Let's Encrypt for a certificate…"):
        tenants.obtain(config, tenant)
    if tenants.save(config, tenant, True):
        openlitespeed.restart(config.ols_root)
    ui.success(f"{tenant.url} has a Let's Encrypt certificate.")


def _let_collabora_in(config, tenant: Tenant, listed: bool = True) -> None:
    """Brings the list of Nextclouds Collabora may open documents for up to date."""
    hosts = {other.host for other in tenants.list_tenants(config)}
    hosts = hosts | {tenant.host} if listed else hosts - {tenant.host}
    if not collabora.configure(config, sorted(hosts)):
        return
    collabora.restart(config)
    if listed:
        ui.success(f"Collabora opens documents for {tenant.host}.")
    else:
        ui.success(f"Collabora no longer opens documents for {tenant.host}.")


def _editor(session: Session, tenant: Tenant) -> None:
    """Nextcloud's side of it: the app that puts Collabora's editor in its pages, and where to find it."""
    config = session.config
    present = nextcloud.apps(config, tenant)
    if nextcloud.add_app(config, tenant, nextcloud.APP, present):
        ui.success("Collabora's editor is switched on in it.")
    if nextcloud.set_app_settings(config, tenant, nextcloud.APP,
                                  nextcloud.collabora_settings(config, wopi_addresses(session))):
        ui.success(f"It opens documents through https://{config.office_host}.")


# --- What the doctor looks at ------------------------------------------------------------------------------

def _collabora_checks(session: Session, found: list[Tenant]):
    config = session.config
    if not collabora.installed(config):
        yield Check("Collabora", Status.FAIL, "Collabora Online isn't installed. Run the Ansible playbook for "
                                              "this server.")
        return
    if collabora.running(config):
        yield Check("Collabora", Status.OK, f"Collabora {collabora.version() or 'Online'} is running.")
    else:
        yield Check("Collabora", Status.FAIL, f"Collabora isn't running. Look at: systemctl status "
                                              f"{config.coolwsd_service}")
    where = _points_here(session, config.office_host)
    yield where
    if not endpoint.certified(config):
        yield Check("Certificate", Status.FAIL,
                    f"{config.office_host} has no certificate, so no Nextcloud can reach it. Get one with: "
                    f"officectl endpoint")
    else:
        invalid = certbot.problem(endpoint.certificate_path(config), [config.office_host], datetime.now(UTC))
        if invalid:
            yield Check("Certificate", Status.FAIL, f"{invalid} Renew it with: certbot renew")
        else:
            expires = certbot.read(endpoint.certificate_path(config)).expires
            yield Check("Certificate", Status.OK, f"Its certificate is valid until {expires:%Y-%m-%d}.")
    listed = collabora.allowed_hosts(collabora.read(config.coolwsd_config))
    wanted = [collabora.host_pattern(host)
              for host in collabora.allowed(config, [tenant.host for tenant in found])]
    if set(listed) == set(wanted):
        yield Check("Which", Status.OK,
                    f"It opens documents for {ui.plural(len(wanted), 'Nextcloud')} on this server"
                    f"{', one of which officectl did not make' if config.other_hosts() else ''}.")
    else:
        yield Check("Which", Status.FAIL,
                    f"The Nextclouds it lets in aren't the ones on this server: it has {len(listed)} and "
                    f"there are {len(wanted)}. Put it right with: officectl sync")


def _tenant_checks(session: Session, tenant: Tenant, now: datetime):
    config = session.config
    yield _points_here(session, tenant.host)
    if not tenants.certified(config, tenant):
        yield Check("Certificate", Status.FAIL,
                    f"There is no certificate, so {tenant.host} is only served over HTTP. Get one with: "
                    f"officectl add {tenant.slug}")
    else:
        invalid = certbot.problem(tenants.certificate_path(config, tenant), [tenant.host], now)
        if invalid:
            yield Check("Certificate", Status.FAIL, f"{invalid} Renew it with: certbot renew")
        else:
            expires = certbot.read(tenants.certificate_path(config, tenant)).expires
            yield Check("Certificate", Status.OK, f"The certificate is valid until {expires:%Y-%m-%d}.")
    wrong = layout.missing_permissions(config, tenant.user)
    if wrong:
        paths = ", ".join(str(path) for path in wrong)
        yield Check("Permissions", Status.FAIL,
                    f"{config.web_user} can't reach its files the way it should: {paths}. Correct it with: "
                    f"officectl sync")
    else:
        yield Check("Permissions", Status.OK, f"{config.web_user} may read its files.")
    found = nextcloud.status(config, tenant)
    if not found:
        yield Check("Nextcloud", Status.FAIL,
                    f"Nextcloud doesn't answer. Look at: officectl occ {tenant.slug} status")
        return
    if not found.get("installed"):
        yield Check("Nextcloud", Status.FAIL,
                    f"Its files are there but it isn't set up. Finish it with: officectl add {tenant.slug}")
        return
    if found.get("needsDbUpgrade"):
        yield Check("Nextcloud", Status.FAIL, f"Its files are newer than its database. Bring it up to date "
                                              f"with: officectl update {tenant.slug}")
    elif found.get("maintenance"):
        yield Check("Nextcloud", Status.WARN, "It is in maintenance mode, so nobody can use it. Switch it off "
                                              f"with: officectl occ {tenant.slug} maintenance:mode --off")
    else:
        yield Check("Nextcloud", Status.OK, f"Nextcloud {found.get('versionstring', '')} answers.")
    to_change = nextcloud.settings_to_change(nextcloud.settings(config, tenant),
                                             nextcloud.current_settings(config, tenant))
    if to_change:
        yield Check("Settings", Status.FAIL,
                    f"{ui.plural(len(to_change), 'setting')} of it isn't what it should be: "
                    f"{', '.join('.'.join(setting.keys) for setting in to_change)}. Put it right with: "
                    f"officectl add {tenant.slug}")
    else:
        yield Check("Settings", Status.OK, "Its own settings are the ones officectl keeps.")
    yield _editor_check(session, tenant)
    last = nextcloud.last_cron(config, tenant)
    if not last:
        yield Check("Jobs", Status.FAIL, "Its background jobs have never run. Look at: "
                                         f"{nextcloud.cron_path(config, tenant)}")
    elif now.timestamp() - last > CRON_OVERDUE:
        yield Check("Jobs", Status.WARN,
                    f"Its background jobs last ran {ui.moment(datetime.fromtimestamp(last, UTC), now)}, which "
                    f"is longer ago than it should be. Look at: {nextcloud.cron_path(config, tenant)}")
    else:
        yield Check("Jobs", Status.OK,
                    f"Its background jobs ran {ui.moment(datetime.fromtimestamp(last, UTC), now)}.")


def _editor_check(session: Session, tenant: Tenant) -> Check:
    config = session.config
    if nextcloud.apps(config, tenant).get(nextcloud.APP) != "enabled":
        return Check("Editor", Status.FAIL,
                     f"Collabora's editor is switched off in it, so nobody can open a document. Switch it on "
                     f"with: officectl add {tenant.slug}")
    wanted = nextcloud.collabora_settings(config, wopi_addresses(session))
    present = nextcloud.app_settings(config, tenant, nextcloud.APP)
    wrong = [key for key, value in wanted.items() if present.get(key) != value]
    if "wopi_url" in wrong:
        return Check("Editor", Status.FAIL,
                     f"It opens documents at {present.get('wopi_url') or 'nowhere'} and not at "
                     f"https://{config.office_host}. Put it right with: officectl add {tenant.slug}")
    if wrong:
        return Check("Editor", Status.WARN,
                     f"Collabora's editor is on, and {', '.join(wrong)} isn't what officectl would set. Put it "
                     f"right with: officectl add {tenant.slug}")
    return Check("Editor", Status.OK, f"It opens documents through https://{config.office_host}.")


def _points_here(session: Session, name: str) -> Check:
    found = dnsnames.check(session.resolver, name, session.server_ips)
    status = Status.OK if found.ready else (Status.WARN if found.state is State.UNKNOWN else Status.FAIL)
    return Check(name, status, f"{name} points to this server." if found.ready else found.detail)


def _certificate(config, tenant: Tenant, now: datetime) -> str:
    if not tenants.certified(config, tenant):
        return "none"
    path = tenants.certificate_path(config, tenant)
    if certbot.problem(path, [tenant.host], now):
        return "problem"
    return f"until {certbot.read(path).expires:%Y-%m-%d}"
