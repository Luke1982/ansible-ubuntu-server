"""What the user sees and types: messages, prompts, tables and formatting.

Text from users, the database or DNS reaches Rich through text(): it's never read as markup, and control
characters are made visible, so a filter script or a DNS record can't steer the terminal.
"""

import glob
import os
import re
import sys
import termios
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

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


def ask_file(prompt: str, value: str | None, argument: str) -> Path:
    """Like ask(), for a file name, which the Tab key completes while it's typed."""
    if value is not None or not interactive():
        return Path(ask(prompt, value, argument))
    with _file_completion():
        return Path(ask(prompt, value, argument)).expanduser()


def complete_file(typed: str) -> list[str]:
    """The file and folder names that start with what was typed; folders end in a slash, to go on typing into."""
    matches = sorted(glob.glob(glob.escape(os.path.expanduser(typed)) + "*"))
    return [match + "/" if os.path.isdir(match) else match for match in matches]


@contextmanager
def _file_completion() -> Iterator[None]:
    """Completes file names on Tab while the block asks for one."""
    try:
        import readline
    except ImportError:  # Python without readline: the name is typed out
        yield
        return
    matches: list[str] = []

    def complete(typed: str, state: int) -> str | None:
        """Readline asks for the matches one by one, with state counting up from 0 for each Tab."""
        if state == 0:
            matches[:] = complete_file(typed)
        return matches[state] if state < len(matches) else None

    completer, delimiters = readline.get_completer(), readline.get_completer_delims()
    readline.set_completer(complete)
    readline.set_completer_delims("\n")  # the whole line is the file name, spaces and all
    readline.parse_and_bind("tab: complete")
    try:
        yield
    finally:
        readline.set_completer(completer)
        readline.set_completer_delims(delimiters)


def ask_secret(prompt: str) -> str:
    return Prompt.ask(text(prompt), console=console, password=True)


def ask_secret_lines(prompt: str, last_line: str) -> str:
    """Pasted lines that aren't shown, like a private key, up to the line that holds last_line: also when the
    line breaks got lost in copying, so it's all one line."""
    console.print(text(prompt))
    descriptor = sys.stdin.fileno()
    shown = termios.tcgetattr(descriptor)
    hidden = termios.tcgetattr(descriptor)
    hidden[3] &= ~termios.ECHO
    lines: list[str] = []
    termios.tcsetattr(descriptor, termios.TCSADRAIN, hidden)
    try:
        while not lines or last_line not in lines[-1]:
            pasted = sys.stdin.readline()
            if not pasted:
                break
            lines.append(pasted)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, shown)
    return "".join(lines)


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


def record_change(added: bool, record: DnsRecord, indent: int = 2) -> None:
    """A DNS record that is added (+) or removed (-), laid out like records()."""
    sign, style = ("+", "green") if added else ("-", "red")
    line(text(sign, style), " ", text(record.type, "bold"), " ", text(record.name, "cyan"), indent=indent)
    line(text(record.value, style), indent=indent + 4)


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
    count, unit = _in_units(seconds)
    return unit if count == 1 else plural(count, unit)


def duration(seconds: int) -> str:
    """A length of time like '1 hour' or '5 minutes'."""
    return plural(*_in_units(seconds))


def _in_units(seconds: int) -> tuple[int, str]:
    """The length of time in the largest unit it's a whole number of."""
    unit_seconds, unit = next((length, unit) for length, unit in _UNITS if seconds % length == 0)
    return seconds // unit_seconds, unit


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
