import socket
from ipaddress import ip_address

import pytest

from mailctl.core import reach
from mailctl.core.dns_check import LookupFailed, Status

# The real probe, kept before conftest's fixture replaces it: these two tests are about what it does itself.
REAL_PROBE = reach.answers
HERE = ip_address("203.0.113.5")
IPV6 = ip_address("2001:db8::5")


class FakeDns:
    """mail.example.nl has both addresses, webmail.example.nl only the IPv4 one, gone.example.nl can't be looked up."""

    def addresses(self, name):
        if name == "gone.example.nl":
            raise LookupFailed("The A lookup for gone.example.nl timed out.")
        return {"mail.example.nl": {HERE, IPV6}, "webmail.example.nl": {HERE}}.get(name, set())


def answering(*silent):
    return lambda address, port: (address, port) not in silent


def test_a_name_whose_addresses_all_answer_is_fine():
    found = reach.check({"mail.example.nl": 993}, FakeDns(), probe=answering())

    assert found.status is Status.OK
    assert found.detail == "Everything these names point to answers."


def test_an_address_that_doesnt_answer_is_named_with_its_port():
    found = reach.check({"mail.example.nl": 993}, FakeDns(), probe=answering((IPV6, 993)))

    assert found.status is Status.WARN
    assert found.detail.startswith("mail.example.nl at 2001:db8::5 doesn't answer on port 993.")
    assert "Take the record away, or let the server answer there." in found.detail


def test_every_silent_address_is_named():
    found = reach.check({"mail.example.nl": 993, "webmail.example.nl": 443}, FakeDns(),
                        probe=answering((IPV6, 993), (HERE, 443)))

    assert "mail.example.nl at 2001:db8::5 doesn't answer on port 993" in found.detail
    assert "webmail.example.nl at 203.0.113.5 doesn't answer on port 443" in found.detail


def test_a_name_that_cant_be_looked_up_is_left_to_the_dns_checks():
    assert reach.check({"gone.example.nl": 993}, FakeDns(), probe=answering()) is None


def test_names_without_records_give_nothing_to_report():
    assert reach.check({"nothing.example.nl": 443}, FakeDns(), probe=answering()) is None


def test_the_probe_says_whether_something_accepts_a_connection():
    """Against a real socket: the check is about what a mail program's connection does."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        port = listening.getsockname()[1]

        assert REAL_PROBE(ip_address("127.0.0.1"), port, timeout=2) is True

    assert REAL_PROBE(ip_address("127.0.0.1"), port, timeout=2) is False  # nothing listens there any more


@pytest.mark.parametrize("address", ["203.0.113.5", "2001:db8::5"])
def test_an_address_nothing_routes_to_is_not_waited_for_forever(address):
    """Documentation ranges: no connection is made, and the timeout is what decides how long that takes."""
    assert REAL_PROBE(ip_address(address), 993, timeout=0.2) is False
