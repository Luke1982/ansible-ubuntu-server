"""Arguments, options and steps that several commands share."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import addresses, autodiscover, dkim, dns_check, domains, mailbox, names, sogofilters, spam, system
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


def into_webmail(session: Session, address: str, scripts: list) -> bool:
    """Puts the imported rules in webmail's filter list, where filters are edited. False when none of them fits,
    and the imported filter is left to run as it is."""
    adopted = sogofilters.adopt(session.db, address, [(script.name, script.content) for script in scripts])
    for line in adopted.left:
        ui.warn(f"Not in webmail: {line}")
    if not adopted.added:
        # Nothing said and nothing to add: the scripts hold no rules at all, so there is nothing to run.
        return not adopted.left
    ui.success(f"{ui.plural(len(adopted.added), 'rule')} of them "
               f"{'is' if len(adopted.added) == 1 else 'are'} in webmail's filters now, which is where they are "
               f"edited from now on. They run already.")
    if adopted.already:
        ui.note(f"Webmail already had {ui.plural(adopted.already, 'filter')} of the same name, left as "
                f"{'it was' if adopted.already == 1 else 'they were'}.")
    if adopted.left:
        ui.note("The rules it doesn't have stay in the imported filter, which doesn't run while webmail's filters "
                f"do. See them with: mailctl filters show {address}")
    return True


def would_be_in_webmail(scripts: list) -> None:
    for script in scripts:
        filters, left = sogofilters.translate(script.content, script.name)
        ui.line(f"Would put {ui.plural(len(filters), 'rule')} of {script.name} in webmail's filters.", indent=2)
        for line in left:
            ui.warn(f"Not in webmail: {line}", indent=2)


def activate_imported(address: str, scripts: list, was_active: str | None) -> None:
    """When nothing went into webmail, the imported filter is the one that runs, as on the other server."""
    for script in scripts:
        if not script.active:
            continue
        mailbox.activate_sieve(address, script.name)
        ui.success(f"{script.name} is the active filter.")
        if was_active and was_active != script.name:
            # An account has one active filter, and webmail keeps its own in a filter of its own.
            ui.warn(f"{was_active} was the active filter and stops running: an account has one. Saving filters in "
                    f"webmail makes {was_active} the active one again, and then the imported filters stop instead.")
