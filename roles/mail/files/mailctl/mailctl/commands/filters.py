"""mailctl filters: the Sieve filters that sort an account's mail."""

from typing import Annotated, Optional

import typer
from rich.panel import Panel
from rich.text import Text

from .. import ui
from ..core import addresses, mailbox, names, sieve, sogofilters, transfer
from ..core.errors import MailctlError
from ..session import Session, open_session
from .dns import DryRun
from .shared import Address, Yes, activate_imported, ask_address, attempt, group, into_webmail, would_be_in_webmail

app = group("Show the mail filters (Sieve scripts) of an account.")

ServerFile = Annotated[Optional[str], typer.Argument(
    metavar="[FILE]", help="The JSON file of the mail server. Asked for when left out.", show_default=False)]
OnlyAddress = Annotated[Optional[str], typer.Argument(
    metavar="[ADDRESS]", help="Only this account's filters. By default every account in the file.",
    show_default=False)]
Replace = Annotated[bool, typer.Option(
    "--replace", help="Overwrite a filter an account already has under the same name.")]


@app.command(name="import")
def import_filters(file: ServerFile = None, address: OnlyAddress = None, replace: Replace = False,
                   dry_run: DryRun = False, yes: Yes = False) -> None:
    """Import the filters from a mail server file, and nothing else from it.

    The file is the one [bold]mailctl export[/] writes on the other server, or export-mailserver.sh on a server
    without mailctl; only its "sieve" part is read, so the accounts, forwards and spam settings in it are left
    alone. The rules go into webmail's filter list, where filters are edited; a rule webmail's editor can't
    express stays in a filter of its own, which runs while webmail has no filters of that account.

    Every account has to be on this server already, and a filter it already has under the same name is left as it
    is unless --replace.

    [dim]Example:[/] mailctl filters import mailserver.json

    [dim]One account:[/] mailctl filters import mailserver.json info@example.nl
    """
    path = ui.ask_file("The JSON file of the mail server", file, "FILE")
    wanted = names.address(address) if address else None
    found = [one for one in transfer.read(path).filters if not wanted or one.address == wanted]
    if not found:
        ui.note(f"{path} holds no filters{f' of {wanted}' if wanted else ''}.")
        return
    with open_session() as session:
        per_account = _per_account(session, found, replace)
        if not per_account:
            return
        problems = sieve.check([sieve.Script(one.name, one.content, one.active)
                                for scripts in per_account.values() for one in scripts])
        if problems:
            raise MailctlError(f"{ui.plural(len(problems), 'filter')} in {path} can't be read, so nothing was "
                               "imported:\n" + "\n".join(f"  {problem}" for problem in problems),
                               hint="Fix them in the file, or leave them out, and try again.")
        count = sum(len(scripts) for scripts in per_account.values())
        ui.line(f"To import from {path}: {ui.plural(count, 'filter')} of "
                f"{ui.plural(len(per_account), 'account')}.")
        if dry_run:
            for account, scripts in per_account.items():
                ui.line(f"Filters of {account}:")
                would_be_in_webmail(scripts)
            ui.note("The file is fine. Nothing was changed (--dry-run).")
            return
        ui.confirm("Import them?", yes)
        warnings: list[str] = []
        for account, scripts in per_account.items():
            attempt(warnings, f"Couldn't import the filters of {account}",
                    lambda account=account, scripts=scripts: _put_filters(session, account, scripts))
    for warning in warnings:
        ui.warn(warning)


