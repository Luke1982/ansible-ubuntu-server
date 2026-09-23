"""A file of accounts to create: addresses with their passwords, as JSON.

Moving a mail server means creating tens of accounts whose passwords are already known, and typing them in one by
one invites a mistake in the one thing nobody can check afterwards. The file is read whole and checked whole, so a
mistake in it is found before any account exists.

Two shapes are read, because both are what people write:

    {"info@example.nl": "a password", "sales@example.nl": "another"}
    [{"address": "info@example.nl", "password": "a password"}, ...]
"""

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from . import addresses, names
from .errors import MailctlError

MAX_SIZE = 1 << 20  # bytes; a password file with more than a few thousand accounts isn't one
_ADDRESS_KEYS = ("address", "email", "e-mail")
_PASSWORD_KEYS = ("password", "pass")


@dataclass(frozen=True)
class Account:
    address: str
    password: str


def read(path: Path) -> list[Account]:
    """The accounts in the file, each with a valid address and a password this server accepts.

    Fails on the first thing that is wrong, saying where it is, since a file of passwords is checked before it is
    used, not halfway through.
    """
    accounts = [_account(entry, place) for entry, place in _entries(_load(path))]
    seen = set()
    for account in accounts:
        if account.address in seen:
            raise MailctlError(f"{path} has {account.address} twice.")
        seen.add(account.address)
    return accounts


def readable_by_others(path: Path) -> bool:
    """Whether anyone but the owner may read the file, which for a file of passwords is worth saying."""
    return bool(path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH))


def _load(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_SIZE:
            raise MailctlError(f"{path} is larger than {MAX_SIZE // 1024} KB, which isn't a file of accounts.")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MailctlError(f"There is no file {path}.") from None
    except (OSError, UnicodeDecodeError) as problem:
        raise MailctlError(f"Can't read {path}: {problem}.") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as problem:
        raise MailctlError(f"{path} isn't JSON: {problem.msg} on line {problem.lineno}.",
                           hint='Like {"info@example.nl": "a password"}.') from None


def _entries(loaded: object) -> list[tuple[object, str]]:
    """Each entry with a word saying where it is, for a message about the one that is wrong."""
    if isinstance(loaded, dict):
        return [({"address": address, "password": password}, f"the entry for {address}")
                for address, password in loaded.items()]
    if isinstance(loaded, list):
        return [(entry, f"entry {number}") for number, entry in enumerate(loaded, start=1)]
    raise MailctlError("The file holds neither a list of accounts nor addresses with their passwords.",
                       hint='Like {"info@example.nl": "a password"}.')


def _account(entry: object, place: str) -> Account:
    if not isinstance(entry, dict):
        raise MailctlError(f"{place.capitalize()} isn't an address with a password.")
    address = _value(entry, _ADDRESS_KEYS, place, "address")
    password = _value(entry, _PASSWORD_KEYS, place, "password")
    try:
        checked = names.address(address)
        addresses.check_password(password)
    except MailctlError as problem:
        raise MailctlError(f"{place.capitalize()}: {problem.message}", hint=problem.hint) from None
    return Account(checked, password)


def _value(entry: dict, keys: tuple[str, ...], place: str, what: str) -> str:
    for key in keys:
        found = next((value for name, value in entry.items() if name.lower() == key), None)
        if found is not None:
            if not isinstance(found, str):
                raise MailctlError(f"The {what} in {place} isn't text.")
            return found
    raise MailctlError(f"{place.capitalize()} has no {what}.", hint=f'Name it "{keys[0]}".')
