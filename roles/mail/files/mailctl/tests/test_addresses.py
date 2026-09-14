import subprocess

import pytest
from conftest import PASSWORD

from mailctl.core import addresses, domains, forwards, senders
from mailctl.core.errors import MailctlError
from mailctl.core.forwards import Forward


@pytest.fixture(autouse=True)
def example_domain(database):
    domains.add(database, "example.nl")


def stored_hash(database, address):
    return database.value("SELECT password FROM virtual_users WHERE email = %s", address)


def test_add_creates_the_account_and_lets_it_send_as_itself(database):
    addresses.add(database, "info@example.nl", PASSWORD)

    assert addresses.list_addresses(database) == ["info@example.nl"]
    assert senders.logins_for(database, "info@example.nl") == ["info@example.nl"]


def test_add_stores_a_sha512_crypt_hash_of_the_password(database):
    addresses.add(database, "info@example.nl", PASSWORD)

    stored = stored_hash(database, "info@example.nl")
    salt = stored.split("$")[2]
    expected = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
        input=PASSWORD + "\n", capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert stored == "{SHA512-CRYPT}" + expected


def test_add_keeps_a_copy_when_the_address_already_has_forwards(database):
    forwards.add(database, "sales@example.nl", "piet@gmail.com")

    addresses.add(database, "sales@example.nl", PASSWORD)

    assert forwards.exists(database, "sales@example.nl", "sales@example.nl")


def test_check_password(database):
    addresses.check_password("correct horse")

    with pytest.raises(MailctlError, match="at least 8 characters"):
        addresses.check_password("short")


def test_add_refuses_an_unknown_domain(database):
    with pytest.raises(MailctlError, match="other.nl isn't a domain on this server"):
        addresses.add(database, "info@other.nl", PASSWORD)


def test_add_refuses_an_existing_address(database):
    addresses.add(database, "info@example.nl", PASSWORD)

    with pytest.raises(MailctlError, match="info@example.nl already exists"):
        addresses.add(database, "info@example.nl", PASSWORD)


@pytest.mark.parametrize("password, problem", [("short", "at least 8 characters"), ("two\nlines!", "line break")])
def test_add_refuses_an_unusable_password(database, password, problem):
    with pytest.raises(MailctlError, match=problem):
        addresses.add(database, "info@example.nl", password)

    assert addresses.list_addresses(database) == []


def test_list_addresses_sorts_by_domain_and_filters_by_domain(database):
    domains.add(database, "a.nl")
    for address in ("zed@a.nl", "info@example.nl", "abc@a.nl"):
        addresses.add(database, address, PASSWORD)

    assert addresses.list_addresses(database) == ["abc@a.nl", "zed@a.nl", "info@example.nl"]
    assert addresses.list_addresses(database, "example.nl") == ["info@example.nl"]


def test_exists_and_require(database):
    addresses.add(database, "info@example.nl", PASSWORD)

    assert addresses.exists(database, "info@example.nl")
    assert not addresses.exists(database, "sales@example.nl")
    with pytest.raises(MailctlError, match="sales@example.nl isn't an account on this server"):
        addresses.require(database, "sales@example.nl")


def test_set_password_replaces_the_hash(database):
    addresses.add(database, "info@example.nl", PASSWORD)
    before = stored_hash(database, "info@example.nl")

    addresses.set_password(database, "info@example.nl", "another password")

    assert stored_hash(database, "info@example.nl") not in (before, None)


def test_set_password_refuses_an_unknown_address(database):
    with pytest.raises(MailctlError, match="isn't an account on this server"):
        addresses.set_password(database, "info@example.nl", PASSWORD)


def test_delete_removes_the_account_with_its_permissions_settings_and_logins(database):
    addresses.add(database, "info@example.nl", PASSWORD)
    addresses.add(database, "piet@example.nl", PASSWORD)
    senders.allow(database, "info@example.nl", "sales@example.nl")
    senders.allow(database, "piet@example.nl", "sales@example.nl")
    forwards.add(database, "info@example.nl", "piet@example.nl")
    database.execute(
        "INSERT INTO spamassassin.userpref (username, preference, value, prefid)"
        " VALUES ('info@example.nl', 'required_score', '4', 1)"
    )
    database.execute("INSERT INTO last_login VALUES ('info@example.nl', 'imap', 1757851200, '203.0.113.5')")

    addresses.delete(database, "info@example.nl")

    assert addresses.list_addresses(database) == ["piet@example.nl"]
    assert senders.logins_for(database, "info@example.nl") == []
    assert senders.logins_for(database, "sales@example.nl") == ["piet@example.nl"]
    assert database.value("SELECT COUNT(*) FROM spamassassin.userpref") == 0
    assert database.value("SELECT COUNT(*) FROM last_login") == 0
    # The forward itself stays, but there is no mailbox left to keep a copy in.
    assert forwards.list_forwards(database) == [Forward("info@example.nl", "piet@example.nl", send_as=False)]
    assert database.value("SELECT COUNT(*) FROM virtual_aliases WHERE source = destination") == 0


def test_delete_refuses_an_unknown_address(database):
    with pytest.raises(MailctlError, match="isn't an account on this server"):
        addresses.delete(database, "info@example.nl")
