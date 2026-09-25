"""What webmail keeps for an account, beside its mail.

SOGo stores an account's calendars and address books in tables of its own, one per folder, and its settings --
which hold its filters -- in a row of sogo_user_profile. None of that is in the mail directory, so deleting an
account leaves it behind: dead weight that a new account with the same address would inherit.

Nothing here touches mail. SOGo reads its tables when it starts a session, so an account being deleted has none.
"""

import re
from dataclasses import dataclass

from .db import Database
from .errors import MailctlError

PROFILE = "sogo_user_profile"
FOLDERS = "sogo_folder_info"
# SOGo names a folder's tables after the account; the name is read back from its own table, and checked all the same.
_TABLE = re.compile(r"[A-Za-z0-9_]{1,64}")


@dataclass(frozen=True)
class Removed:
    folders: int  # calendars and address books
    settings: bool  # the account's webmail settings, which hold its filters


def exists(db: Database) -> bool:
    """Whether webmail's database is there at all: a mail server can run without SOGo."""
    return PROFILE in _tables(db)


def _tables(db: Database) -> set[str]:
    """The tables webmail's database has. SOGo makes them when it first runs, and a server that has never had a
    webmail session has fewer of them than one that has."""
    # MariaDB answers with the column named as it was asked for, so it is asked for in lower case.
    return {row["name"] for row in
            db.rows("SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'sogo'")}


def remove_user(db: Database, address: str) -> Removed:
    """Deletes what webmail keeps for the account: its calendars, its address books and its settings."""
    tables = _tables(db)
    if PROFILE not in tables:
        return Removed(0, False)
    folders = db.rows(f"SELECT c_folder_id, c_location, c_quick_location, c_acl_location FROM sogo.{FOLDERS} "
                      "WHERE c_path2 = %s", address) if FOLDERS in tables else []
    for folder in folders:
        for column in ("c_location", "c_quick_location", "c_acl_location"):
            _drop(db, folder[column])
        for table in ("sogo_acl", "sogo_store"):
            if table in tables:
                db.execute(f"DELETE FROM sogo.{table} WHERE c_folder_id = %s", folder["c_folder_id"])
        db.execute(f"DELETE FROM sogo.{FOLDERS} WHERE c_folder_id = %s", folder["c_folder_id"])
    for table in ("sogo_acl", "sogo_cache_folder", "sogo_users"):
        if table in tables:
            db.execute(f"DELETE FROM sogo.{table} WHERE c_uid = %s", address)
    settings = db.execute(f"DELETE FROM sogo.{PROFILE} WHERE c_uid = %s", address)
    return Removed(len(folders), bool(settings))


def _drop(db: Database, location: str | None) -> None:
    """Drops the table a folder's rows are in. Its name is the last part of the address SOGo stored for it."""
    if not location:
        return
    name = location.rstrip("/").rsplit("/", 1)[-1]
    if not _TABLE.fullmatch(name):
        raise MailctlError(f"{FOLDERS} names a table mailctl won't touch: {name!r}.")
    db.execute(f"DROP TABLE IF EXISTS sogo.{name}")
