"""Mail accounts (virtual_users)."""

from . import activity, domains, forwards, names, senders, spam, system
from .db import Database
from .errors import MailctlError

MIN_PASSWORD_LENGTH = 8


def list_addresses(db: Database, domain: str | None = None) -> list[str]:
    rows = db.rows(
        "SELECT u.email FROM virtual_users u JOIN virtual_domains d ON d.id = u.domain_id"
        " WHERE %s IS NULL OR d.name = %s ORDER BY d.name, u.email",
        domain, domain,
    )
    return [row["email"] for row in rows]


def exists(db: Database, address: str) -> bool:
    return db.value("SELECT 1 FROM virtual_users WHERE email = %s", address) is not None


def require(db: Database, address: str) -> None:
    if not exists(db, address):
        raise MailctlError(f"{address} isn't an account on this server.", hint="See the accounts with: mailctl address list")


def require_available(db: Database, address: str) -> None:
    if exists(db, address):
        raise MailctlError(f"{address} already exists.")


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise MailctlError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")
    if "\n" in password or "\r" in password:
        raise MailctlError("The password can't contain a line break.")


def add(db: Database, address: str, password: str) -> None:
    """Creates the account, lets it send as itself, and keeps it a copy if the address already has forwards."""
    domain_id = domains.id_of(db, names.split(address)[1])
    require_available(db, address)
    db.execute(
        "INSERT INTO virtual_users (domain_id, email, password) VALUES (%s, %s, %s)",
        domain_id, address, _hash_password(password),
    )
    senders.allow(db, address, address)
    forwards.keep_copy(db, address)


def set_password(db: Database, address: str, password: str) -> None:
    require(db, address)
    db.execute("UPDATE virtual_users SET password = %s WHERE email = %s", _hash_password(password), address)


def delete(db: Database, address: str) -> None:
    """Deletes the account with its sender permissions, spam settings and login history. Its forwards stay."""
    require(db, address)
    db.execute("DELETE FROM virtual_users WHERE email = %s", address)
    forwards.drop_copy(db, address)
    senders.forget_login(db, address)
    spam.forget(db, address)
    activity.forget(db, address)


def _hash_password(password: str) -> str:
    check_password(password)
    return "{SHA512-CRYPT}" + system.run("openssl", "passwd", "-6", "-stdin", stdin=password + "\n").strip()
