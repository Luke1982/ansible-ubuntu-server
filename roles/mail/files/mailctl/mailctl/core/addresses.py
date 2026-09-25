"""Mail accounts (virtual_users)."""

import re

from . import activity, domains, forwards, names, senders, spam, system
from .db import Database
from .errors import MailctlError

MIN_PASSWORD_LENGTH = 8
MAX_HASH_LENGTH = 150  # the size of the password column
# A hash as Dovecot reads it: with its scheme in braces, or a crypt hash like $6$..., which Dovecot checks with crypt().
_PASSWORD_HASH = re.compile(r"(\{[A-Za-z0-9.-]+\}\S+|\$[0-9a-z]+\$\S+)")
SCHEME = "SHA512-CRYPT"  # what this server hashes passwords with, and what webmail (SOGo) expects
# Schemes that are slow and salted enough to keep. The others, like MD5-CRYPT or PLAIN, can be cracked or read.
STRONG_SCHEMES = {"SHA256-CRYPT", "SHA512-CRYPT", "BLF-CRYPT", "ARGON2I", "ARGON2ID", "PBKDF2", "SCRAM-SHA-256"}
# The scheme of a crypt hash without one, by its $id$; Dovecot reads those with crypt(), which knows them by that id.
_CRYPT_SCHEMES = {"1": "MD5-CRYPT", "5": "SHA256-CRYPT", "6": "SHA512-CRYPT", "2a": "BLF-CRYPT", "2b": "BLF-CRYPT",
                  "2y": "BLF-CRYPT"}


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


def check_hash(password_hash: str) -> None:
    if len(password_hash) > MAX_HASH_LENGTH:
        raise MailctlError(f"The password hash is longer than {MAX_HASH_LENGTH} characters.")
    if not _PASSWORD_HASH.fullmatch(password_hash):
        raise MailctlError("The password hash isn't one Dovecot reads.",
                           hint="Like {SHA512-CRYPT}$6$... or $6$..., as the password column of the other server has it.")


def labelled(password_hash: str) -> str:
    """The hash with its scheme in front, as this server stores hashes: webmail takes one without a scheme for
    SHA512-CRYPT."""
    if password_hash.startswith("{"):
        return password_hash
    crypt_id = password_hash.split("$")[1]
    return "{" + _CRYPT_SCHEMES.get(crypt_id, "CRYPT") + "}" + password_hash


def scheme_of(password_hash: str) -> str:
    """The scheme of a labelled hash, in capitals."""
    return password_hash[1:password_hash.index("}")].upper()


def supported_schemes() -> set[str]:
    """The schemes Dovecot on this server can check a password against."""
    return {scheme.upper() for scheme in system.run("doveadm", "pw", "-l").split()}


def add(db: Database, address: str, password: str) -> None:
    """Creates the account, lets it send as itself, and keeps it a copy if the address already has forwards."""
    add_hashed(db, address, _hash_password(password))


def add_hashed(db: Database, address: str, password_hash: str) -> None:
    """Like add(), with the password as its hash, as another server stores it."""
    check_hash(password_hash)
    domain_id = domains.id_of(db, names.split(address)[1])
    require_available(db, address)
    db.execute(
        "INSERT INTO virtual_users (domain_id, email, password) VALUES (%s, %s, %s)", domain_id, address, password_hash,
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
