"""mailctl status: how an account or a domain is doing."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import activity, dkim, dns_check, domains, mailbox, names, system
from ..core.errors import MailctlError
from ..session import Session, open_session
from .shared import ask_target

Target = Annotated[Optional[str], typer.Argument(
    metavar="[ADDRESS_OR_DOMAIN]", help="An e-mail address or a domain. Asked for when left out.", show_default=False)]


def status(target: Target = None) -> None:
    """Show how an account or a domain is doing.

    For an account: its folders, last logins, sending against the limits and recent bounces.

    For a domain: whether its DNS records are right, and its addresses, forwards and disk usage.

    [dim]Example:[/] mailctl status info@example.nl
    """
    with open_session() as session:
        target = ask_target(session, target, "Address or domain", "ADDRESS_OR_DOMAIN")
        if names.is_address(target):
            _account_status(session, target)
        else:
            _domain_status(session, target)


def _section(title: str, show: Callable[[], None]) -> None:
    """Shows one part of the status. A part that fails is a warning, so the other parts still show."""
    ui.heading(title)
    try:
        show()
    except MailctlError as problem:
        ui.warn(problem.message, indent=2)


def _account_status(session: Session, address: str) -> None:
    now = datetime.now().astimezone()
    ui.heading(address)
    _section("Folders", lambda: _folders(mailbox.folders(session.config, address)))
    _section("Last login (IMAP and webmail)", lambda: _logins(activity.last_login_per_service(session.db, address), now))
    _section("Sending", lambda: _sending(session, address, now))


def _folders(folders: list[mailbox.Folder]) -> None:
    table = ui.table("Folder", ("Messages", "right"), ("Size", "right"))
    for folder in folders:
        if folder.alias_of:
            ui.add_row(table, ui.dim(f"{folder.name} → {folder.alias_of}"), "", "")
        else:
            ui.add_row(table, folder.name, str(folder.messages), ui.size(folder.size))
    stored = [folder for folder in folders if not folder.alias_of]
    table.add_section()
    ui.add_row(
        table,
        ui.text("Total", "bold"),
        str(sum(folder.messages for folder in stored)),
        ui.size(sum(folder.size for folder in stored)),
    )
    ui.console.print(table)


def _logins(logins: list[activity.Login], now: datetime) -> None:
    for login in logins:
        ui.line(f"{login.service}: {ui.moment(login.when, now)} from {login.ip}", indent=2)
    if not logins:
        ui.note("None recorded.", indent=2)


def _sending(session: Session, address: str, now: datetime) -> None:
    log = activity.MailLog(session.config.mail_logs)
    sending = activity.sending(address, session.config.limits_for(address), log, now)
    for path in log.unreadable:
        ui.warn(f"Can't read {path}, so these numbers may be incomplete.", indent=2)
    if not log.found and not log.unreadable:
        ui.warn("There is no mail log to count from.", indent=2)
    for window in sending.windows:
        _window(window)
    if sending.refused_recipients:
        refused = ui.plural(sending.refused_recipients, "recipient")
        ui.warn(f"The sending limit refused {refused} in the last day.", indent=2)

    ui.heading("Bounces in the last week")
    if not sending.bounces:
        ui.note("None.", indent=2)
        return
    table = ui.table("When", "Recipient", "Status", "Reason")
    for bounce in sending.bounces:
        ui.add_row(table, ui.moment(bounce.when, now), bounce.recipient, bounce.dsn, bounce.reason)
    ui.console.print(table)


def _window(window: activity.WindowUsage) -> None:
    label = f"Last {ui.period(window.seconds)}: "
    if window.limit is None:
        ui.line(label + ui.plural(window.used, "recipient"), indent=2)
        return
    style = "red" if window.used >= window.limit else "yellow" if window.used >= 0.8 * window.limit else "green"
    ui.line(label, ui.text(f"{window.used} of {window.limit}", style), " recipients", indent=2)


def _domain_status(session: Session, domain: str) -> None:
    ui.heading(domain)
    _section("DNS records", lambda: _dns_checks(session, domain))
    summary = domains.get(session.db, domain)
    usage = mailbox.disk_usage(mailbox.domain_dir(session.config, domain))
    addresses_text = ui.plural(summary.addresses, "address", "addresses")
    ui.line("")
    ui.line(f"{addresses_text}, {ui.plural(summary.forwards, 'forward')}, {ui.size(usage)} of mail", indent=2)


def _dns_checks(session: Session, domain: str) -> None:
    dkim_value = None
    if dkim.has_key(session.config, domain):
        try:
            dkim_value = dkim.record_value(session.config, domain)
        except MailctlError as problem:
            ui.warn(f"Couldn't read the DKIM key: {problem.message}", indent=2)
    with ui.console.status("Checking the DNS records…"):
        checks = dns_check.check_domain(
            domain, server_ips=system.server_ips(), dkim_value=dkim_value, resolver=dns_check.SystemResolver()
        )
    width = max(len(check.name) for check in checks)
    for check in checks:
        ui.line(ui.mark(check.status), " ", ui.text(check.name.ljust(width), "bold"), "  ", check.detail, indent=2)
        ui.records(check.fixes, indent=4)
