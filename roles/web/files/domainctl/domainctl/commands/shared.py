"""Arguments and steps that several commands share."""

from typing import Annotated, Optional

import typer

from serverctl import names, ui
from serverctl.errors import CtlError

from ..config import Config
from ..core import sites

# Typer 0.9 (Ubuntu 24.04) needs Optional[...]: it doesn't understand "str | None" in command parameters.
# Metavars are in brackets, since domainctl asks for arguments that are left out.
Domain = Annotated[Optional[str], typer.Argument(
    metavar="[DOMAIN]", help="A domain, like example.nl. Asked for when left out.", show_default=False)]
User = Annotated[Optional[str], typer.Argument(
    metavar="[USER]", help="The site's Linux user. Asked for when left out.", show_default=False)]
UserFilter = Annotated[Optional[str], typer.Argument(
    metavar="[USER]", help="Only show this site.", show_default=False)]
UserOption = Annotated[Optional[str], typer.Option(
    "--user", "-u", help="The Linux user to make for the site. Taken from the domain when left out.",
    show_default=False)]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")]
NoDns = Annotated[bool, typer.Option(
    "--no-dns", help="Don't offer to publish a missing record at TransIP; only look the names up.")]
ExistingUser = Annotated[bool, typer.Option(
    "--existing-user", help="Use a Linux user that is already there instead of making one.")]
Purge = Annotated[bool, typer.Option(
    "--purge", help="Also delete the Linux user and its home directory, with the site's files.")]


def group(description: str) -> typer.Typer:
    return typer.Typer(help=description, no_args_is_help=True, rich_markup_mode="rich")


def ask_domain(value: str | None) -> str:
    """A domain name, up to DNS's own limit: domainctl has no database column to fit it in."""
    return names.domain(ui.ask("Domain", value, "DOMAIN"), names.MAX_DNS_NAME_LENGTH)


def ask_user(value: str | None) -> str:
    return names.user_name(ui.ask("Site user", value, "USER"))


def find_site(config: Config, user: str) -> sites.Site:
    found = sites.find(config, user)
    if not found:
        known = ", ".join(site.user for site in sites.list_sites(config))
        raise CtlError(f"There is no site {user} on this server.",
                       hint=f"The sites are: {known}." if known else "Add one with: domainctl add DOMAIN")
    return found


def www(domain: str) -> str:
    return f"www.{domain}"
