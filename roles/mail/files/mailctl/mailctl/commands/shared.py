"""Arguments, options and steps that several commands share."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import addresses, autodiscover, dkim, dns_check, domains, names, spam, system
from ..core.dns_check import DnsRecord
from ..core.errors import MailctlError
from ..core.forwards import Forward
from ..session import Session

# Typer 0.9 (Ubuntu 24.04) needs Optional[...]: it doesn't understand "str | None" in command parameters.
# Metavars are in brackets, since mailctl asks for arguments that are left out.
Domain = Annotated[Optional[str], typer.Argument(
    metavar="[DOMAIN]", help="A domain, like example.nl. Asked for when left out.", show_default=False)]
DomainFilter = Annotated[Optional[str], typer.Argument(
    metavar="[DOMAIN]", help="Only show this domain.", show_default=False)]
Address = Annotated[Optional[str], typer.Argument(
    metavar="[ADDRESS]", help="An e-mail address, like info@example.nl. Asked for when left out.", show_default=False)]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")]
DeleteMail = Annotated[Optional[bool], typer.Option(
    "--delete-mail/--keep-mail", help="Delete or keep the stored mail. Asked for when left out.", show_default=False)]
PasswordStdin = Annotated[bool, typer.Option(
    "--password-stdin", help="Read the password from standard input, for scripts. In a terminal it's asked for.")]


def group(description: str) -> typer.Typer:
    return typer.Typer(help=description, no_args_is_help=True, rich_markup_mode="rich")


def ask_domain(value: str | None) -> str:
    return names.domain(ui.ask("Domain", value, "DOMAIN"))


def ask_address(value: str | None, prompt: str = "Address", argument: str = "ADDRESS") -> str:
    return names.address(ui.ask(prompt, value, argument))


def ask_target(session: Session, value: str | None, prompt: str, argument: str, allow_server: bool = False) -> str:
    """An account or a domain on this server, or 'server' when allowed."""
    target = ui.ask(prompt, value, argument).strip().lower()
    if allow_server and target == spam.SERVER:
        return target
    if names.is_address(target):
        target = names.address(target)
        addresses.require(session.db, target)
    else:
        target = names.domain(target)
        domains.require(session.db, target)
    return target


def domain_filter(session: Session, value: str | None) -> str | None:
    """The domain a listing is limited to, if any, after checking it's on this server."""
    if value is None:
        return None
    domain = names.domain(value)
    domains.require(session.db, domain)
    return domain


def decide_mail(mail: Path, choice: bool | None) -> bool:
    """Whether to delete the stored mail in a folder. Not asked when there is none."""
    return mail.exists() and ui.decide(f"Delete the stored mail in {mail} too?", choice, "--delete-mail or --keep-mail")


def attempt[T](problems: list[str], failure: str, step: Callable[[], T]) -> T | None:
    """Runs a step that follows a saved change. The change stands, so a failure is added to problems, to show as a
    warning, instead of stopping the command."""
    try:
        return step()
    except MailctlError as problem:
        problems.append(f"{failure}: {problem.message}")
        return None


def recommended_records(session: Session, domain: str, problems: list[str]) -> list[DnsRecord]:
    """The DNS records the domain needs. What can't be read is added to problems and its records left out."""
    dkim_value = (
        attempt(problems, "Couldn't read the DKIM key", lambda: dkim.record_value(session.config, domain))
        if dkim.has_key(session.config, domain) else None
    )
    server_ips = attempt(problems, "Couldn't read this server's addresses", system.server_ips) or set()
    records = dns_check.recommended_records(domain, server_ips, dkim_value)
    if attempt(problems, "Couldn't read OpenLiteSpeed's config", lambda: autodiscover.has_site(session.config, domain)):
        records += dns_check.autodetect_records(domain, server_ips)
    return records


def warn_about_incoming_forwards(incoming: list[Forward]) -> None:
    for forward in incoming:
        ui.warn(f"{forward.source} still forwards to {forward.destination}, which is no longer on this server.")
