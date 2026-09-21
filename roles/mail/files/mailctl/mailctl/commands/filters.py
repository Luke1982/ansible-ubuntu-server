"""mailctl filters: the Sieve filters that sort an account's mail."""

from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel
from rich.text import Text

from .. import ui
from ..core import addresses, mailbox, sieve
from ..core.errors import MailctlError
from ..session import open_session
from .shared import Address, ask_address, group

Archive = Annotated[Path, typer.Argument(
    metavar="ARCHIVE", help="A tar archive of the account's sieve directory from the other server.",
    exists=True, dir_okay=False, readable=True)]
Replace = Annotated[bool, typer.Option("--replace", help="Overwrite filters the account already has.")]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Only show what would be imported; import nothing.")]

app = group("Show the mail filters (Sieve scripts) of an account.")


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


@app.command(name="import")
def import_scripts(address: Address = None, archive: Archive = None, replace: Replace = False,
                   dry_run: DryRun = False) -> None:
    """Import an account's filters from a tar archive of its sieve directory on another server.

    Takes the .sieve files in the archive and makes active the one .dovecot.sieve named there. Filters the account
    already has are kept, unless you add --replace.

    [dim]Example:[/] mailctl filters import info@example.nl sieve.tar.gz
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require(session.db, address)
        scripts, skipped = sieve.read_archive(archive)
        if not scripts:
            raise MailctlError(f"{archive} holds no filters.",
                               hint="It should hold the account's .sieve files, as its sieve directory has them.")
        existing = {script.name for script in mailbox.sieve_scripts(address)}
        for line in skipped:
            ui.warn(f"Left out {line}")
        imported = []
        for script in scripts:
            if script.name in existing and not replace:
                ui.note(f"{address} already has a filter {script.name}; --replace overwrites it.")
                continue
            imported.append(script)
            if dry_run:
                ui.line(f"Would import {script.name}{' and make it active' if script.active else ''}.")
                continue
            mailbox.put_sieve(address, script.name, script.content)
            ui.success(f"Imported {script.name} for {address}.")
            if script.active:
                mailbox.activate_sieve(address, script.name)
                ui.success(f"{script.name} is the active filter.")
    if not imported:
        ui.note("Nothing was imported.")
    elif dry_run:
        ui.note("Nothing was changed (--dry-run).")
    elif not any(script.active for script in imported):
        ui.note(f"None of them is active. See them with: mailctl filters show {address}")
