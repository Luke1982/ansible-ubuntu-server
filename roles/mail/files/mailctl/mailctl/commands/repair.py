"""mailctl repair: do what a domain still needs, one step at a time.

'doctor' says what is wrong; this offers to put each of it right, with the same commands a person would run.
"""

from collections.abc import Callable
from datetime import datetime

from .. import ui
from ..core import domains
from ..core.dns_check import Status
from ..core.errors import MailctlError
from ..session import open_session
from . import autodiscover as autodiscover_command
from . import certificate as certificate_command
from . import checks
from . import dns as dns_command
from . import webmail as webmail_command
from .shared import Domain, Yes, ask_domain, domain_filter

# Which check leads to which command, in the order they are worth doing.
_STEPS = (
    ("MX", "the DNS records"), ("SPF", "the DNS records"), ("DKIM", "the DNS records"),
    ("DMARC", "the DNS records"), ("SRV", "the DNS records"),
    ("Certificate", "the certificate"), ("Webmail", "webmail"), ("Autodiscover", "autodiscover"),
)


def repair(domain: Domain = None, yes: Yes = False) -> None:
    """Put right what a domain still needs: its DNS records, the certificate, webmail, autodiscover.

    Checks the domain as 'doctor' does and offers each step that would fix what it found, so there is one command
    to run after moving a domain here. Every step asks first.

    [dim]Example:[/] mailctl repair example.nl
    [dim]Without questions:[/] mailctl repair example.nl --yes
    """
    now = datetime.now().astimezone()
    with open_session() as session:
        domain = domain_filter(session, ask_domain(domain))
        with ui.console.status(f"Checking {domain}…"):
            facts = checks.server(session)
            found = checks.domain_checks(session, facts, domain, now)
    checks.show(found, everything=False)
    todo = _what_to_do(found)
    if not todo:
        ui.success(f"{domain} needs nothing: everything is set up.")
        return
    if "the DNS records" in todo and _agreed("Publish the DNS records at TransIP?", yes):
        _step(lambda: dns_command.publish(domain, dry_run=False, yes=yes))
    if "the certificate" in todo and _agreed(f"Put mail.{domain} in this server's certificate?", yes):
        _step(lambda: certificate_command.sync(dry_run=False, yes=True))
    if "webmail" in todo and _agreed(f"Give {domain} webmail?", yes):
        _step(lambda: webmail_command.sync(no_dns=False))
    if "autodiscover" in todo and _agreed(f"Set up autoconfig and autodiscover for {domain}?", yes):
        _step(lambda: autodiscover_command.publish(domain, dry_run=False, yes=True, no_dns=False))
    ui.note(f"See how it stands now with: mailctl doctor {domain}")


def _step(run: Callable[[], None]) -> None:
    """One step. A step that can't be done says why and the next one is still offered: they are separate jobs,
    and the one that failed is often waiting for something outside this server."""
    try:
        run()
    except MailctlError as problem:
        ui.warn(problem.message)
        if problem.hint:
            ui.note(problem.hint)


def _what_to_do(found: list) -> list[str]:
    """The steps worth offering, in order, without repeating one."""
    needs = {check.name for check in found if check.status is not Status.OK}
    steps = []
    for name, step in _STEPS:
        if name in needs and step not in steps:
            steps.append(step)
    return steps


def _agreed(question: str, yes: bool) -> bool:
    return ui.decide(question, True if yes else None, "--yes", default=True)
