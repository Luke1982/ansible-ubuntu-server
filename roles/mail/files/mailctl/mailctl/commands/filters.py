"""mailctl filters: the Sieve filters that sort an account's mail."""

from rich.panel import Panel
from rich.text import Text

from .. import ui
from ..core import addresses, mailbox
from ..session import open_session
from .shared import Address, ask_address, group

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
