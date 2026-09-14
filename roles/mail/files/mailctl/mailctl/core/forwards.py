"""Forwards (virtual_aliases).

Postfix delivers mail for an address that has forwards only to those forwards. So an account with forwards
also gets a forward to itself, which keeps a copy in its mailbox. Those rows are managed here and never
listed as forwards. Accounts forwarded without such a row before mailctl are left that way.
"""

from dataclasses import dataclass

from . import domains, names, senders
from .db import Database
from .errors import MailctlError


@dataclass(frozen=True)
class Forward:
    source: str
    destination: str
    send_as: bool  # whether the destination may send as the source


def list_forwards(db: Database, domain: str | None = None) -> list[Forward]:
    return _select(db, "(%s IS NULL OR d.name = %s)", domain, domain)


def to_address(db: Database, address: str) -> list[Forward]:
    return _select(db, "a.destination = %s", address)


def from_address(db: Database, address: str) -> list[Forward]:
    return _select(db, "a.source = %s", address)


def to_domain(db: Database, domain: str) -> list[Forward]:
    """The forwards from other domains to addresses of the domain."""
    return _select(db, "a.destination LIKE %s AND d.name <> %s", f"%@{domain}", domain)


def exists(db: Database, source: str, destination: str) -> bool:
    return db.value(
        "SELECT 1 FROM virtual_aliases WHERE source = %s AND destination = %s LIMIT 1", source, destination
    ) is not None


def check_new(db: Database, source: str, destination: str) -> None:
    """Raises MailctlError when the forward can't be added."""
    if source == destination:
        raise MailctlError(f"{source} can't forward to itself.")
    domains.require(db, names.split(source)[1])
    if exists(db, source, destination):
        raise MailctlError(f"{source} already forwards to {destination}.")


def add(db: Database, source: str, destination: str) -> None:
    check_new(db, source, destination)
    first_forward = not _has_forwards(db, source)
    _insert(db, source, destination)
    if first_forward and _is_account(db, source):
        keep_copy(db, source)


def delete(db: Database, source: str, destination: str) -> bool:
    """Deletes the forward and the destination's permission to send as the source. Returns whether the destination
    had that permission."""
    if source == destination or not exists(db, source, destination):
        raise MailctlError(f"{source} doesn't forward to {destination}.", hint="See the forwards with: mailctl forward list")
    db.execute("DELETE FROM virtual_aliases WHERE source = %s AND destination = %s", source, destination)
    could_send_as = senders.revoke(db, destination, source)
    if not _has_forwards(db, source):
        drop_copy(db, source)
    return could_send_as


def keep_copy(db: Database, address: str) -> None:
    """Keeps an account with forwards receiving a copy in its mailbox."""
    if _has_forwards(db, address) and not exists(db, address, address):
        _insert(db, address, address)


def drop_copy(db: Database, address: str) -> None:
    db.execute("DELETE FROM virtual_aliases WHERE source = %s AND destination = %s", address, address)


def _has_forwards(db: Database, address: str) -> bool:
    return db.value(
        "SELECT 1 FROM virtual_aliases WHERE source = %s AND destination <> %s LIMIT 1", address, address
    ) is not None


def _is_account(db: Database, address: str) -> bool:
    # Not addresses.exists: the addresses module uses this one.
    return db.value("SELECT 1 FROM virtual_users WHERE email = %s", address) is not None


def _select(db: Database, condition: str, *params) -> list[Forward]:
    rows = db.rows(
        "SELECT a.source, a.destination FROM virtual_aliases a JOIN virtual_domains d ON d.id = a.domain_id"
        f" WHERE a.source <> a.destination AND {condition} ORDER BY d.name, a.source, a.destination",
        *params,
    )
    allowed = senders.permissions(db)
    return [
        Forward(row["source"], row["destination"], row["destination"].lower() in allowed.get(row["source"].lower(), set()))
        for row in rows
    ]


def _insert(db: Database, source: str, destination: str) -> None:
    db.execute(
        "INSERT INTO virtual_aliases (domain_id, source, destination) VALUES (%s, %s, %s)",
        domains.id_of(db, names.split(source)[1]), source, destination,
    )
