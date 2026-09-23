from dataclasses import replace
from ipaddress import ip_address

import pytest

from domainctl.core import dnsnames
from domainctl.core.dnsnames import State
from serverctl import transip
from serverctl.dns import LookupFailed
from serverctl.errors import CtlError
from serverctl.transip import Entry

HERE = {ip_address("81.4.127.10"), ip_address("2a01:7c8:aab1::10")}
THERE = ip_address("45.87.2.9")


class FakeResolver:
    def __init__(self, answers):
        self._answers = answers

    def addresses(self, name):
        answer = self._answers.get(name, set())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def txt(self, name): return []
    def mx(self, name): return []
    def srv(self, name): return []
    def ptr(self, address): return []


def test_a_name_pointing_only_here_is_ready():
    found = dnsnames.check(FakeResolver({"example.nl": HERE}), "example.nl", HERE)
    assert found.state is State.HERE and found.ready
    assert found.addresses == frozenset(HERE)


def test_a_name_with_no_record_is_missing():
    found = dnsnames.check(FakeResolver({}), "www.example.nl", HERE)
    assert found.state is State.MISSING
    assert "no A or AAAA record" in found.detail


def test_a_name_pointing_somewhere_else_is_not_taken_over():
    found = dnsnames.check(FakeResolver({"example.nl": {THERE}}), "example.nl", HERE)
    assert found.state is State.ELSEWHERE
    assert "45.87.2.9" in found.detail


def test_one_foreign_address_among_ours_is_still_not_ready():
    """Let's Encrypt may check any of them, so a stray AAAA elsewhere fails the whole request."""
    found = dnsnames.check(FakeResolver({"example.nl": {*HERE, THERE}}), "example.nl", HERE)
    assert found.state is State.ELSEWHERE
    assert "also points to" in found.detail


def test_a_failed_lookup_is_not_read_as_a_missing_record():
    resolver = FakeResolver({"example.nl": LookupFailed("the A lookup for example.nl failed: timed out")})
    found = dnsnames.check(resolver, "example.nl", HERE)
    assert found.state is State.UNKNOWN and not found.ready


def test_only_addresses_reachable_from_the_internet_are_published():
    ips = {ip_address("81.4.127.10"), ip_address("10.0.0.5"), ip_address("127.0.0.1")}
    assert dnsnames.publishable_ips(ips) == {ip_address("81.4.127.10")}


@pytest.mark.parametrize("zone, name, expected", [
    ("example.nl", "example.nl", "@"),
    ("example.nl", "www.example.nl", "www"),
    ("example.nl", "shop.example.nl", "shop"),
    ("example.nl", "www.shop.example.nl", "www.shop"),
    ("Example.NL", "WWW.example.nl", "www"),
])
def test_a_name_is_written_the_way_transip_writes_it(zone, name, expected):
    assert dnsnames.relative(zone, name) == expected


class FakeClient:
    login = "someone"

    def __init__(self, zones, nameservers=("ns0.transip.net", "ns1.transip.nl"), changed_to=None):
        self.zones = zones
        self._nameservers = list(nameservers)
        self.changed_to = changed_to  # what a second read gives back, as if someone else saved meanwhile
        self.reads = 0
        self.saved = None

    def dns_entries(self, zone):
        if zone not in self.zones:
            raise transip.NotInAccount(f"{zone} isn't in the account")
        self.reads += 1
        if self.changed_to is not None and self.reads > 1:
            return list(self.changed_to)
        return list(self.zones[zone])

    def nameservers(self, zone):
        return self._nameservers

    def replace_dns_entries(self, zone, entries):
        self.saved = (zone, tuple(entries))


def test_the_zone_is_the_nearest_parent_domain_in_the_account():
    client = FakeClient({"example.nl": []})
    assert dnsnames.find_zone(client, "www.shop.example.nl")[0] == "example.nl"


def test_a_domain_outside_the_account_says_where_to_publish_instead():
    with pytest.raises(transip.NotInAccount, match="isn't in the TransIP account"):
        dnsnames.find_zone(FakeClient({}), "example.nl")


def test_publishing_adds_an_a_and_an_aaaa_record_for_the_name():
    client = FakeClient({"example.nl": [Entry("@", 3600, "MX", "10 mail.example.nl.")]})
    zone, added = dnsnames.publish(client, "www.example.nl", HERE)
    assert zone == "example.nl"
    assert {(entry.name, entry.type, entry.content) for entry in added} == {
        ("www", "A", "81.4.127.10"), ("www", "AAAA", "2a01:7c8:aab1::10")}
    # The records that were already there are kept.
    assert client.saved[1][0].type == "MX"


