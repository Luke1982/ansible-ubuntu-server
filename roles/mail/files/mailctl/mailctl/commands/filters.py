"""mailctl filters: the Sieve filters that sort an account's mail."""

from rich.panel import Panel
from rich.text import Text

from .. import ui
from ..core import addresses, mailbox, names, sogofilters
from ..session import open_session
from .dns import DryRun
from .shared import Address, ask_address, group

app = group("Show the mail filters (Sieve scripts) of an account.")


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

