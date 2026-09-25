"""A mail server in one JSON file: its domains, accounts, forwards, spam settings and filters, to move them to
another server. mailctl export writes it, and export-mailserver.sh writes the same from a server without mailctl.

    {
      "version": 1,
      "domains": ["example.nl"],
      "addresses": [{"address": "info@example.nl", "password_hash": "{SHA512-CRYPT}$6$..."},
                    {"address": "sales@example.nl", "password": "a password"}],
      "forwards": [{"source": "sales@example.nl", "destination": "info@example.nl", "send_as": true}],
      "spam": [{"target": "server", "setting": "required_score", "value": "5"},
               {"target": "example.nl", "setting": "welcomelist_from", "value": "*@partner.nl"}],
      "sieve": [{"address": "info@example.nl", "name": "roundcube", "active": true, "content": "require ..."}]
    }

Every part may be left out. An account has its password as a hash, as the other server stores it, or in plain text.
The file is read and checked whole, so a mistake in it is found before anything is created.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import addresses, forwards, names, sieve, spam
from .db import Database
from .errors import MailctlError
from .mailbox import SieveScript

VERSION = 1
MAX_SIZE = 256 << 20  # bytes; filters make the file bigger than a list of accounts, but not this big
_PARTS = ("version", "domains", "addresses", "forwards", "spam", "sieve")


@dataclass(frozen=True)
class Account:
    address: str
    password: str | None = None  # in plain text
    password_hash: str | None = None


@dataclass(frozen=True)
class Forward:
    source: str
    destination: str
    send_as: bool = False


@dataclass(frozen=True)
class SpamValue:
    target: str  # an address, a domain or 'server'
    setting: str
    value: str


@dataclass(frozen=True)
class Filter:
    address: str
    name: str
    content: str
    active: bool = False


@dataclass
class Contents:
    domains: list[str] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    forwards: list[Forward] = field(default_factory=list)
    spam: list[SpamValue] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)

    def needed_domains(self) -> set[str]:
        """The domains the file's accounts, forwards and settings belong to."""
        needed = set(self.domains)
        needed |= {names.split(account.address)[1] for account in self.accounts}
        needed |= {names.split(forward.source)[1] for forward in self.forwards}
        for value in self.spam:
            if value.target != spam.SERVER:
                needed.add(names.split(value.target)[1] if names.is_address(value.target) else value.target)
        return needed


def read(path: Path) -> Contents:
    """The file's contents, checked. Fails on the first thing that is wrong, saying where it is."""
    loaded = _load(path)
    if not isinstance(loaded, dict):
        raise MailctlError(f"{path} isn't a mail server file: it should hold one JSON object.", hint=_EXAMPLE)
    unknown = sorted(set(loaded) - set(_PARTS))
    if unknown:
        raise MailctlError(f"{path} has parts mailctl doesn't know: {', '.join(unknown)}.",
                           hint=f"The parts are: {', '.join(_PARTS)}.")
    version = loaded.get("version", VERSION)
    if version != VERSION:
        raise MailctlError(f"{path} is version {version} of the file, and this mailctl reads version {VERSION}.")
    contents = Contents(
        domains=[_domain(entry, place) for entry, place in _list(loaded, "domains")],
        accounts=[_account(entry, place) for entry, place in _list(loaded, "addresses")],
        forwards=[_forward(entry, place) for entry, place in _list(loaded, "forwards")],
        spam=[value for entry, place in _list(loaded, "spam") for value in _spam_values(entry, place)],
        filters=[_filter(entry, place) for entry, place in _list(loaded, "sieve")],
    )
    _check_together(path, contents)
    # A forward to itself is how the other server kept a copy in the mailbox; adding the forwards does that here.
    contents.forwards = list(dict.fromkeys(forward for forward in contents.forwards
                                           if forward.source != forward.destination))
    contents.domains = list(dict.fromkeys(contents.domains))
    contents.spam = list(dict.fromkeys(contents.spam))
    return contents