def _per_account(session: Session, found: list[transfer.Filter], replace: bool) -> dict[str, list[transfer.Filter]]:
    """The file's filters per account, leaving out the accounts this server doesn't have and the filters they
    already have under the same name."""
    per_account: dict[str, list[transfer.Filter]] = {}
    for script in found:
        per_account.setdefault(script.address, []).append(script)
    wanted: dict[str, list[transfer.Filter]] = {}
    for account, scripts in per_account.items():
        if not addresses.exists(session.db, account):
            ui.warn(f"{account} isn't an account on this server, so its {ui.plural(len(scripts), 'filter')} "
                    f"{'is' if len(scripts) == 1 else 'are'} left out. Create it with: mailctl address add {account}")
            continue
        here = {script.name for script in mailbox.sieve_scripts(account)}
        keeping = [script for script in scripts if script.name in here] if not replace else []
        for script in keeping:
            ui.note(f"{account} already has a filter called {script.name}, left as it is. "
                    f"Overwrite it with: mailctl filters import --replace")
        rest = [script for script in scripts if script not in keeping]
        if rest:
            wanted[account] = rest
    if not wanted:
        ui.note("There is nothing to import.")
    return wanted


def _put_filters(session: Session, address: str, scripts: list[transfer.Filter]) -> None:
    """The rules go into webmail's filters, where they are edited. When none of them fits, the filter that was
    active on the other server runs here too, which stops the one the account has now."""
    was_active = next((script.name for script in mailbox.sieve_scripts(address) if script.active), None)
    for script in scripts:
        mailbox.put_sieve(address, script.name, script.content)
    ui.success(f"Imported {ui.plural(len(scripts), 'filter')} of {address}.")
    if not into_webmail(session, address, scripts):
        activate_imported(address, scripts, was_active)


@app.command()
def adopt(address: Address = None, dry_run: DryRun = False) -> None:
    """Put the filters an account already has in webmail's list, where filters are edited.

    For accounts whose filters came from another server before mailctl put them in webmail's list, or that were
    made by hand. Webmail's own filter is left as it is, and a rule its editor can't express stays in the script
    it is in. Without an address, every account on this server.

    [dim]Example:[/] mailctl filters adopt info@example.nl

    [dim]Every account:[/] mailctl filters adopt
    """
    with open_session() as session:
        if address:
            wanted = [names.address(address)]
            addresses.require(session.db, wanted[0])
        else:
            wanted = addresses.list_addresses(session.db)
        for account in wanted:
            scripts = [(script.name, script.content) for script in mailbox.sieve_scripts(account)
                       if script.name != sogofilters.SCRIPT]
            if not scripts:
                continue
            if dry_run:
                filters, left = [], []
                for name, content in scripts:
                    found, reasons = sogofilters.translate(content, name)
                    filters += found
                    left += reasons
                ui.line(f"{account}: would put {ui.plural(len(filters), 'rule')} in webmail's filters.")
                for line in left:
                    ui.warn(f"Not in webmail: {line}", indent=2)
                continue
            adopted = sogofilters.adopt(session.db, account, scripts)
            if adopted.added:
                ui.success(f"{account}: {ui.plural(len(adopted.added), 'rule')} in webmail's filters, which run "
                           f"from webmail's own filter now.")
            for line in adopted.left:
                ui.warn(f"Not in webmail: {line}", indent=2)
    if dry_run:
        ui.note("Nothing was changed (--dry-run).")


@app.command()
def show(address: Address = None) -> None:
    """Show an account's own filters, and the server-wide filters that run after them.

    [dim]Example:[/] mailctl filters show info@example.nl
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require(session.db, address)
        own_scripts = mailbox.sieve_scripts(address)
        server_scripts = mailbox.server_scripts(session.config)
    if not own_scripts:
        ui.note(f"{address} has no filters of its own.")
    for script in own_scripts:
        state = ui.text("active", "green") if script.active else ui.dim("inactive")
        title = Text.assemble(ui.text(script.name), " (", state, ")")
        ui.console.print(Panel(ui.text(script.content.rstrip()), title=title, title_align="left"))
    for script in server_scripts:
        title = ui.text(f"{script.name} (runs for every account, after its own filters)")
        ui.console.print(Panel(ui.text(script.content.rstrip()), title=title, title_align="left", border_style="dim"))
    if len(own_scripts) > 1:
        # Which is what webmail's own filter does to an imported one, and the other way round.
        ui.note("Only the active filter runs. The others are kept and do nothing.")

