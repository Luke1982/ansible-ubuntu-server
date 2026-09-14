"""The mail databases, reached as root over MariaDB's unix socket."""

from collections.abc import Iterator
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .config import Config
from .errors import MailctlError


class Database:
    """Queries with %s placeholders. Tables of other databases are named in full, like spamassassin.userpref."""

    def __init__(self, connection: pymysql.connections.Connection) -> None:
        self._connection = connection
        self._in_transaction = False

    def rows(self, sql: str, *params) -> list[dict]:
        with self._connection.cursor() as cursor:
            # Without parameters PyMySQL leaves the query alone, so a literal % needs no escaping.
            cursor.execute(sql, params or None)
            return list(cursor.fetchall())

    def row(self, sql: str, *params) -> dict | None:
        rows = self.rows(sql, *params)
        return rows[0] if rows else None

    def value(self, sql: str, *params):
        """The first column of the first row, or None when there are no rows."""
        row = self.row(sql, *params)
        return next(iter(row.values())) if row else None

    def execute(self, sql: str, *params) -> int:
        """Runs a statement and returns the number of affected rows."""
        with self._connection.cursor() as cursor:
            return cursor.execute(sql, params or None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commits the block as a whole or not at all. A transaction inside another one is part of the outer one."""
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        self._connection.begin()
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
        finally:
            self._in_transaction = False


@contextmanager
def connect(config: Config) -> Iterator[Database]:
    try:
        connection = pymysql.connect(
            unix_socket=config.db_socket,
            user=config.db_user,
            database="mailserver",
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    except pymysql.err.OperationalError as error:
        raise MailctlError(f"Can't connect to the database ({error.args[-1]}).", hint="Check that MariaDB is running.") from None
    try:
        yield Database(connection)
    finally:
        connection.close()
