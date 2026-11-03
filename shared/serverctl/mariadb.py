"""MariaDB: the databases and users the tools make for the sites and applications on this server.

Root reaches MariaDB over the local socket, logging in as the server's root does, so no password is needed or
kept anywhere. Every name and value that goes into a statement is quoted here, so a name with a quote or a
backtick in it can't end the string it is in and become part of the statement.

A database and the user that shares its name belong together: they are made and dropped as a pair. A user of
another name is only ever given rights or taken off a database, never dropped, because one customer with
several sites logs into all of them with one name.
"""

from typing import IO

from . import system


def sql(statements: str) -> str:
    """Runs the statements and gives back what MariaDB printed, one value per line, without a header."""
    return system.run("mariadb", "--batch", "--skip-column-names", stdin=statements)


def string(value: str) -> str:
    """A value as a quoted SQL string."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def name(value: str) -> str:
    """A database or table name, quoted the way MariaDB quotes identifiers."""
    return "`" + value.replace("`", "``") + "`"


def database_taken(database: str) -> str | None:
    """What already has the name in MariaDB, a database or a user, or None when both are free."""
    counts = sql(f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = {string(database)};\n"
                 f"SELECT COUNT(*) FROM mysql.user WHERE User = {string(database)};\n").split()
    if counts and counts[0] != "0":
        return f"a database {database}"
    if len(counts) > 1 and counts[1] != "0":
        return f"a MariaDB user {database}"
    return None


def user_taken(user: str) -> bool:
    """Whether MariaDB already has this user."""
    found = sql(f"SELECT COUNT(*) FROM mysql.user WHERE User = {string(user)};\n").split()
    return bool(found) and found[0] != "0"


def create_database(database: str, password: str, db_user: str | None = None) -> None:
    """A database with a user that may only use it, only over the local socket. The user has the database's own
    name unless another is given: a site that came from another server keeps the user name its settings hold,
    so its own configuration still fits.

    A user that is already there keeps its password and whatever else it may use: one customer with two sites
    logs into both with one name, and taking that name away from the first to give it to the second would stop
    the first working.
    """
    user = f"{string(db_user or database)}@'localhost'"
    sql(f"CREATE DATABASE {name(database)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {string(password)};\n"
        f"GRANT ALL PRIVILEGES ON {name(database)}.* TO {user};\n")


def drop_database(database: str, db_user: str | None = None) -> None:
    """The database and the user of the same name. A user of another name is only taken off this database:
    it may be the login of other sites of the same customer, and nothing here may break those."""
    # Only the database and the user that shares its name. A user of another name is left alone altogether:
    # it may be the login of other sites of this customer, and the grant it has on this database is the one
    # that is handed out again the moment the database is made anew.
    sql(f"DROP DATABASE IF EXISTS {name(database)};\nDROP USER IF EXISTS {string(database)}@'localhost';\n")


def import_dump(database: str, dump: IO[bytes]) -> None:
    """Reads a dump into the database as bytes, so a big one isn't held in memory and nothing in it is re-encoded."""
    system.run_fed("mariadb", database, source=dump)
