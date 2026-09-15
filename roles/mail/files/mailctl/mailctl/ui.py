"""What the user sees and types: messages, prompts, tables and formatting.

Text from users, the database or DNS reaches Rich through text(): it's never read as markup, and control
characters are made visible, so a filter script or a DNS record can't steer the terminal.
"""

import re
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta

from rich import box
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .core.dns_check import DnsRecord, Status
from .core.errors import MailctlError

console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)

_MARKS = {Status.OK: ("✓", "green"), Status.WARN: ("!", "yellow"), Status.FAIL: ("✗", "red")}
_UNITS = ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second"))
# Control characters apart from tab and line feed, including the 8-bit ones terminals also act on.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def interactive() -> bool:
    return sys.stdin.isatty()


def text(value: str, style: str = "") -> Text:
    return Text(_CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match.group()):02x}", value), style=style)


def dim(value: str) -> Text:
    return text(value, "dim")


def mark(status: Status) -> Text:
    symbol, style = _MARKS[status]
    return Text(symbol, style=style)


def yes_no(value: bool) -> Text:
    return mark(Status.OK) if value else dim("–")


def line(*parts: str | Text, indent: int = 0) -> None:
    """Prints a line; str parts are shown as they are. Lines are never broken, so commands in them can be copied
    and Ansible reads them whole."""
    shown = (text(part) if isinstance(part, str) else part for part in parts)
    console.print(Text.assemble(" " * indent, *shown), soft_wrap=True)


def success(message: str, indent: int = 0) -> None:
    line(mark(Status.OK), " ", message, indent=indent)


def warn(message: str, indent: int = 0) -> None:
    line(mark(Status.WARN), " ", message, indent=indent)


def note(message: str, indent: int = 0) -> None:
    line(dim(message), indent=indent)


def error(problem: MailctlError) -> None:
    error_console.print(Text.assemble(mark(Status.FAIL), " ", text(problem.message)), soft_wrap=True)
    if problem.hint:
        error_console.print(dim("  " + problem.hint), soft_wrap=True)


def heading(title: str) -> None:
    line("\n", text(title, "bold"))


def ask(prompt: str, value: str | None, argument: str) -> str:
    """The value given on the command line, or else the user's answer. Without a terminal, a missing value is an error."""
    if value is not None:
        return value
    if not interactive():
        raise MailctlError(
            f"{argument} is missing.", hint="Give it on the command line, or run mailctl in a terminal to be asked for it."
        )
    answer = ""
    while not answer:
        answer = Prompt.ask(text(prompt), console=console).strip()
    return answer


def ask_secret(prompt: str) -> str:
    return Prompt.ask(text(prompt), console=console, password=True)


def decide(question: str, choice: bool | None, flags: str, default: bool = False) -> bool:
    """The yes/no choice given with flags, or else the user's answer."""
    if choice is not None:
        return choice
    if not interactive():
        raise _no_terminal(question, flags)
    return Confirm.ask(text(question), console=console, default=default)


def confirm(question: str, assume_yes: bool) -> None:
    """Stops the command unless the user agrees."""
    if assume_yes:
        return
    if not interactive():
        raise _no_terminal(question, "--yes")
    if not Confirm.ask(text(question), console=console, default=False):
        raise _cancelled()


def confirm_by_typing(name: str, question: str, assume_yes: bool) -> None:
    """Stops the command unless the user types the name: for changes that are hard to undo."""
    if assume_yes:
        return
    if not interactive():
        raise _no_terminal(question, "--yes")
    prompt = Text.assemble(text(question), " Type ", text(name, "bold"), " to confirm")
    if Prompt.ask(prompt, console=console).strip().lower() != name:
        raise _cancelled()


def table(*columns: str | tuple[str, str]) -> Table:
    """A table with the given column names; a column given as (name, "right") is aligned right."""
    result = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False)
    for column in columns:
        name, justify = column if isinstance(column, tuple) else (column, "left")
        result.add_column(name, justify=justify)
    return result


def add_row(to_table: Table, *cells: str | Text) -> None:
    to_table.add_row(*(text(cell) if isinstance(cell, str) else cell for cell in cells))


def records(dns_records: Iterable[DnsRecord], indent: int = 2) -> None:
    """DNS records, with each value on a line of its own so it can be copied in one piece."""
    for record in dns_records:
        line(text(record.type, "bold"), " ", text(record.name, "cyan"), indent=indent)
        line(text(record.value, "green"), indent=indent + 2)


def size(byte_count: int) -> str:
    amount = float(byte_count)
    for unit in ("B", "KB", "MB", "GB"):
        decimals = 0 if unit == "B" else 1
        if round(amount, decimals) < 1024:
            return f"{amount:.{decimals}f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def moment(when: datetime, now: datetime) -> str:
    """A date and time in the server's time zone, and how long ago that was."""
    return f"{when.astimezone():%Y-%m-%d %H:%M} ({_ago(now - when)})"


def period(seconds: int) -> str:
    """A length of time for phrases like 'the last hour' or 'the last 2 days'."""
    unit_seconds, unit = next((length, unit) for length, unit in _UNITS if seconds % length == 0)
    count = seconds // unit_seconds
    return unit if count == 1 else plural(count, unit)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural_form or singular + 's'}"


def _ago(elapsed: timedelta) -> str:
    seconds = int(elapsed.total_seconds())
    for unit_seconds, unit in _UNITS[:-1]:
        if seconds >= unit_seconds:
            return f"{plural(seconds // unit_seconds, unit)} ago"
    return "just now"


def _no_terminal(question: str, flags: str) -> MailctlError:
    return MailctlError(f"mailctl can't ask without a terminal: {question}", hint=f"Add {flags}.")


def _cancelled() -> MailctlError:
    return MailctlError("Cancelled. Nothing was changed.")
