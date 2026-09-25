"""Sieve filters: their name, and whether Dovecot can compile them.

A filter only counts as one when sievec, the compiler Dovecot itself uses, accepts it, so everything that brings
filters to an account checks them here first: an account is never left with a filter that fails at delivery.
"""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import system
from .errors import MailctlError

MAX_SCRIPT = 1 << 20  # bytes; a filter is text, and a bigger file isn't one
# Sieve script names, as ManageSieve allows them, kept to what a file name can hold.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")


@dataclass(frozen=True)
class Script:
    name: str
    content: str
    active: bool


# doveadm sieve get writes this field header before the script, whichever output format it's given.
DOVEADM_HEADER = "sieve script:\n"


def without_doveadm_header(content: str) -> str:
    """The script without doveadm's header, also in files exported before mailctl left it out: no Sieve script
    starts with it."""
    return content.removeprefix(DOVEADM_HEADER)


def get_script(address: str, name: str) -> str:
    return without_doveadm_header(system.run("doveadm", "sieve", "get", "-u", address, name))


def valid_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def check(scripts: list[Script]) -> list[str]:
    """What sievec, which compiles scripts the way Dovecot does, finds wrong with them: one line per script."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="mailctl-sieve-") as directory:
        for script in scripts:
            source = Path(directory) / f"{script.name}.sieve"
            source.write_text(script.content)
            complaint = system.run_check("sievec", str(source), str(source.with_suffix(".svbin")))
            if complaint is not None:
                problems.append(f"{script.name}: {' '.join(complaint.replace(str(source), script.name).split())}")
    return problems


# What sieve-filter prints above each message it looks at, and the lines of its report that matter.
_FILTERING = re.compile(r"^>> Filtering message:", re.MULTILINE)
_STORED = re.compile(r"\*\s*store message in folder:\s*(.+?)\s*$")
_PERFORMED = "Performed actions:"
_KEEP = "Implicit keep:"


def looked_at(output: str) -> int:
    """How many messages sieve-filter read."""
    return len(_FILTERING.findall(output))


def moves(output: str) -> dict[str, int]:
    """Which folders the filter put messages in, and how many in each.

    Only what the filter itself did counts: sieve-filter reports an "Implicit keep" under that for every message
    the filter didn't act on, which is the message staying where it already is.
    """
    found: dict[str, int] = {}
    performing = False
    for line in output.splitlines():
        if line.startswith(_PERFORMED):
            performing = True
        elif line.startswith(_KEEP) or line.startswith(">> "):
            performing = False
        elif performing:
            stored = _STORED.search(line)
            if stored:
                found[stored.group(1)] = found.get(stored.group(1), 0) + 1
    return found


def apply_to(address: str, script: str, folder: str, execute: bool, owner: str = "") -> tuple[str, int]:
    """Runs a filter over the mail already in a folder, with sieve-filter, which is what Dovecot delivers with.

    Without execute nothing is moved and sieve-filter only says what it would do. Returns its output and how many
    messages it looked at.
    """
    with tempfile.TemporaryDirectory(prefix="mailctl-sieve-") as directory:
        source = Path(directory) / "filter.sieve"
        source.write_text(script)
        # sieve-filter becomes the mail user before it opens the script, and root's own temp directory is closed
        # to everybody else, so both are handed over first. The directory goes with its contents when this ends.
        if owner:
            _give_to(Path(directory), owner)
            _give_to(source, owner)
        arguments = ["sieve-filter", "-u", address, "-W"] + (["-e"] if execute else []) + [str(source), folder]
        output = system.run(*arguments)
    return output, looked_at(output)


def _give_to(path: Path, owner: str) -> None:
    try:
        shutil.chown(path, user=owner)
    except (LookupError, OSError) as problem:
        raise MailctlError(f"Can't give {path} to {owner}: {problem}.") from None
