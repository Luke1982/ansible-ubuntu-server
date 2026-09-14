"""SpamAssassin user preferences (spamassassin.userpref).

For each message SpamAssassin reads the rows of the whole server ($GLOBAL), the recipient's domain (%domain)
and the recipient, in that order (see templates/spamassassin-sql.cf.j2). A later single value replaces an
earlier one; list values add up. spamass-milter only passes the recipient for mail with one recipient, so
mail to several recipients at once gets the server's settings.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import names
from .db import Database
from .errors import MailctlError

GLOBAL = "$GLOBAL"
SERVER = "server"  # the target that stands for the whole server
MAX_VALUE_LENGTH = 100  # the size of the value column
# 0 for the server, 1 for a domain, 2 for an account; as in templates/spamassassin-sql.cf.j2 (% doubled for PyMySQL).
_POSITION = "CASE LEFT(username, 1) WHEN '$' THEN 0 WHEN '%%' THEN 1 ELSE 2 END"


@dataclass(frozen=True)
class Setting:
    many: bool  # whether values add up instead of replacing each other
    normalise: Callable[[str], str]  # returns the value to store, or raises MailctlError


@dataclass(frozen=True)
class Scope:
    username: str
    label: str


@dataclass(frozen=True)
class Preference:
    setting: str
    value: str
    scope: Scope
    in_effect: bool


def _number(value: str) -> str:
    number = value.strip()
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", number):
        raise MailctlError(f"'{number}' isn't a number.", hint="Use a number like 5 or 4.5.")
    return number


def _sender_pattern(value: str) -> str:
    pattern = value.strip().lower()
    if len(pattern) > MAX_VALUE_LENGTH:
        raise MailctlError(f"'{value.strip()}' is too long: a pattern can have at most {MAX_VALUE_LENGTH} characters.")
    if not re.fullmatch(r"\S*[@.]\S*", pattern):
        raise MailctlError(f"'{value.strip()}' isn't an address or pattern like *@example.nl.")
    return pattern


SETTINGS = {
    "required_score": Setting(many=False, normalise=_number),
    "welcomelist_from": Setting(many=True, normalise=_sender_pattern),
    "blocklist_from": Setting(many=True, normalise=_sender_pattern),
}


def domain_username(domain: str) -> str:
    return f"%{domain}"


def scope_chain(target: str) -> list[Scope]:
    """The scopes whose settings apply to an address, a domain or 'server', ending with the target's own."""
    chain = [Scope(GLOBAL, "the whole server")]
    if target != SERVER:
        domain = names.split(target)[1] if names.is_address(target) else target
        chain.append(Scope(domain_username(domain), f"domain {domain}"))
        if names.is_address(target):
            chain.append(Scope(target, target))
    return chain


def known_setting(name: str) -> str:
    setting = name.strip().lower()
    if setting not in SETTINGS:
        raise MailctlError(f"Unknown spam setting '{name.strip()}'.", hint=f"Known settings: {', '.join(SETTINGS)}")
    return setting


def normalise(setting: str, value: str) -> str:
    """The value as it's stored, or MailctlError when it isn't valid for the setting."""
    return SETTINGS[known_setting(setting)].normalise(value)


def show(db: Database, chain: list[Scope]) -> list[Preference]:
    # The database matches hand-made rows that differ in case or accents, so it also tells where each belongs:
    # the scope's position in the chain, the same expression SpamAssassin's query orders by.
    rows = db.rows(
        f"SELECT preference, value, {_POSITION} AS position FROM spamassassin.userpref"
        " WHERE username IN %s ORDER BY preference, position, prefid",
        tuple(scope.username for scope in chain),
    )
    final = {row["preference"]: row for row in rows}  # the value a single setting ends up with
    return [
        Preference(
            row["preference"],
            row["value"],
            # A row starting with an invisible character still matches, but isn't placed as a server or domain row.
            chain[min(row["position"], len(chain) - 1)],
            in_effect=_values_add_up(row["preference"]) or final[row["preference"]] is row,
        )
        for row in rows
    ]


def set_value(db: Database, scope: Scope, setting: str, value: str) -> bool:
    """Sets a single value or adds one to a list; the setting and value as known_setting() and normalise() give them.
    Returns whether anything changed."""
    if _is_set(db, scope, setting, value):
        return False
    if not SETTINGS[setting].many:
        unset_value(db, scope, setting)
    # The table has no auto-increment, but prefid is required.
    db.execute(
        "INSERT INTO spamassassin.userpref (username, preference, value, prefid)"
        " SELECT %s, %s, %s, COALESCE(MAX(prefid), 0) + 1 FROM spamassassin.userpref",
        scope.username, setting, value,
    )
    return True


def unset_value(db: Database, scope: Scope, setting: str, value: str | None = None) -> int:
    """Removes the setting, or just one of its values (as normalise() gives it). Returns the number of values removed."""
    if value is None:
        return db.execute(
            "DELETE FROM spamassassin.userpref WHERE username = %s AND preference = %s", scope.username, setting
        )
    return db.execute(
        "DELETE FROM spamassassin.userpref WHERE username = %s AND preference = %s AND value = %s",
        scope.username, setting, value,
    )


def count_values(db: Database, scope: Scope, setting: str) -> int:
    return db.value(
        "SELECT COUNT(*) FROM spamassassin.userpref WHERE username = %s AND preference = %s", scope.username, setting
    )


def forget(db: Database, username: str) -> None:
    db.execute("DELETE FROM spamassassin.userpref WHERE username = %s", username)


def _is_set(db: Database, scope: Scope, setting: str, value: str) -> bool:
    return db.value(
        "SELECT 1 FROM spamassassin.userpref WHERE username = %s AND preference = %s AND value = %s",
        scope.username, setting, value,
    ) is not None


def _values_add_up(setting: str) -> bool:
    # Settings mailctl doesn't know were added by hand; they're never shown as overridden.
    return SETTINGS[setting].many if setting in SETTINGS else True
