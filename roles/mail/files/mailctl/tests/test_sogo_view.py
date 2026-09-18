"""The view SOGo reads the mail accounts through (roles/mail/files/setup_sogo_view.sql), used as the sogo database user,
who has rights on SOGo's own database only."""

import pymysql
import pytest
from conftest import ROLE_FILES

SOGO_PASSWORD = "sogo-test"


@pytest.fixture
def sogo(db_config):
    """A connection as the sogo user, with the view in place and two accounts in two domains."""
    root = pymysql.connect(unix_socket=db_config.db_socket, user="root", autocommit=True)
    with root.cursor() as cursor:
        cursor.execute("DROP DATABASE IF EXISTS sogo")
        cursor.execute("CREATE DATABASE sogo")
        cursor.execute("USE sogo")
        cursor.execute((ROLE_FILES / "setup_sogo_view.sql").read_text())
        cursor.execute("DROP USER IF EXISTS 'sogo'@'localhost'")
        cursor.execute("CREATE USER 'sogo'@'localhost' IDENTIFIED BY %s", SOGO_PASSWORD)
        cursor.execute("GRANT ALL ON sogo.* TO 'sogo'@'localhost'")
        cursor.execute("INSERT INTO mailserver.virtual_domains (name) VALUES ('example.nl'), ('other.nl')")
        cursor.execute(
            "INSERT INTO mailserver.virtual_users (domain_id, email, password) VALUES"
            " (1, 'info@example.nl', '{SHA512-CRYPT}$6$old'), (2, 'sales@other.nl', '{SHA256-CRYPT}$5$other')"
        )
    connection = pymysql.connect(
        unix_socket=db_config.db_socket, user="sogo", password=SOGO_PASSWORD, database="sogo", autocommit=True
    )
    yield connection
    connection.close()
    root.close()


def query(connection, sql, *params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def test_the_view_lists_every_account_with_its_domain(sogo):
    rows = query(sogo, "SELECT c_uid, c_name, c_cn, mail, c_domain, c_password FROM sogo_users ORDER BY c_uid")

    assert rows == (
        ("info@example.nl", "info@example.nl", "info@example.nl", "info@example.nl", "example.nl",
         "{SHA512-CRYPT}$6$old"),
        ("sales@other.nl", "sales@other.nl", "sales@other.nl", "sales@other.nl", "other.nl", "{SHA256-CRYPT}$5$other"),
    )


def test_a_password_changed_through_the_view_reaches_the_account(sogo, database):
    query(sogo, "UPDATE sogo_users SET c_password = %s WHERE c_uid = %s", "{SHA512-CRYPT}$6$new", "info@example.nl")

    assert database.rows("SELECT email, password FROM virtual_users ORDER BY email") == [
        {"email": "info@example.nl", "password": "{SHA512-CRYPT}$6$new"},
        {"email": "sales@other.nl", "password": "{SHA256-CRYPT}$5$other"},
    ]


def test_the_sogo_user_cant_read_the_mail_database_itself(sogo):
    with pytest.raises(pymysql.err.OperationalError, match="denied"):
        query(sogo, "SELECT password FROM mailserver.virtual_users")
