import pytest

from serverctl import names
from serverctl.errors import CtlError


@pytest.mark.parametrize("domain, expected", [
    ("example.nl", "example"),
    ("casabarata.nl", "casabarata"),
    ("shop.example.co.uk", "shopexample"),
    ("my-site.com", "mysite"),
    ("a.b.c.example.org", "abcexample"),
])
def test_user_name_for_drops_the_public_suffix(domain, expected):
    assert names.user_name_for(domain) == expected


def test_user_name_for_stays_within_the_length_of_a_linux_user_name():
    suggested = names.user_name_for("averyveryverylongdomainnamethatkeepsgoingandgoing.example.com")
    assert len(suggested) <= names.MAX_USER_NAME_LENGTH
    assert names.valid_user_name(suggested)


@pytest.mark.parametrize("name", ["example", "site1", "my-site", "a_b"])
def test_valid_user_names(name):
    assert names.user_name(name) == name


@pytest.mark.parametrize("name", ["-leading", "_leading", "Has Space", "a" * 33, "", "bad/name"])
def test_invalid_user_names_are_refused(name):
    with pytest.raises(CtlError):
        names.user_name(name)


def test_a_web_domain_may_be_longer_than_a_mail_domain():
    long_domain = "sub.averylongdomainnameusedforawebsite-only.example.com"
    assert len(long_domain) > names.MAX_DOMAIN_LENGTH
    with pytest.raises(CtlError):
        names.domain(long_domain)
    assert names.domain(long_domain, names.MAX_DNS_NAME_LENGTH) == long_domain
