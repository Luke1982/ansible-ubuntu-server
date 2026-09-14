import pytest
from conftest import PASSWORD

from mailctl.core import addresses, domains, forwards, senders
from mailctl.core.domains import Domain
from mailctl.core.errors import MailctlError


def test_add_and_list_domains_with_their_counts(database):
    domains.add(database, "b.nl")
    domains.add(database, "a.nl")
    addresses.add(database, "info@a.nl", PASSWORD)
    forwards.add(database, "sales@a.nl", "info@a.nl")
    forwards.add(database, "info@a.nl", "info@gmail.com")

    assert domains.list_domains(database) == [
        Domain("a.nl", addresses=1, forwards=2),
        Domain("b.nl", addresses=0, forwards=0),
    ]


def test_get_gives_one_domain_with_its_counts(database):
    domains.add(database, "example.nl")
    domains.add(database, "other.nl")
    addresses.add(database, "info@example.nl", PASSWORD)

    assert domains.get(database, "example.nl") == Domain("example.nl", addresses=1, forwards=0)
    with pytest.raises(MailctlError, match="unknown.nl isn't a domain on this server"):
        domains.get(database, "unknown.nl")


def test_add_refuses_an_existing_domain(database):
    domains.add(database, "example.nl")

    with pytest.raises(MailctlError, match="example.nl already exists"):
        domains.add(database, "example.nl")


def test_exists(database):
    domains.add(database, "example.nl")

    assert domains.exists(database, "example.nl")
    assert not domains.exists(database, "other.nl")


def test_id_of_and_require_report_an_unknown_domain(database):
    domains.add(database, "example.nl")
    domains.require(database, "example.nl")

    with pytest.raises(MailctlError, match="other.nl isn't a domain on this server"):
        domains.id_of(database, "other.nl")
    with pytest.raises(MailctlError, match="other.nl isn't a domain on this server"):
        domains.require(database, "other.nl")


def test_duplicate_domain_rows_from_the_old_helper_script_count_as_one_domain(database):
    database.execute("INSERT INTO virtual_domains (name) VALUES ('example.nl'), ('example.nl')")

    addresses.add(database, "info@example.nl", PASSWORD)

    assert domains.list_domains(database) == [Domain("example.nl", addresses=1, forwards=0)]


def test_delete_removes_the_domain_with_its_forwards_permissions_and_spam_settings(database):
    domains.add(database, "example.nl")
    domains.add(database, "other.nl")
    forwards.add(database, "sales@example.nl", "someone@gmail.com")
    senders.allow(database, "someone@gmail.com", "sales@example.nl")
    database.execute(
        "INSERT INTO spamassassin.userpref (username, preference, value, prefid)"
        " VALUES ('%example.nl', 'required_score', '4', 1), ('%other.nl', 'required_score', '6', 2)"
    )

    domains.delete(database, "example.nl")

    assert [domain.name for domain in domains.list_domains(database)] == ["other.nl"]
    assert database.value("SELECT COUNT(*) FROM virtual_aliases") == 0
    assert database.value("SELECT COUNT(*) FROM virtual_sender_aliases") == 0
    assert database.rows("SELECT username FROM spamassassin.userpref") == [{"username": "%other.nl"}]


def test_delete_refuses_a_domain_that_still_has_addresses(database):
    domains.add(database, "example.nl")
    addresses.add(database, "info@example.nl", PASSWORD)

    with pytest.raises(MailctlError, match="example.nl still has addresses"):
        domains.delete(database, "example.nl")


def test_delete_refuses_an_unknown_domain(database):
    with pytest.raises(MailctlError, match="isn't a domain on this server"):
        domains.delete(database, "example.nl")
