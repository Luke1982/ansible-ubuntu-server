"""mailctl spam: SpamAssassin settings for an account, a domain or the whole server."""

import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import addresses, spam, spamlearn
from ..core.spam import Scope
from ..session import Session, open_session
from .dns import DryRun
from .shared import Address, Yes, ask_address, ask_target, group

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
Everything = Annotated[bool, typer.Option(
    "--all", "-a", help="Learn from every message again, not only from the ones since the last run.")]


@app.command()
def learn(everything: Everything = False, dry_run: DryRun = False) -> None:
    """Teach SpamAssassin from the mail people file: their Junk folders are spam, their inboxes are ham.

    SpamAssassin only learns what it is shown, and this shows it what everybody here decided themselves. It reads
    the messages that arrived or were moved since the last run, so it stays quick; --all reads every message again
    (sa-learn skips the ones it has already seen). A message nobody has opened yet is left out of the inbox side:
    it may be spam that hasn't been filed.

    The playbook runs this every night (systemctl status mailctl-spam-learn.timer).

    [dim]Example:[/] mailctl spam learn

    [dim]Everything there is:[/] mailctl spam learn --all
    """
    started = time.time()
    with open_session() as session:
        state = session.config.spam_learn_state
        known = spamlearn.read(state)
        accounts = addresses.list_addresses(session.db)
        folders = spamlearn.folders(session.config, accounts)
        with ui.console.status("Looking for mail to learn from…"):
            found = {folder: spamlearn.messages(folder, 0.0 if everything else known.since(folder.address))
                     for folder in folders}
    per_account = _per_account(found)
    spam_count = sum(len(spam_here) for spam_here, _ in per_account.values())
    ham_count = sum(len(ham_here) for _, ham_here in per_account.values())
    if not spam_count and not ham_count:
        learned_before = bool(known.last_run or known.accounts)
        ui.success("Nothing new to learn from." if learned_before else "There is no mail to learn from.")
        return
    counted = f"{ui.plural(spam_count, 'spam message')} and {ui.plural(ham_count, 'other message')}"
    if dry_run:
        ui.line(f"Would learn from {counted}, of {ui.plural(len(accounts), 'account')}.")
        ui.note("Nothing was learned (--dry-run).")
        return
    problems = []
    learned_spam = learned_ham = 0
    # Account by account, each one written down as soon as it is learned: a run that is stopped halfway then
    # keeps what it did instead of starting the whole server over the next night.
    with ui.console.status(f"Learning from {counted}…"):
        for address, (spam_here, ham_here) in per_account.items():
            if not spam_here and not ham_here:
                continue
            learned, trouble = spamlearn.learn(spam_here, spam=True)
            learned_spam += learned
            problems += trouble
            learned, trouble = spamlearn.learn(ham_here, spam=False)
            learned_ham += learned
            problems += trouble
            spamlearn.remember_account(address, started, state)
        spamlearn.sync()
    spamlearn.remember(started, state)
    ui.success(f"Learned from {ui.plural(learned_spam, 'new spam message')} and "
               f"{ui.plural(learned_ham, 'new other message')}; the rest was known already.")
    for problem in problems:
        ui.warn(problem)


def _per_account(found: dict[spamlearn.Folder, list[Path]]) -> dict[str, tuple[list[Path], list[Path]]]:
    """The messages to learn from, per account: the spam ones and the others."""
    per_account: dict[str, tuple[list[Path], list[Path]]] = {}
    for folder, paths in found.items():
        spam_here, ham_here = per_account.setdefault(folder.address, ([], []))
        (spam_here if folder.spam else ham_here).extend(paths)
    return per_account


@app.command()
def skip(address: Address = None) -> None:
    """Leave the mail that is in a mailbox now out of the learning.

    A mailbox that has just been copied from another server holds nothing but mail that looks as if it
    arrived this minute, and the next nightly run would hand every message of it to SpamAssassin: hours of
    work for a filter that was taught on the other server already. This says that what is there now needn't
    be learned from. Mail that comes in after it still is.

    [dim]Example:[/] mailctl spam skip info@example.nl
    """
    with open_session() as session:
        address = ask_address(address)
        addresses.require(session.db, address)
        spamlearn.remember_account(address, time.time(), session.config.spam_learn_state)
    ui.success(f"The mail {address} holds now won't be learned from; what comes in after this will.")


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
