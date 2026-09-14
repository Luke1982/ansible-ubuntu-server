"""Mail domains (virtual_domains).

The old helper script inserted the domain every time it ran, so a name can occur more than once.
"""

from dataclasses import dataclass

from . import spam
from .db import Database
from .errors import MailctlError


@dataclass(frozen=True)
class Domain:
    name: str
    addresses: int
    forwards: int


def list_domains(db: Database) -> list[Domain]:
    return _summaries(db, "TRUE")


def get(db: Database, name: str) -> Domain:
    summaries = _summaries(db, "name = %s", name)
    if not summaries:
        raise _not_on_this_server(name)
    return summaries[0]


def exists(db: Database, name: str) -> bool:
    return db.value("SELECT 1 FROM virtual_domains WHERE name = %s LIMIT 1", name) is not None


def require(db: Database, name: str) -> None:
    if not exists(db, name):
        raise _not_on_this_server(name)


def id_of(db: Database, name: str) -> int:
    """The id that rows belonging to the domain refer to."""
    domain_id = db.value("SELECT MIN(id) FROM virtual_domains WHERE name = %s", name)
    if domain_id is None:
        raise _not_on_this_server(name)
    return domain_id


def add(db: Database, name: str) -> None:
    if exists(db, name):
        raise MailctlError(f"{name} already exists.")
    db.execute("INSERT INTO virtual_domains (name) VALUES (%s)", name)


def delete(db: Database, name: str) -> None:
    """Deletes a domain that has no addresses left, with its forwards, sender permissions and spam settings."""
    require(db, name)
    has_addresses = db.value(
        "SELECT 1 FROM virtual_users u JOIN virtual_domains d ON d.id = u.domain_id WHERE d.name = %s LIMIT 1", name
    )
    if has_addresses:
        raise MailctlError(f"{name} still has addresses.", hint="Delete its addresses first.")
    spam.forget(db, spam.domain_username(name))
    # Forwards and sender permissions go along through their foreign keys.
    db.execute("DELETE FROM virtual_domains WHERE name = %s", name)


def _summaries(db: Database, condition: str, *params) -> list[Domain]:
    rows = db.rows(
        f"""
        SELECT d.name,
               (SELECT COUNT(*) FROM virtual_users u JOIN virtual_domains du ON du.id = u.domain_id
                WHERE du.name = d.name) AS addresses,
               (SELECT COUNT(*) FROM virtual_aliases a JOIN virtual_domains da ON da.id = a.domain_id
                WHERE da.name = d.name AND a.source <> a.destination) AS forwards
        FROM (SELECT DISTINCT name FROM virtual_domains WHERE {condition}) d
        ORDER BY d.name
        """,
        *params,
    )
    return [Domain(**row) for row in rows]


def _not_on_this_server(name: str) -> MailctlError:
    return MailctlError(f"{name} isn't a domain on this server.", hint="See the domains with: mailctl domain list")
