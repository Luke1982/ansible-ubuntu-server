import pytest

from mailctl.core import names
from mailctl.core.errors import MailctlError


@pytest.mark.parametrize(
    "value, expected",
    [
        ("example.nl", "example.nl"),
        ("  Mail.Example.NL ", "mail.example.nl"),
        ("my-shop.co.uk", "my-shop.co.uk"),
        ("xn--bcher-kva.de", "xn--bcher-kva.de"),
    ],
)
def test_domain_normalises_valid_names(value, expected):
    assert names.domain(value) == expected


@pytest.mark.parametrize(
    "value", ["", "localhost", "-shop.nl", "shop-.nl", "sh op.nl", "shop..nl", ".nl", "example.nl.", "192.168.0.1"]
)
def test_domain_rejects_invalid_names(value):
    with pytest.raises(MailctlError, match="valid domain"):
        names.domain(value)


def test_domain_rejects_names_longer_than_the_database_holds():
    assert names.domain("a" * 47 + ".nl")

    with pytest.raises(MailctlError, match="at most 50 characters"):
        names.domain("a" * 48 + ".nl")


def test_valid_domain_tells_domain_names_from_other_text():
    assert names.valid_domain("example.nl")
    assert not names.valid_domain("old keys")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Info@Example.NL", "info@example.nl"),
        (" piet.jansen+news@shop.example.nl ", "piet.jansen+news@shop.example.nl"),
        ("no_reply-2@example.nl", "no_reply-2@example.nl"),
    ],
)
def test_address_normalises_valid_addresses(value, expected):
    assert names.address(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "info",
        "@example.nl",
        "info@",
        "info@@example.nl",
        "in fo@example.nl",
        ".info@example.nl",
        "info.@example.nl",
        "in..fo@example.nl",
        "in%fo@example.nl",
        "info@localhost",
        "x" * 65 + "@example.nl",
    ],
)
def test_address_rejects_invalid_addresses(value):
    with pytest.raises(MailctlError, match="valid e-mail address"):
        names.address(value)


def test_address_rejects_addresses_longer_than_the_database_holds():
    assert names.address("a" * 64 + "@" + "b" * 32 + ".nl")

    with pytest.raises(MailctlError, match="at most 100 characters"):
        names.address("a" * 64 + "@" + "b" * 33 + ".nl")


def test_split_returns_local_part_and_domain():
    assert names.split("info@example.nl") == ("info", "example.nl")


def test_is_address_tells_addresses_from_domains():
    assert names.is_address("info@example.nl")
    assert not names.is_address("example.nl")
