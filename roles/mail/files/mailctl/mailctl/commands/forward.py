"""mailctl forward: add, list and delete forwards."""

from typing import Annotated, Optional

import typer

from .. import ui
from ..core import addresses, forwards, senders
from ..session import open_session
from .shared import DomainFilter, ask_address, domain_filter, group

app = group("Forward mail for an address to other addresses.")

Source = Annotated[Optional[str], typer.Argument(
    metavar="[SOURCE]", help="The forwarded address, on a domain of this server. Asked for when left out.",
    show_default=False)]
Destination = Annotated[Optional[str], typer.Argument(
    metavar="[DESTINATION]", help="The address the mail goes to, on any server. Asked for when left out.",
    show_default=False)]
SendAs = Annotated[Optional[bool], typer.Option(
    "--send-as/--no-send-as",
    help="Whether a destination that's an account on this server may send as the source. Asked for when left out.",
    show_default=False)]


@app.command()
def add(source: Source = None, destination: Destination = None, send_as: SendAs = None) -> None:
    """Forward mail for an address to another address.

    When the destination is an account on this server, it can also be allowed to send as the source.
    A forwarded account keeps a copy in its own mailbox.

    [dim]Example:[/] mailctl forward add sales@example.nl piet@example.nl --send-as
    """
    with open_session() as session:
        source = ask_address(source, "Forward mail for", "SOURCE")
        destination = ask_address(destination, "Forward it to", "DESTINATION")
        forwards.check_new(session.db, source, destination)
        local = addresses.exists(session.db, destination)
        if send_as and not local:
            ui.warn(f"{destination} isn't an account on this server, so it can't send as {source}.")
        allow_send_as = local and ui.decide(f"May {destination} send as {source}?", send_as, "--send-as or --no-send-as")
        with session.db.transaction():
            forwards.add(session.db, source, destination)
            if allow_send_as:
                senders.allow(session.db, destination, source)
        keeps_copy = forwards.exists(session.db, source, source)
    ui.success(f"{source} now forwards to {destination}.")
    if allow_send_as:
        ui.success(f"{destination} may send as {source}.")
    if keeps_copy:
        ui.note(f"{source} keeps a copy in its own mailbox.")


@app.command(name="list")
def list_forwards(domain: DomainFilter = None) -> None:
    """List the forwards, and whether each destination may send as its source.

    [dim]Example:[/] mailctl forward list example.nl
    """
    with open_session() as session:
        domain = domain_filter(session, domain)
        rows = forwards.list_forwards(session.db, domain)
    if not rows:
        ui.note(f"{domain} has no forwards yet." if domain else "There are no forwards yet. Add one with: mailctl forward add")
        return
    table = ui.table("From", "To", "May send as")
    for row in rows:
        ui.add_row(table, row.source, row.destination, ui.yes_no(row.send_as))
    ui.console.print(table)


@app.command()
def delete(source: Source = None, destination: Destination = None) -> None:
    """Stop forwarding an address to another address. The destination may no longer send as the source.

    [dim]Example:[/] mailctl forward delete sales@example.nl piet@example.nl
    """
    with open_session() as session:
        source = ask_address(source, "Stop forwarding mail for", "SOURCE")
        destination = ask_address(destination, "To", "DESTINATION")
        with session.db.transaction():
            could_send_as = forwards.delete(session.db, source, destination)
    ui.success(f"{source} no longer forwards to {destination}.")
    if could_send_as:
        ui.note(f"{destination} may no longer send as {source}.")
