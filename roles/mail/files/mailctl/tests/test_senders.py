import pytest

from mailctl.core import domains, senders
from mailctl.core.errors import MailctlError


@pytest.fixture(autouse=True)
def example_domain(database):
    domains.add(database, "example.nl")


def permission_rows(database):
    return database.value("SELECT COUNT(*) FROM virtual_sender_aliases")


def add_raw_permission(database, users, alias):
    database.execute(
        "INSERT INTO virtual_sender_aliases (domain_id, users, alias) SELECT id, %s, %s FROM virtual_domains",
        users, alias,
    )


def test_allow_creates_the_permission_and_adds_logins_once(database):
    senders.allow(database, "info@example.nl", "sales@example.nl")
    senders.allow(database, "piet@gmail.com", "sales@example.nl")
    senders.allow(database, "piet@gmail.com", "sales@example.nl")

    assert senders.logins_for(database, "sales@example.nl") == ["info@example.nl", "piet@gmail.com"]
    assert permission_rows(database) == 1


def test_allow_refuses_an_address_whose_domain_isnt_on_this_server(database):
    with pytest.raises(MailctlError, match="isn't a domain on this server"):
        senders.allow(database, "info@example.nl", "sales@other.nl")


def test_allow_refuses_more_logins_than_the_database_column_holds(database):
    for number in range(11):
        senders.allow(database, f"employee{number:02}@example.nl", "shop@example.nl")

    with pytest.raises(MailctlError, match="Too many accounts may send as shop@example.nl"):
        senders.allow(database, "employee11@example.nl", "shop@example.nl")

    assert len(senders.logins_for(database, "shop@example.nl")) == 11


def test_logins_for_reads_comma_and_space_separated_lists(database):
    add_raw_permission(database, "a@example.nl, b@example.nl c@example.nl", "sales@example.nl")

    assert senders.logins_for(database, "sales@example.nl") == ["a@example.nl", "b@example.nl", "c@example.nl"]
    assert senders.logins_for(database, "info@example.nl") == []


def test_permissions_maps_each_address_to_its_logins_in_lowercase(database):
    add_raw_permission(database, "Info@Example.nl,piet@example.nl", "Sales@example.nl")

    assert senders.permissions(database) == {"sales@example.nl": {"info@example.nl", "piet@example.nl"}}


def test_revoke_removes_one_login_and_deletes_the_permission_once_empty(database):
    senders.allow(database, "info@example.nl", "sales@example.nl")
    senders.allow(database, "piet@example.nl", "sales@example.nl")

    assert senders.revoke(database, "info@example.nl", "sales@example.nl")
    assert senders.logins_for(database, "sales@example.nl") == ["piet@example.nl"]

    assert senders.revoke(database, "piet@example.nl", "sales@example.nl")
    assert permission_rows(database) == 0


def test_revoke_ignores_a_login_without_permission(database):
    senders.allow(database, "info@example.nl", "sales@example.nl")

    assert not senders.revoke(database, "piet@example.nl", "sales@example.nl")

    assert senders.logins_for(database, "sales@example.nl") == ["info@example.nl"]


def test_revoke_matches_logins_written_in_another_case(database):
    add_raw_permission(database, "Info@Example.nl,piet@example.nl", "sales@example.nl")

    senders.revoke(database, "info@example.nl", "sales@example.nl")

    assert senders.logins_for(database, "sales@example.nl") == ["piet@example.nl"]


def test_forget_login_revokes_it_everywhere_and_keeps_other_logins(database):
    senders.allow(database, "info@example.nl", "info@example.nl")
    senders.allow(database, "info@example.nl", "sales@example.nl")
    senders.allow(database, "piet@example.nl", "sales@example.nl")
    # A login whose name contains another one mustn't be touched.
    senders.allow(database, "xinfo@example.nl", "support@example.nl")

    senders.forget_login(database, "info@example.nl")

    assert senders.logins_for(database, "info@example.nl") == []
    assert senders.logins_for(database, "sales@example.nl") == ["piet@example.nl"]
    assert senders.logins_for(database, "support@example.nl") == ["xinfo@example.nl"]
