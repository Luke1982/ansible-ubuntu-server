"""Who may send as which address (virtual_sender_aliases, Postfix's smtpd_sender_login_maps).

Rows written before mailctl may use capitals, so logins are compared regardless of case.
"""

import re

from . import domains, names
from .db import Database
from .errors import MailctlError

MAX_USERS_LENGTH = 255  # the size of the users column


def logins_for(db: Database, address: str) -> list[str]:
    """The logins allowed to send as the address."""
    return _parse(db.value("SELECT users FROM virtual_sender_aliases WHERE alias = %s", address))


def permissions(db: Database) -> dict[str, set[str]]:
    """Every address with the logins allowed to send as it, all in lowercase."""
    return {
        row["alias"].lower(): {login.lower() for login in _parse(row["users"])}
        for row in db.rows("SELECT alias, users FROM virtual_sender_aliases")
    }


def allow(db: Database, login: str, address: str) -> None:
    logins = logins_for(db, address)
    if _contains(logins, login):
        return
    if logins:
        _store(db, address, [*logins, login])
    else:
        db.execute(
            "INSERT INTO virtual_sender_aliases (domain_id, users, alias) VALUES (%s, %s, %s)",
            domains.id_of(db, names.split(address)[1]), login, address,
        )


def revoke(db: Database, login: str, address: str) -> bool:
    """Takes away the login's permission to send as the address. Returns whether it had that permission."""
    logins = logins_for(db, address)
    if not _contains(logins, login):
        return False
    remaining = [other for other in logins if other.lower() != login.lower()]
    if remaining:
        _store(db, address, remaining)
    else:
        db.execute("DELETE FROM virtual_sender_aliases WHERE alias = %s", address)
    return True


def forget_login(db: Database, login: str) -> None:
    """Revokes every permission a login has, for when its account is deleted."""
    # LIKE only narrows the search down; revoke() matches the login exactly.
    for row in db.rows("SELECT alias FROM virtual_sender_aliases WHERE users LIKE %s", f"%{login}%"):
        revoke(db, login, row["alias"])


def _parse(users: str | None) -> list[str]:
    return [login for login in re.split(r"[\s,]+", users or "") if login]


def _contains(logins: list[str], login: str) -> bool:
    return login.lower() in (other.lower() for other in logins)


def _store(db: Database, address: str, logins: list[str]) -> None:
    users = ",".join(logins)
    if len(users) > MAX_USERS_LENGTH:
        raise MailctlError(
            f"Too many accounts may send as {address} to add another one.",
            hint="Forward the address to fewer accounts, or let them send from their own address.",
        )
    db.execute("UPDATE virtual_sender_aliases SET users = %s WHERE alias = %s", users, address)