def test_a_record_pointing_elsewhere_is_never_replaced():
    client = FakeClient({"example.nl": [Entry("www", 3600, "A", "45.87.2.9")]})
    with pytest.raises(CtlError, match="already has a record"):
        dnsnames.publish(client, "www.example.nl", HERE)
    assert client.saved is None


def test_a_cname_in_the_way_is_left_alone_too():
    client = FakeClient({"example.nl": [Entry("www", 3600, "CNAME", "elsewhere.example.com.")]})
    with pytest.raises(CtlError, match="already has a record"):
        dnsnames.publish(client, "www.example.nl", HERE)


def test_publishing_into_a_zone_the_internet_does_not_read_is_refused():
    client = FakeClient({"example.nl": []}, nameservers=("ns1.otherhost.com", "ns2.otherhost.com"))
    with pytest.raises(CtlError, match="doesn't use TransIP's nameservers"):
        dnsnames.publish(client, "example.nl", HERE)
    assert client.saved is None


def test_a_zone_changed_in_the_meantime_is_not_overwritten():
    """The whole zone is written back at once, so a change made in the control panel meanwhile would be lost."""
    client = FakeClient({"example.nl": [Entry("@", 3600, "A", "81.4.127.10")]},
                        changed_to=[Entry("@", 3600, "TXT", "someone else was here")])
    with pytest.raises(CtlError, match="changed in the meantime"):
        dnsnames.publish(client, "www.example.nl", HERE)
    assert client.saved is None


def test_waiting_stops_as_soon_as_the_name_resolves_here():
    resolver = FakeResolver({"example.nl": HERE})
    found = dnsnames.wait_until_resolving(resolver, "example.nl", HERE, wait=0, poll=0)
    assert found.ready


def test_waiting_gives_up_and_says_what_it_found():
    found = dnsnames.wait_until_resolving(FakeResolver({}), "example.nl", HERE, wait=0, poll=0)
    assert found.state is State.MISSING


def test_a_different_transip_login_kept_by_mailctl_is_pointed_out(config, tmp_path, monkeypatch, capsys):
    """Until mailctl reads the shared files, a key replaced there looks like a refusal with no reason."""
    from domainctl.commands import dns as dns_command
    from domainctl.session import Session

    shared = tmp_path / "transip"
    shared.mkdir()
    (shared / "transip.key").write_text("the key domainctl has")
    (shared / "transip.json").write_text('{"login": "someone", "global_key": false}')
    mailctl_dir = tmp_path / "mailctl"
    mailctl_dir.mkdir()
    (mailctl_dir / "transip.key").write_text("a newer key")
    monkeypatch.setattr(dns_command, "MAILCTL_TRANSIP", mailctl_dir)

    settings = replace(config, transip_settings=shared / "transip.json", transip_key=shared / "transip.key")
    monkeypatch.setattr(transip, "saved_credentials", lambda access: transip.Credentials("someone", access.key))
    dns_command.saved_or_asked(Session(settings))
    assert "different TransIP login" in capsys.readouterr().out


def test_nothing_is_said_when_mailctl_has_the_same_login(config, tmp_path, monkeypatch, capsys):
    from domainctl.commands import dns as dns_command
    from domainctl.session import Session

    shared = tmp_path / "transip"
    shared.mkdir()
    (shared / "transip.key").write_text("the same key")
    (shared / "transip.json").write_text('{"login": "someone", "global_key": false}')
    mailctl_dir = tmp_path / "mailctl"
    mailctl_dir.mkdir()
    (mailctl_dir / "transip.key").write_text("the same key")
    (mailctl_dir / "transip.json").write_text('{"login": "someone", "global_key": false}')
    monkeypatch.setattr(dns_command, "MAILCTL_TRANSIP", mailctl_dir)

    settings = replace(config, transip_settings=shared / "transip.json", transip_key=shared / "transip.key")
    monkeypatch.setattr(transip, "saved_credentials", lambda access: transip.Credentials("someone", access.key))
    dns_command.saved_or_asked(Session(settings))
    assert capsys.readouterr().out == ""


def test_nothing_is_said_on_a_server_without_mailctl(config, tmp_path, monkeypatch, capsys):
    from domainctl.commands import dns as dns_command
    from domainctl.session import Session

    shared = tmp_path / "transip"
    shared.mkdir()
    (shared / "transip.key").write_text("the key")
    monkeypatch.setattr(dns_command, "MAILCTL_TRANSIP", tmp_path / "not-installed")
    settings = replace(config, transip_settings=shared / "transip.json", transip_key=shared / "transip.key")
    monkeypatch.setattr(transip, "saved_credentials", lambda access: transip.Credentials("someone", access.key))
    dns_command.saved_or_asked(Session(settings))
    assert capsys.readouterr().out == ""
