"""Arguments and steps that several commands share."""

from typing import Annotated, Optional

import typer

from serverctl import dnsnames, transip, ui
from serverctl.dnsnames import NameCheck, State
from serverctl.errors import CtlError
from serverctl.transip import NotInAccount

from ..config import Config
from ..core import collabora, endpoint, php, tenants
from ..core.tenants import Tenant
from ..session import Session

# Typer 0.9 (Ubuntu 24.04) needs Optional[...]: it doesn't understand "str | None" in command parameters.
# Metavars are in brackets, since officectl asks for an argument that is left out.
Name = Annotated[Optional[str], typer.Argument(
    metavar="[NAME]", help="The Nextcloud's name, like acme. Asked for when left out.", show_default=False)]
NameFilter = Annotated[Optional[str], typer.Argument(
    metavar="[NAME]", help="Only this Nextcloud.", show_default=False)]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")]
NoDns = Annotated[bool, typer.Option(
    "--no-dns", help="Don't offer to publish a missing record at TransIP; only look the name up.")]
Purge = Annotated[bool, typer.Option(
    "--purge", help="Also delete its database, its Linux user and all the files in it.")]


def group(description: str) -> typer.Typer:
    return typer.Typer(help=description, no_args_is_help=True, rich_markup_mode="rich")


def ask_name(value: str | None, config: Config) -> str:
    return tenants.slug(ui.ask("Name", value, "NAME"), config)


def find_tenant(config: Config, name: str) -> Tenant:
    found = tenants.find(config, name)
    if not found:
        raise no_tenant(config, name)
    return found


def no_tenant(config: Config, name: str) -> CtlError:
    known = ", ".join(tenant.slug for tenant in tenants.list_tenants(config))
    return CtlError(f"There is no Nextcloud called {name} on this server.",
                    hint=f"There is: {known}." if known else "Add one with: officectl add NAME")


def require_collabora(config: Config) -> None:
    """Stops unless Collabora and the PHP Nextcloud needs are installed and its own site has a certificate.

    A Nextcloud without them is a Nextcloud nobody can open a document in, and that is the one thing these are
    for, so it is better said before anything is made than after.
    """
    if not php.installed(config):
        raise CtlError(f"{config.lsphp()} isn't there, so there is no PHP to run Nextcloud on.",
                       hint="Run the Ansible playbook for this server: the office role installs it.")
    if not collabora.installed(config):
        raise CtlError("Collabora Online isn't installed on this server.",
                       hint="Run the Ansible playbook for this server: the office role installs it.")
    if not endpoint.certified(config):
        raise CtlError(f"Collabora's own site at {config.office_host} has no certificate yet, so no Nextcloud "
                       f"can reach it.",
                       hint="Set it up first with: officectl endpoint")


def point_here(session: Session, name: str, no_dns: bool, yes: bool) -> NameCheck:
    """Looks the name up, and offers to publish a record for it at TransIP when it has none.

    A name that points somewhere else is left alone and reported: these names are this server's own
    subdomains, so one pointing elsewhere is a mistake to look at rather than something to move.
    """
    found = dnsnames.check(session.resolver, name, session.server_ips)
    if found.state is not State.MISSING or no_dns:
        return found
    ips = dnsnames.publishable_ips(session.server_ips)
    if not ips:
        return found
    if not ui.interactive() and not yes:
        ui.note(f"{found.detail} Add --yes to publish one at TransIP without being asked.")
        return found
    where = ", ".join(str(ip) for ip in sorted(ips, key=lambda ip: (ip.version, ip)))
    if not ui.decide(f"{name} has no DNS record. Publish one at TransIP, pointing here ({where})?",
                     True if yes else None, "--yes", default=True):
        return found
    try:
        # The login is read before the spinner starts: a question underneath it is one nobody can see.
        client = transip_client(session)
        with ui.console.status(f"Publishing {name} at TransIP…"):
            zone, added = dnsnames.publish(client, name, ips)
    except (CtlError, NotInAccount) as problem:
        ui.warn(f"Couldn't publish {name}: {problem.message}")
        if problem.hint:
            ui.note(problem.hint)
        return found
    ui.success(f"Published {ui.plural(len(added), 'record')} for {name} in the zone {zone}.")
    with ui.console.status(f"Waiting until {name} resolves here…"):
        return dnsnames.wait_until_resolving(session.resolver, name, session.server_ips)


def transip_client(session: Session) -> transip.Client:
    """The TransIP login this server already has. officectl doesn't ask for one: domainctl and mailctl share
    the same two files, and one place to enter them is enough."""
    access = session.config.transip_access()
    saved = transip.saved_credentials(access)
    if not saved:
        raise CtlError("There is no TransIP login and key on this server yet.",
                       hint="Enter them once with: domainctl dns credentials")
    return transip.Client(saved, access=access, read_only=False)


def addresses(session: Session) -> str:
    return ", ".join(str(ip) for ip in sorted(session.server_ips, key=lambda ip: (ip.version, ip)))


def wopi_addresses(session: Session) -> list[str]:
    """The addresses Collabora comes from when it asks Nextcloud for a document: this server's own, since it
    goes out to the internet and back in, and the loopback."""
    try:
        own = session.server_ips
    except CtlError:
        own = set()
    return sorted(str(ip) for ip in own) + ["127.0.0.1"]
