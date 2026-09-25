"""Sieve filters: their name, and whether Dovecot can compile them.

A filter only counts as one when sievec, the compiler Dovecot itself uses, accepts it, so everything that brings
filters to an account checks them here first: an account is never left with a filter that fails at delivery.
"""

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import system

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
