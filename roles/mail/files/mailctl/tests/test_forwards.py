import pytest
from conftest import PASSWORD

from mailctl.core import addresses, domains, forwards, senders
from mailctl.core.errors import MailctlError
from mailctl.core.forwards import Forward


@pytest.fixture(autouse=True)
def example_domain(database):
    domains.add(database, "example.nl")
    addresses.add(database, "piet@example.nl", PASSWORD)


def alias_rows(database):
    return {
        (row["source"], row["destination"])
        for row in database.rows("SELECT source, destination FROM virtual_aliases")
    }


def test_add_and_list_forwards(database):
    forwards.add(database, "sales@example.nl", "someone@gmail.com")
    forwards.add(database, "sales@example.nl", "piet@example.nl")

    assert forwards.list_forwards(database) == [
        Forward("sales@example.nl", "piet@example.nl", send_as=False),
        Forward("sales@example.nl", "someone@gmail.com", send_as=False),
    ]


def test_list_shows_whether_the_destination_may_send_as_the_source(database):
    forwards.add(database, "sales@example.nl", "piet@example.nl")
    senders.allow(database, "Piet@Example.nl", "sales@example.nl")

    assert forwards.list_forwards(database) == [Forward("sales@example.nl", "piet@example.nl", send_as=True)]


def test_list_filters_by_domain(database):
    domains.add(database, "other.nl")
    forwards.add(database, "sales@example.nl", "piet@example.nl")
    forwards.add(database, "sales@other.nl", "piet@example.nl")

    assert [forward.source for forward in forwards.list_forwards(database, "other.nl")] == ["sales@other.nl"]


@pytest.mark.parametrize(
    "source, destination, problem",
    [
        ("piet@example.nl", "piet@example.nl", "piet@example.nl can't forward to itself"),
        ("sales@other.nl", "piet@example.nl", "other.nl isn't a domain on this server"),
        ("sales@example.nl", "someone@gmail.com", "sales@example.nl already forwards to someone@gmail.com"),
    ],
)
def test_check_new_and_add_refuse_forwards_that_cant_be_added(database, source, destination, problem):
    forwards.add(database, "sales@example.nl", "someone@gmail.com")

    with pytest.raises(MailctlError, match=problem):
        forwards.check_new(database, source, destination)
    with pytest.raises(MailctlError, match=problem):
        forwards.add(database, source, destination)


def test_forwarding_an_account_keeps_delivering_to_its_mailbox(database):
    forwards.add(database, "piet@example.nl", "piet@gmail.com")
    forwards.add(database, "piet@example.nl", "jan@gmail.com")

    assert alias_rows(database) == {
        ("piet@example.nl", "piet@example.nl"),
        ("piet@example.nl", "piet@gmail.com"),
        ("piet@example.nl", "jan@gmail.com"),
    }
    assert len(forwards.list_forwards(database)) == 2


def test_an_account_forwarded_without_a_copy_before_mailctl_is_left_that_way(database):
    database.execute(
        "INSERT INTO virtual_aliases (domain_id, source, destination)"
        " SELECT id, 'piet@example.nl', 'piet@gmail.com' FROM virtual_domains"
    )

    forwards.add(database, "piet@example.nl", "jan@gmail.com")

    assert ("piet@example.nl", "piet@example.nl") not in alias_rows(database)


def test_keep_copy_and_drop_copy(database):
    forwards.keep_copy(database, "piet@example.nl")
    assert alias_rows(database) == set()

    database.execute(
        "INSERT INTO virtual_aliases (domain_id, source, destination)"
        " SELECT id, 'piet@example.nl', 'piet@gmail.com' FROM virtual_domains"
    )
    forwards.keep_copy(database, "piet@example.nl")
    forwards.keep_copy(database, "piet@example.nl")
    assert ("piet@example.nl", "piet@example.nl") in alias_rows(database)

    forwards.drop_copy(database, "piet@example.nl")
    assert alias_rows(database) == {("piet@example.nl", "piet@gmail.com")}


def test_delete_removes_the_forward_and_the_destinations_send_as_permission(database):
    forwards.add(database, "sales@example.nl", "piet@example.nl")
    forwards.add(database, "sales@example.nl", "jan@gmail.com")
    senders.allow(database, "piet@example.nl", "sales@example.nl")

    assert forwards.delete(database, "sales@example.nl", "piet@example.nl")
    assert not forwards.delete(database, "sales@example.nl", "jan@gmail.com")

    assert forwards.list_forwards(database) == []
    assert senders.logins_for(database, "sales@example.nl") == []


def test_deleting_the_last_forward_of_an_account_removes_its_mailbox_copy(database):
    forwards.add(database, "piet@example.nl", "piet@gmail.com")
    forwards.add(database, "piet@example.nl", "jan@gmail.com")

    forwards.delete(database, "piet@example.nl", "piet@gmail.com")
    assert ("piet@example.nl", "piet@example.nl") in alias_rows(database)

    forwards.delete(database, "piet@example.nl", "jan@gmail.com")
    assert alias_rows(database) == set()


@pytest.mark.parametrize("destination", ["piet@example.nl", "someone@gmail.com"])
def test_delete_refuses_a_forward_that_doesnt_exist(database, destination):
    forwards.add(database, "sales@example.nl", "someone@gmail.com")
    forwards.delete(database, "sales@example.nl", "someone@gmail.com")

    with pytest.raises(MailctlError, match=f"sales@example.nl doesn't forward to {destination}"):
        forwards.delete(database, "sales@example.nl", destination)


def test_to_domain_lists_forwards_from_other_domains(database):
    domains.add(database, "other.nl")
    forwards.add(database, "sales@other.nl", "piet@example.nl")
    forwards.add(database, "sales@example.nl", "piet@example.nl")
    forwards.add(database, "info@other.nl", "someone@gmail.com")

    assert forwards.to_domain(database, "example.nl") == [Forward("sales@other.nl", "piet@example.nl", False)]


def test_to_address_and_from_address(database):
    forwards.add(database, "sales@example.nl", "piet@example.nl")
    forwards.add(database, "piet@example.nl", "someone@gmail.com")

    assert forwards.to_address(database, "piet@example.nl") == [Forward("sales@example.nl", "piet@example.nl", False)]
    assert forwards.from_address(database, "piet@example.nl") == [Forward("piet@example.nl", "someone@gmail.com", False)]
