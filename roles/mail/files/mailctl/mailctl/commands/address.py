"""mailctl address: add, list and delete mail accounts, and change their passwords."""

import sys
from datetime import datetime, timezone

from .. import ui
from ..core import activity, addresses, domains, forwards, mailbox, names
from ..core.errors import MailctlError
from ..session import Session, open_session
from . import domain as domain_command
from .shared import (
    Address, DeleteMail, DomainFilter, PasswordStdin, Yes, ask_address, attempt, decide_mail, domain_filter, group,
    warn_about_incoming_forwards,
)

app = group("Add, list and delete mail accounts, and change their passwords.")


@app.command()
def add(address: Address = None, password_stdin: PasswordStdin = False) -> None:
    """Create a mail account with its mailbox.

    Asks for the password, and offers to add the domain when it isn't on this server yet.

    [dim]Example:[/] mailctl address add info@example.nl

    [dim]In a script:[/] mailctl address add info@example.nl --password-stdin < password.txt
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require_available(session.db, address)
        domain = names.split(address)[1]
        new_domain = _domain_to_add(session, domain)
        password = _new_password(password_stdin)
        if new_domain:
            domain_command.create(session, domain)
        home = mailbox.home_dir(session.config, address)
        had_mail = home.exists()
        with session.db.transaction():
            addresses.add(session.db, address, password)
            mailbox.create_maildir(session.config, address)
    ui.success(f"Created {address}.")
    if had_mail:
        ui.note(f"The mail that was kept in {home} is in the account again.")


@app.command(name="list")
def list_addresses(domain: DomainFilter = None) -> None:
    """List the accounts with their disk usage and last login.

    Logins are recorded for IMAP, which webmail uses too; logins that only send mail aren't.

    [dim]Example:[/] mailctl address list example.nl
    """
    with open_session() as session:
        domain = domain_filter(session, domain)
        accounts = addresses.list_addresses(session.db, domain)
        logins = activity.last_login_per_address(session.db, domain)
        usage = {account: mailbox.disk_usage(mailbox.home_dir(session.config, account)) for account in accounts}
    if not accounts:
        ui.note(f"{domain} has no accounts yet." if domain else "There are no accounts yet. Add one with: mailctl address add")
        return
    now = datetime.now(timezone.utc)
    table = ui.table("Address", ("Disk usage", "right"), "Last IMAP login")
    for account in accounts:
        last_login = ui.moment(logins[account], now) if account in logins else ui.dim("never")
        ui.add_row(table, account, ui.size(usage[account]), last_login)
    ui.console.print(table)


@app.command()
def password(address: Address = None, password_stdin: PasswordStdin = False) -> None:
    """Set a new password for an account.

    [dim]Example:[/] mailctl address password info@example.nl
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require(session.db, address)
        addresses.set_password(session.db, address, _new_password(password_stdin))
    ui.success(f"Changed the password of {address}.")


@app.command()
def delete(address: Address = None, delete_mail: DeleteMail = None, yes: Yes = False) -> None:
    """Delete an account with its sender permissions, spam settings and login history.

    Asks for confirmation, and whether to delete its stored mail too. Forwards from the address stay.

    [dim]Example:[/] mailctl address delete info@example.nl --yes --keep-mail
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require(session.db, address)
        ui.confirm(f"Delete {address}?", yes)
        home = mailbox.home_dir(session.config, address)
        delete_mail = decide_mail(home, delete_mail)
        incoming = forwards.to_address(session.db, address)
        outgoing = forwards.from_address(session.db, address)

        with session.db.transaction():
            addresses.delete(session.db, address)
        mailbox.kick(address)
        problems: list[str] = []
        if delete_mail:
            attempt(problems, "Couldn't delete the mail", lambda: mailbox.delete_mail(session.config, home))
    ui.success(f"Deleted {address}.")
    if home.exists() and not delete_mail:
        ui.note(f"Its mail is kept in {home}; adding {address} again brings it back.")
    for problem in problems:
        ui.warn(problem)
    for forward in outgoing:
        ui.note(f"{address} still forwards to {forward.destination}. To stop that: mailctl forward delete {address} {forward.destination}")
    # While the address forwards its mail itself, mail forwarded to it still arrives somewhere.
    if not outgoing:
        warn_about_incoming_forwards(incoming)


def _domain_to_add(session: Session, domain: str) -> bool:
    """Whether the account's domain has to be added first. Asks before saying yes."""
    if domains.exists(session.db, domain):
        return False
    if not ui.interactive():
        raise MailctlError(f"{domain} isn't a domain on this server.", hint=f"Add it first with: mailctl domain add {domain}")
    ui.confirm(f"{domain} isn't a domain on this server yet. Add it too?", assume_yes=False)
    return True


def _new_password(from_stdin: bool) -> str:
    if not ui.interactive():
        if not from_stdin:
            raise MailctlError(
                "The password is missing.", hint="Pipe it in with --password-stdin, or run mailctl in a terminal to be asked."
            )
        new_password = sys.stdin.readline().rstrip("\r\n")
        addresses.check_password(new_password)
        return new_password
    while True:
        new_password = ui.ask_secret("Password")
        try:
            addresses.check_password(new_password)
        except MailctlError as problem:
            ui.warn(problem.message)
            continue
        if ui.ask_secret("Password again") == new_password:
            return new_password
        ui.warn("The passwords don't match. Try again.")
