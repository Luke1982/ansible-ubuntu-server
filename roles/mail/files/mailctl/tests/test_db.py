from dataclasses import replace

import pytest

from mailctl.core import db
from mailctl.core.errors import MailctlError


def add_domains(database, *names):
    for name in names:
        database.execute("INSERT INTO virtual_domains (name) VALUES (%s)", name)


def test_rows_row_and_value_read_query_results(database):
    add_domains(database, "a.nl", "b.nl")

    assert database.rows("SELECT name FROM virtual_domains ORDER BY name") == [{"name": "a.nl"}, {"name": "b.nl"}]
    assert database.row("SELECT name FROM virtual_domains WHERE name = %s", "b.nl") == {"name": "b.nl"}
    assert database.row("SELECT name FROM virtual_domains WHERE name = %s", "c.nl") is None
    assert database.value("SELECT COUNT(*) FROM virtual_domains") == 2
    assert database.value("SELECT name FROM virtual_domains WHERE name = %s", "c.nl") is None


def test_execute_returns_the_number_of_affected_rows(database):
    add_domains(database, "a.nl", "b.nl")

    assert database.execute("DELETE FROM virtual_domains") == 2


def test_transaction_commits_when_the_block_succeeds(database, db_config):
    with database.transaction():
        add_domains(database, "a.nl")

    with db.connect(db_config) as other:
        assert other.value("SELECT COUNT(*) FROM virtual_domains") == 1


def test_transaction_rolls_back_when_the_block_fails(database):
    with pytest.raises(RuntimeError):
        with database.transaction():
            add_domains(database, "a.nl")
            raise RuntimeError

    assert database.value("SELECT COUNT(*) FROM virtual_domains") == 0


def test_a_transaction_inside_another_one_is_part_of_the_outer_one(database):
    with pytest.raises(RuntimeError):
        with database.transaction():
            add_domains(database, "a.nl")
            with database.transaction():
                add_domains(database, "b.nl")
            raise RuntimeError

    assert database.value("SELECT COUNT(*) FROM virtual_domains") == 0


def test_spamassassin_tables_are_reachable_by_database_name(database):
    assert database.value("SELECT COUNT(*) FROM spamassassin.userpref") == 0


def test_the_schema_has_the_last_login_table(database):
    assert database.value("SELECT COUNT(*) FROM last_login") == 0


def test_connect_reports_an_unreachable_server(config):
    with pytest.raises(MailctlError, match="Can't connect to the database"):
        with db.connect(replace(config, db_socket="/nonexistent/mysqld.sock")):
            pass
