import json

import pytest

from mailctl.core import accountfile
from mailctl.core.accountfile import Account
from mailctl.core.errors import MailctlError

PASSWORD = "correct horse battery"


def write(tmp_path, content, name="accounts.json"):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_addresses_with_their_passwords_are_read(tmp_path):
    path = write(tmp_path, {"info@example.nl": PASSWORD, "Sales@Example.NL": "another one entirely"})

    assert accountfile.read(path) == [
        Account("info@example.nl", PASSWORD),
        Account("sales@example.nl", "another one entirely"),  # the address is what this server stores
    ]


def test_a_list_of_entries_is_read_too(tmp_path):
    path = write(tmp_path, [{"address": "info@example.nl", "password": PASSWORD},
                            {"email": "sales@example.nl", "Password": PASSWORD}])

    assert [account.address for account in accountfile.read(path)] == ["info@example.nl", "sales@example.nl"]


def test_an_address_that_isnt_one_is_named_with_its_place(tmp_path):
    path = write(tmp_path, [{"address": "info@example.nl", "password": PASSWORD},
                            {"address": "not an address", "password": PASSWORD}])

    with pytest.raises(MailctlError, match="Entry 2: 'not an address' isn't a valid e-mail address"):
        accountfile.read(path)


def test_a_password_this_server_refuses_is_refused_before_anything_is_created(tmp_path):
    path = write(tmp_path, {"info@example.nl": "short"})

    with pytest.raises(MailctlError, match="The entry for info@example.nl: The password needs at least"):
        accountfile.read(path)


def test_an_entry_without_a_password_says_which(tmp_path):
    path = write(tmp_path, [{"address": "info@example.nl"}])

    with pytest.raises(MailctlError, match="Entry 1 has no password"):
        accountfile.read(path)


def test_a_password_that_isnt_text_is_refused(tmp_path):
    path = write(tmp_path, [{"address": "info@example.nl", "password": 12345678}])

    with pytest.raises(MailctlError, match="The password in entry 1 isn't text"):
        accountfile.read(path)


def test_the_same_address_twice_is_refused(tmp_path):
    path = write(tmp_path, [{"address": "info@example.nl", "password": PASSWORD},
                            {"address": "INFO@example.nl", "password": "a different one"}])

    with pytest.raises(MailctlError, match="has info@example.nl twice"):
        accountfile.read(path)


def test_a_file_that_isnt_json_says_where_it_goes_wrong(tmp_path):
    path = write(tmp_path, '{"info@example.nl": "a password",\n"sales@example.nl"\n}')

    with pytest.raises(MailctlError, match="isn't JSON: .* on line 3"):
        accountfile.read(path)


def test_json_that_isnt_accounts_is_refused(tmp_path):
    with pytest.raises(MailctlError, match="neither a list of accounts nor addresses"):
        accountfile.read(write(tmp_path, '"just some text"'))  # valid JSON, but not accounts
    with pytest.raises(MailctlError, match="Entry 1 isn't an address with a password"):
        accountfile.read(write(tmp_path, ["info@example.nl", "sales@example.nl"]))


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(MailctlError, match="There is no file"):
        accountfile.read(tmp_path / "gone.json")


def test_a_file_too_large_to_be_accounts_is_refused(tmp_path):
    path = write(tmp_path, '{"info@example.nl": "' + "x" * accountfile.MAX_SIZE + '"}')

    with pytest.raises(MailctlError, match="larger than"):
        accountfile.read(path)


def test_a_file_others_can_read_is_recognised(tmp_path):
    path = write(tmp_path, {"info@example.nl": PASSWORD})

    path.chmod(0o600)
    assert accountfile.readable_by_others(path) is False
    path.chmod(0o644)
    assert accountfile.readable_by_others(path) is True