def collect(db: Database, filters_of: Callable[[str], list[SieveScript]]) -> dict:
    """This server's contents in the shape read() reads, with the accounts' filters as filters_of gives them."""
    accounts = db.rows("SELECT email, password FROM virtual_users ORDER BY email")
    preferences = db.rows("SELECT username, preference, value FROM spamassassin.userpref ORDER BY prefid")
    return {
        "version": VERSION,
        "domains": [row["name"] for row in db.rows("SELECT DISTINCT name FROM virtual_domains ORDER BY name")],
        "addresses": [{"address": row["email"], "password_hash": row["password"]} for row in accounts],
        "forwards": [{"source": forward.source, "destination": forward.destination, "send_as": forward.send_as}
                     for forward in forwards.list_forwards(db)],
        "spam": [{"target": spam.target_of(row["username"]), "setting": row["preference"], "value": row["value"]}
                 for row in preferences],
        "sieve": [{"address": row["email"], "name": script.name, "active": script.active, "content": script.content}
                  for row in accounts for script in filters_of(row["email"])],
    }


_EXAMPLE = 'Like {"domains": ["example.nl"], "addresses": [{"address": "info@example.nl", "password": "..."}]}.'


def _load(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_SIZE:
            raise MailctlError(f"{path} is larger than {MAX_SIZE >> 20} MB, which isn't a mail server file.")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MailctlError(f"There is no file {path}.") from None
    except IsADirectoryError:
        raise MailctlError(f"{path} is a folder.", hint="Give the JSON file.") from None
    except (OSError, UnicodeDecodeError) as problem:
        raise MailctlError(f"Can't read {path}: {problem}.") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as problem:
        raise MailctlError(f"{path} isn't JSON: {problem.msg} on line {problem.lineno}.", hint=_EXAMPLE) from None


def _list(loaded: dict, part: str) -> list[tuple[object, str]]:
    """The entries of a part, each with words saying where it is, for a message about the one that is wrong."""
    entries = loaded.get(part, [])
    if not isinstance(entries, list):
        raise MailctlError(f'"{part}" should be a list.')
    return [(entry, f'entry {number} of "{part}"') for number, entry in enumerate(entries, start=1)]


def _checked[T](place: str, check: Callable[[], T]) -> T:
    """What check returns, with the place added to its complaint."""
    try:
        return check()
    except MailctlError as problem:
        raise MailctlError(f"{place.capitalize()}: {problem.message}", hint=problem.hint) from None


def _domain(entry: object, place: str) -> str:
    if not isinstance(entry, str):
        raise MailctlError(f"{place.capitalize()} isn't a domain name.")
    return _checked(place, lambda: names.domain(entry))


def _account(entry: object, place: str) -> Account:
    entry = _object(entry, place, "an address with its password")
    address = _checked(place, lambda: names.address(_text(entry, "address", place)))
    password, password_hash = _text(entry, "password", place, None), _text(entry, "password_hash", place, None)
    if (password is None) == (password_hash is None):
        raise MailctlError(f'{place.capitalize()} ({address}) should have either a "password" or a "password_hash".')
    if password is not None:
        _checked(f"{place} ({address})", lambda: addresses.check_password(password))
    else:
        _checked(f"{place} ({address})", lambda: addresses.check_hash(password_hash))
        password_hash = addresses.labelled(password_hash)
    return Account(address, password, password_hash)


def _forward(entry: object, place: str) -> Forward:
    entry = _object(entry, place, "a forward")
    source = _checked(place, lambda: names.address(_text(entry, "source", place)))
    destination = _checked(place, lambda: names.address(_text(entry, "destination", place)))
    send_as = entry.get("send_as", False)
    if not isinstance(send_as, bool):
        raise MailctlError(f'"send_as" in {place} should be true or false.')
    return Forward(source, destination, send_as)


def _spam_values(entry: object, place: str) -> list[SpamValue]:
    """The entry's setting; a list setting can have several values in one, as SpamAssassin allows."""
    entry = _object(entry, place, "a spam setting")
    target = _text(entry, "target", place).strip().lower()
    if target != spam.SERVER:
        target = _checked(place, lambda: names.address(target) if names.is_address(target) else names.domain(target))
    setting = _text(entry, "setting", place).strip().lower()
    setting = spam.RENAMED.get(setting, setting)
    value = _text(entry, "value", place)
    if setting in spam.SETTINGS:
        values = value.split() if spam.SETTINGS[setting].many else [value]
        return [SpamValue(target, setting, _checked(f"{place} ({setting})", lambda: spam.normalise(setting, one)))
                for one in values]
    # Settings mailctl doesn't know were added by hand on the other server; they come along as they are.
    if not setting or len(setting) > spam.MAX_VALUE_LENGTH or len(value) > spam.MAX_VALUE_LENGTH:
        raise MailctlError(f"{place.capitalize()} ({setting or 'no setting'}) is empty or longer than "
                           f"{spam.MAX_VALUE_LENGTH} characters.")
    return [SpamValue(target, setting, value)]


def _filter(entry: object, place: str) -> Filter:
    entry = _object(entry, place, "a filter")
    address = _checked(place, lambda: names.address(_text(entry, "address", place)))
    name = _text(entry, "name", place)
    if not sieve.valid_name(name):
        raise MailctlError(f"{place.capitalize()}: '{name}' isn't a filter name.",
                           hint="Letters, digits, spaces, dots, dashes and underscores, up to 64 of them.")
    content = sieve.without_doveadm_header(_text(entry, "content", place))
    if len(content.encode()) > sieve.MAX_SCRIPT:
        raise MailctlError(f"{place.capitalize()} ({name}) is bigger than {sieve.MAX_SCRIPT // 1024} KB.")
    if "\0" in content:
        raise MailctlError(f"{place.capitalize()} ({name}) holds NUL characters, so it isn't a Sieve script.")
    active = entry.get("active", False)
    if not isinstance(active, bool):
        raise MailctlError(f'"active" in {place} should be true or false.')
    return Filter(address, name, content, active)


def _check_together(path: Path, contents: Contents) -> None:
    """What only shows when the entries are seen together: an account or filter twice, or more than one active
    filter for an account."""
    seen: set[str] = set()
    for account in contents.accounts:
        if account.address in seen:
            raise MailctlError(f"{path} has the account {account.address} twice.")
        seen.add(account.address)
    filters: set[tuple[str, str]] = set()
    active: set[str] = set()
    for script in contents.filters:
        if (script.address, script.name) in filters:
            raise MailctlError(f"{path} has the filter {script.name} of {script.address} twice.")
        filters.add((script.address, script.name))
        if script.active and script.address in active:
            raise MailctlError(f"{path} has more than one active filter for {script.address}.",
                               hint="An account has one active filter; set \"active\" to false for the others.")
        if script.active:
            active.add(script.address)
    without_account = sorted({script.address for script in contents.filters} - seen)
    if without_account:
        which = "isn't an account" if len(without_account) == 1 else "aren't accounts"
        raise MailctlError(f"{path} has filters for {', '.join(without_account)}, which {which} in the file.",
                           hint='Add the account to "addresses", or leave its filters out.')


def _object(entry: object, place: str, what: str) -> dict:
    if not isinstance(entry, dict):
        raise MailctlError(f"{place.capitalize()} isn't {what}.")
    return entry


_REQUIRED = object()


def _text(entry: dict, key: str, place: str, default=_REQUIRED):
    value = entry.get(key, default)
    if value is _REQUIRED:
        raise MailctlError(f'{place.capitalize()} has no "{key}".')
    if value is not default and not isinstance(value, str):
        raise MailctlError(f'"{key}" in {place} isn\'t text.')
    return value
