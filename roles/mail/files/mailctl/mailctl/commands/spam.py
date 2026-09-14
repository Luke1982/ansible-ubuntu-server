"""mailctl spam: SpamAssassin settings for an account, a domain or the whole server."""

from typing import Annotated, Optional

import typer

from .. import ui
from ..core import spam
from ..core.spam import Scope
from ..session import Session, open_session
from .shared import Yes, ask_target, group

app = group("Show and change SpamAssassin settings for an account, a domain or the whole server.")

Target = Annotated[Optional[str], typer.Argument(
    metavar="[TARGET]", help="An address, a domain, or 'server' for the whole server. Asked for when left out.",
    show_default=False)]
Setting = Annotated[Optional[str], typer.Argument(
    metavar="[SETTING]", help=f"One of: {', '.join(spam.SETTINGS)}. Asked for when left out.", show_default=False)]
Value = Annotated[Optional[str], typer.Argument(
    metavar="[VALUE]", help="A number for required_score, an address or pattern for the others. Asked for when left out.",
    show_default=False)]
ValueToRemove = Annotated[Optional[str], typer.Argument(
    metavar="[VALUE]", help="Remove only this value of a list setting.", show_default=False)]

SETTING_PROMPT = f"Setting ({', '.join(spam.SETTINGS)})"


@app.command()
def show(target: Target = None) -> None:
    """Show the spam settings that apply, and where each one is set.

    [dim]Example:[/] mailctl spam show info@example.nl
    """
    with open_session() as session:
        chain = _scope_chain(session, target)
        preferences = spam.show(session.db, chain)
    if not preferences:
        ui.note("No spam settings: SpamAssassin's defaults apply.")
        return
    table = ui.table("Setting", "Value", "Set for", "In effect")
    for preference in preferences:
        cells = [preference.setting, preference.value, preference.scope.label]
        if preference.in_effect:
            ui.add_row(table, *cells, "yes")
        else:
            ui.add_row(table, *map(ui.dim, cells), ui.dim("overridden"))
    ui.console.print(table)


@app.command(name="set")
def set_(target: Target = None, setting: Setting = None, value: Value = None) -> None:
    """Change a spam setting for an account, a domain or the whole server.

    [bold]required_score[/]: the score from which mail counts as spam. SpamAssassin's default is 5.

    [bold]welcomelist_from[/]: senders whose mail never counts as spam, like *@partner.nl. Each value adds to the list.

    [bold]blocklist_from[/]: senders whose mail always counts as spam. Each value adds to the list.

    Settings for an address or a domain apply to mail with one recipient; mail to several recipients at once gets
    the settings of the whole server.

    [dim]Example:[/] mailctl spam set example.nl required_score 4
    """
    with open_session() as session:
        scope = _scope_chain(session, target)[-1]
        setting = spam.known_setting(ui.ask(SETTING_PROMPT, setting, "SETTING"))
        value = spam.normalise(setting, ui.ask("Value", value, "VALUE"))
        with session.db.transaction():
            changed = spam.set_value(session.db, scope, setting, value)
    if spam.SETTINGS[setting].many:
        message = f"Added {value} to {setting}" if changed else f"{value} is already in {setting}"
    else:
        message = f"{setting} is now {value}" if changed else f"{setting} is already {value}"
    (ui.success if changed else ui.note)(f"{message} for {scope.label}.")


@app.command()
def unset(target: Target = None, setting: Setting = None, value: ValueToRemove = None, yes: Yes = False) -> None:
    """Remove a spam setting, or one value of a list setting.

    Removing every value of a list setting asks for confirmation.

    [dim]Example:[/] mailctl spam unset example.nl welcomelist_from *@partner.nl
    """
    with open_session() as session:
        scope = _scope_chain(session, target)[-1]
        setting = ui.ask(SETTING_PROMPT, setting, "SETTING").strip().lower()
        if value is not None and setting in spam.SETTINGS:
            value = spam.normalise(setting, value)
        if value is None:
            count = spam.count_values(session.db, scope, setting)
            if count > 1:
                ui.confirm(f"Remove all {count} values of {setting} for {scope.label}?", yes)
        removed = spam.unset_value(session.db, scope, setting, value)
    if removed:
        ui.success(f"Removed {setting if value is None else f'{value} from {setting}'} for {scope.label}.")
    elif value is None:
        ui.warn(f"{setting} isn't set for {scope.label}.")
    else:
        ui.warn(f"{value} isn't in {setting} for {scope.label}.")


def _scope_chain(session: Session, target: str | None) -> list[Scope]:
    return spam.scope_chain(ask_target(session, target, "Address, domain or 'server'", "TARGET", allow_server=True))
