"""The check that OpenLiteSpeed serves a site's challenges before certbot asks Let's Encrypt to."""

from ipaddress import ip_address

import pytest

from domainctl.core import serving
from serverctl.errors import CtlError

V4, V6 = ip_address("81.4.127.10"), ip_address("2a02:4780:7e:6327::1")


@pytest.fixture
def answering(monkeypatch):
    """answering({address: answer}) makes every request to that address get that answer."""
    monkeypatch.setattr(serving, "POLL", 0)

    def install(answers):
        monkeypatch.setattr(serving, "serves", lambda address, name, file_name, token: answers[address])

    return install


def test_an_address_where_nothing_answers_is_left_to_lets_encrypt(tmp_path, answering):
    """OpenLiteSpeed listens on IPv4 only: Let's Encrypt tries IPv6, gets no answer, and uses IPv4."""
    answering({V4: serving.SERVED, V6: serving.NO_ANSWER})

    lines = serving.wait_until_served(tmp_path, "web03.example.nl", {V4, V6}, timeout=60)

    assert len(lines) == 1 and str(V6) in lines[0]


def test_a_site_served_nowhere_is_an_error(tmp_path, answering):
    answering({V4: serving.NO_ANSWER, V6: serving.NO_ANSWER})

    with pytest.raises(CtlError, match="doesn't serve"):
        serving.wait_until_served(tmp_path, "web03.example.nl", {V4, V6}, timeout=0)


def test_another_site_answering_is_an_error(tmp_path, answering):
    """Let's Encrypt fails on an answer that isn't the challenge, instead of trying the other address."""
    answering({V4: serving.SERVED, V6: serving.ANOTHER_SITE})

    with pytest.raises(CtlError, match="Something else answers .* at 2a02"):
        serving.wait_until_served(tmp_path, "web03.example.nl", {V4, V6}, timeout=0)


def test_the_probe_is_removed_again(tmp_path, answering):
    answering({V4: serving.SERVED})

    serving.wait_until_served(tmp_path, "web03.example.nl", {V4})

    assert list(serving.challenge_dir(tmp_path).iterdir()) == []
