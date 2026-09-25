"""mailctl doctor: whether the server and its domains are set up for mail."""

from datetime import datetime
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import domains
from ..core.dns_check import Check, Status
from ..session import open_session
from . import checks
from .shared import domain_filter

OnlyDomain = Annotated[Optional[str], typer.Argument(
    metavar="[DOMAIN]", help="Only check this domain. By default every domain is checked.", show_default=False)]
Everything = Annotated[bool, typer.Option(
    "--all", "-a", help="Show every check, not only the ones that need attention.")]


def doctor(domain: OnlyDomain = None, everything: Everything = False) -> None:
    """Check that this server and its domains are set up for mail.

    The server: its hostname and reverse DNS, which receiving servers check, and its certificate.

    Every domain, or the one given: the MX, SPF, DKIM, DMARC and SRV records, and whether the certificate includes
    its mail host. Only what needs attention is shown; --all shows every check. Ends with exit status 1 when there
    is a problem.

    [dim]Example:[/] mailctl doctor example.nl
    [dim]Everything it checked:[/] mailctl doctor --all
    """
    now = datetime.now().astimezone()
    found: list[Check] = []
    with open_session() as session:
        names = [domain_filter(session, domain)] if domain else [row.name for row in domains.list_domains(session.db)]
        with ui.console.status("Checking this server…"):
            facts = checks.server(session)
            server_checks = checks.server_checks(facts, now)
        _section(f"Server {facts.hostname}", server_checks, everything)
        found += server_checks
        for name in names:
            with ui.console.status(f"Checking {name}…"):
                domain_checks = checks.domain_checks(session, facts, name)
            _section(name, domain_checks, everything)
            found += domain_checks
    if not names:
        ui.note("\nThere are no domains yet.")
    _summary(found)


def _section(title: str, found: list[Check], everything: bool) -> None:
    """The checks of the server or a domain. One line for a domain with nothing to report, so a long list stays
    a list of the things that need doing."""
    if everything or any(check.status is not Status.OK for check in found):
        ui.heading(title)
        checks.show(found, everything)
    else:
        ui.line(ui.mark(Status.OK), " ", ui.text(title, "bold"), "  everything is set up.")


def _summary(found: list[Check]) -> None:
    problems = sum(check.status is Status.FAIL for check in found)
    warnings = sum(check.status is Status.WARN for check in found)
    ui.line("")
    if problems:
        also = f" and {ui.plural(warnings, 'warning')}" if warnings else ""
        ui.line(ui.mark(Status.FAIL), " ", f"Found {ui.plural(problems, 'problem')}{also}.")
        raise typer.Exit(1)
    if warnings:
        ui.warn(f"No problems, but {ui.plural(warnings, 'warning')}.")
    else:
        ui.success("Everything is set up.")

