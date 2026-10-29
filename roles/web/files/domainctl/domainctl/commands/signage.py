"""domainctl signage: piSignage servers, one per customer, each at a name of its own over HTTPS."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from serverctl import certbot, names, openlitespeed, ui
from serverctl.errors import CtlError

from ..core import serving, signage, users
from ..core.dnsnames import State
from ..core.signage import Signage
from ..session import open_session
from . import site
from .shared import Domain, NoDns, SiteName, UserOption, Yes, ask_domain, ask_site, group

app = group("piSignage servers: one per customer, at a name of its own like andre.milocast.nl, over HTTPS.")

Port = Annotated[Optional[int], typer.Option(
    "--port", min=1024, max=65535, show_default=False,
    help="The port piSignage runs on behind OpenLiteSpeed. The first free one from 3000 up when left out.")]
Purge = Annotated[bool, typer.Option(
    "--purge", help="Also delete the Linux user, its home directory with piSignage and the media, and its database.")]
FromDatabase = Annotated[str, typer.Option(
    "--from-database", show_default=False, help="The database's name on the server it comes from.")]
Dump = Annotated[Path, typer.Option(
    "--dump", show_default=False, help="The mongodump archive (--archive --gzip), or - to read it from standard input.")]


@app.command()
def add(domain: Domain = None, user: UserOption = None, port: Port = None, no_dns: NoDns = False,
        yes: Yes = False) -> None:
    """Set up a piSignage server for a customer, at a name of its own, over HTTPS.

    It gets a Linux user of its own with piSignage in its home directory, a MongoDB database and a port of its own,
    the first free one from 3000 up, and runs as the service pisignage@USER. OpenLiteSpeed passes everything for the
    name on to it, the players' WebSocket connections included, once Let's Encrypt has given the name a certificate.

    The server is made and started whatever the DNS says; the certificate needs the name to point here. Run the
    same command again once it does, or to pick up wherever it stopped.

    [dim]Example:[/] domainctl signage add andre.milocast.nl
    [dim]On a port of your choice:[/] domainctl signage add andre.milocast.nl --port 3005
    """
    with open_session() as session:
        config = session.config
        if not signage.installed():
            raise CtlError("piSignage isn't installed on this server.",
                           hint="Set web_pisignage: true in this server's playbook and run it: that installs MongoDB, "
                                "Node.js and the service the piSignage servers run as.")
        domain = ask_domain(domain)
        server, new = _server_for(config, domain, user, port)
        if not users.exists(server.user):
            users.create(server.user, config.home(server.user), config.user_shell)
            ui.success(f"Made the user {server.user} with {config.home(server.user)}, without a password.")
        with ui.console.status("Fetching piSignage and the packages it needs…"):
            done = signage.install(config, server.user)
        for line in done:
            ui.success(line)
        signage.start(server, restart=signage.save(config, server))
        with ui.console.status(f"Waiting for piSignage to answer on port {server.port}…"):
            answering = signage.wait_until_answering(server.port)
        if answering:
            ui.success(f"piSignage runs as {server.unit}, on port {server.port}.")
        else:
            ui.warn(f"piSignage doesn't answer on port {server.port}.")
            ui.note(f"See why with: journalctl -u {server.unit}")
        _serve(session, server, no_dns, yes)
        if new:
            ui.note("A new piSignage server's login is pi with the password pi: change it under Settings.")


def _server_for(config, domain: str, user: str | None, port: int | None) -> tuple[Signage, bool]:
    """The server for the domain, the one it is already or a new one, and whether it is new."""
    existing = signage.find_domain(config, domain)
    name = names.user_name(user) if user else existing.user if existing else names.user_name_for(domain)
    if existing and existing.user != name:
        raise CtlError(f"{domain} is already the piSignage server {existing.user}.",
                       hint=f"Leave --user out to work on it: domainctl signage add {domain}")
    mine = signage.find(config, name)
    if mine and mine.domain != domain:
        raise CtlError(f"The piSignage server {name} is already {mine.domain}'s.",
                       hint="Choose another name with --user.")
    clash = signage.taken(config, domain, name, openlitespeed.read(config.ols_root))
    if clash:
        raise CtlError(clash, hint="Remove it first, or choose another domain.")
    if not mine and users.exists(name) and not signage.made_by_domainctl(config, name):
        raise CtlError(f"There is already a Linux user {name} on this server, which isn't a piSignage server.",
                       hint="Choose another name with --user.")
    if port:
        problem = signage.port_problem(config, name, port)
        if problem:
            raise CtlError(problem, hint="Choose another one with --port, or leave it out to take the first free one.")
    chosen = port or (mine.port if mine else signage.free_port(config))
    return Signage(name, domain, chosen), not mine and not signage.made_by_domainctl(config, name)


def _serve(session, server: Signage, no_dns: bool, yes: bool) -> None:
    """Puts OpenLiteSpeed in front of the server, and gets its certificate once the name points here."""
    config = session.config
    found = site._resolve(session, server.domain, no_dns, yes)
    if found.state is State.UNKNOWN:
        raise CtlError(f"Can't tell where {server.domain} points: {found.detail}", hint="Try again when DNS answers.")
    certified = signage.certified(config, server)
    if signage.publish(config, server, certified):
        openlitespeed.restart(config.ols_root)
    if not found.ready:
        ui.warn(f"{found.detail} Let's Encrypt checks the same thing, so there is no certificate yet.")
        ui.note(f"Point {server.domain} at {site._addresses(session)} and run the same command again: it then "
                f"gets its certificate and is served over HTTPS.")
        return
    ui.success(f"{server.domain} points to this server.")
    if certified:
        ui.success(f"https://{server.domain} is live.")
        return
    with ui.console.status("Checking that OpenLiteSpeed serves the name…"):
        unanswered = serving.wait_until_served(signage.webroot(config, server.user), server.domain,
                                               set(found.addresses))
    for line in unanswered:
        ui.note(line)
    try:
        with ui.console.status("Asking Let's Encrypt for a certificate…"):
            signage.obtain(config, server)
    except CtlError as problem:
        ui.warn(problem.message)
        ui.note(f"Fix the reason above and run: domainctl signage add {server.domain}")
        return
    if signage.publish(config, server, True):
        openlitespeed.restart(config.ols_root)
    ui.success(f"https://{server.domain} is live with a Let's Encrypt certificate.")


@app.command(name="list")
def list_servers() -> None:
    """List the piSignage servers: their name, port, whether they run, and their certificate.

    [dim]Example:[/] domainctl signage list
    """
    with open_session() as session:
        config = session.config
        found = signage.all_servers(config)
        if not found:
            ui.note("There are no piSignage servers on this server yet.")
            return
        now = datetime.now(UTC)
        table = ui.table("Name", "Domain", "Port", "Running", "Certificate")
        for server in found:
            ui.add_row(table, server.user, server.domain, str(server.port), ui.yes_no(signage.running(server.user)),
                       site._certificate(config, server, now))
        ui.console.print(table)


@app.command()
def delete(name: SiteName = None, purge: Purge = False, yes: Yes = False) -> None:
    """Remove a piSignage server: it stops, and its virtual host and its certificate go.

    The Linux user, its home directory with piSignage and the uploaded media, and its database are kept unless you
    add --purge. Adding the same domain again later takes them back up.

    Takes the server's domain as well as its name.

    [dim]Example:[/] domainctl signage delete andremilocast
    [dim]By its domain:[/] domainctl signage delete andre.milocast.nl
    [dim]Media, database and all:[/] domainctl signage delete andremilocast --purge
    """
    with open_session() as session:
        config = session.config
        given = ask_site(name)
        server = signage.find_domain(config, given) if "." in given else signage.find(config, given)
        if not server:
            _delete_leftover(config, given, yes)
            return
        what = f"the piSignage server {server.user} ({server.domain})"
        if purge:
            ui.warn(f"--purge also deletes {config.home(server.user)} with the uploaded media in it, and the database "
                    f"{server.database}. This cannot be undone.")
            ui.confirm_by_typing(server.user, f"Delete {what}, its media and its database?", yes)
        else:
            ui.confirm(f"Delete {what}?", yes)
        signage.stop(server.user)
        ui.success(f"Stopped {server.unit}.")
        if signage.unpublish(config, server.user):
            openlitespeed.restart(config.ols_root)
        if certbot.delete(config.letsencrypt_dir, server.user):
            ui.success(f"Removed the certificate for {server.domain}.")
        signage.settings_file(config, server.user).unlink(missing_ok=True)
        ui.success(f"Removed the piSignage server {server.user}.")
        if purge:
            signage.drop_database(server.user)
            users.delete(server.user, remove_home=True)
            ui.success(f"Removed the database {server.database}, the user {server.user} and "
                       f"{config.home(server.user)}.")
        else:
            ui.note(f"{config.home(server.user)} with the media, the user {server.user} and the database "
                    f"{server.database} are still there. Remove them with: domainctl signage delete {server.user}")


@app.command()
def restore(name: SiteName = None, from_database: FromDatabase = ..., dump: Dump = ..., yes: Yes = False) -> None:
    """Read a piSignage server in from another server: its database, and the media and data copied into its home.

    migrate.py on the old server runs this. It first copies the media into ~/media and piSignage's data folder (the
    licences and player software) into ~/pisignage-server/data, then sends the database here as a mongodump archive.
    The server's own database is replaced by it, and the files copied in become the server's user's. piSignage is
    stopped meanwhile and started again afterwards.

    [dim]Example:[/] domainctl signage restore andremilocast --from-database pisignage-server-dev --dump andre.archive
    """
    with open_session() as session:
        config = session.config
        given = ask_site(name)
        server = signage.find_domain(config, given) if "." in given else signage.find(config, given)
        if not server:
            raise CtlError(f"There is no piSignage server {given} on this server.",
                           hint="Make it first with: domainctl signage add DOMAIN")
        ui.confirm(f"Replace the database of {server.user} ({server.domain}) with the one in the archive?", yes)
        if str(dump) == "-":
            with ui.console.status(f"Reading the database {from_database} in…"):
                signage.restore(config, server, from_database, sys.stdin.buffer)
        else:
            try:
                archive = dump.open("rb")
            except OSError as error:
                raise CtlError(f"Can't read {dump}: {error.strerror}.") from None
            with archive, ui.console.status(f"Reading the database {from_database} in…"):
                signage.restore(config, server, from_database, archive)
        ui.success(f"Read the database {from_database} in as {server.database}.")
        if signage.wait_until_answering(server.port):
            ui.success(f"piSignage runs again as {server.unit}.")
        else:
            ui.warn(f"piSignage doesn't answer on port {server.port}.")
            ui.note(f"See why with: journalctl -u {server.unit}")


def _delete_leftover(config, given: str, yes: bool) -> None:
    """A server deleted before is only its user, its home and its database, so deleting it deletes those."""
    if "." in given or not users.exists(given) or not signage.made_by_domainctl(config, given):
        known = ", ".join(found.user for found in signage.all_servers(config))
        raise CtlError(f"There is no piSignage server {given} on this server.",
                       hint=f"They are: {known}." if known else "Add one with: domainctl signage add DOMAIN")
    ui.warn(f"{given} is no piSignage server any more, only its Linux user, {config.home(given)} with the media, "
            f"and the database {signage.database(given)}, so those are deleted. This cannot be undone.")
    ui.confirm_by_typing(given, f"Delete the user {given}, its media and its database?", yes)
    signage.drop_database(given)
    users.delete(given, remove_home=True)
    ui.success(f"Removed the database {signage.database(given)}, the user {given} and {config.home(given)}.")
