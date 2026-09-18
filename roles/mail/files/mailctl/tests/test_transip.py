import http.client
import subprocess
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from mailctl.core import transip
from mailctl.core.errors import MailctlError
from mailctl.core.zone import Entry


@pytest.fixture
def credentials(transip_key_pair):
    return transip.Credentials("mailadmin", transip_key_pair[0])


def client(credentials, read_only=True):
    return transip.Client(credentials, hostname="server.hosting.example", read_only=read_only)


def test_the_client_logs_in_with_a_signed_request_and_uses_the_token(credentials, fake_transip):
    fake_transip.zones["example.nl"] = [{"name": "@", "expire": 300, "type": "MX", "content": "10 mx.transip.email."}]

    entries = client(credentials, read_only=False).dns_entries("example.nl")

    assert entries == [Entry("@", 300, "MX", "10 mx.transip.email.")]
    assert fake_transip.paths() == ["/auth", "/domains/example.nl/dns"]
    login = fake_transip.requests[0][2]
    assert {key: login[key] for key in ("login", "read_only", "expiration_time", "global_key")} == {
        "login": "mailadmin", "read_only": False, "expiration_time": "30 minutes", "global_key": False,
    }
    assert len(login["nonce"]) == 32
    assert "server.hosting.example" in login["label"]


def test_a_client_that_only_reads_asks_for_a_read_only_token(credentials, fake_transip):
    client(credentials).nameservers("example.nl")

    assert fake_transip.requests[0][2]["read_only"] is True


def test_a_global_key_asks_for_a_token_that_works_from_anywhere(credentials, fake_transip):
    client(replace(credentials, global_key=True)).nameservers("example.nl")

    assert fake_transip.requests[0][2]["global_key"] is True


def test_the_client_logs_in_once(credentials, fake_transip):
    api = client(credentials)
    api.nameservers("example.nl")
    api.dns_entries("example.nl")

    assert fake_transip.paths("POST") == ["/auth"]


def test_replacing_the_entries_sends_the_whole_zone(credentials, fake_transip):
    entries = (Entry("@", 3600, "MX", "10 mail.example.nl."), Entry("www", 300, "CNAME", "@"))

    client(credentials, read_only=False).replace_dns_entries("example.nl", entries)

    assert fake_transip.entries() == [("@", 3600, "MX", "10 mail.example.nl."), ("www", 300, "CNAME", "@")]


def test_a_refused_login_explains_the_whitelist(credentials, fake_transip):
    fake_transip.answers[("POST", "/auth")] = [(403, {"error": "Remote IP 203.0.113.5 is not authorized"})]

    with pytest.raises(MailctlError) as problem:
        client(credentials).nameservers("example.nl")

    assert problem.value.message == "TransIP refused to log in as mailadmin: Remote IP 203.0.113.5 is not authorized"
    assert "whitelist" in problem.value.hint


def test_a_login_signed_with_another_key_is_refused(credentials, fake_transip, tmp_path):
    other_key = tmp_path / "other.key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", str(other_key)], check=True, capture_output=True)

    with pytest.raises(MailctlError, match="TransIP refused to log in as mailadmin: Signature invalid"):
        client(replace(credentials, key=other_key)).nameservers("example.nl")


def test_a_domain_outside_the_account_points_to_the_records_to_publish_by_hand(credentials, fake_transip):
    with pytest.raises(transip.NotInAccount) as problem:
        client(credentials).dns_entries("other.nl")

    assert problem.value.message == "TransIP: Domain with name 'other.nl' not found"
    assert "other.nl isn't in the TransIP account mailadmin" in problem.value.hint
    assert "mailctl dns show other.nl" in problem.value.hint


BUSY = (409, {"error": "Error fetching Dns Entries: DNS Entries are currently being saved"})


def test_reading_a_zone_that_is_still_being_saved_is_tried_again(credentials, fake_transip):
    fake_transip.answers[("GET", "/domains/example.nl/dns")] = [BUSY, BUSY]

    assert client(credentials).dns_entries("example.nl") == []
    assert fake_transip.paths("GET").count("/domains/example.nl/dns") == 3


def test_a_zone_that_stays_busy_gives_transips_error(credentials, fake_transip):
    fake_transip.answers[("GET", "/domains/example.nl/dns")] = [BUSY] * (transip.BUSY_RETRIES + 1)

    with pytest.raises(MailctlError, match="DNS Entries are currently being saved$") as problem:
        client(credentials).dns_entries("example.nl")

    assert "try again in a minute" in problem.value.hint


def test_saving_a_zone_that_is_still_being_saved_isnt_tried_again(credentials, fake_transip):
    # The zone mailctl would save was read before the change TransIP is saving, so saving it would undo that.
    fake_transip.answers[("PUT", "/domains/example.nl/dns")] = [BUSY]

    with pytest.raises(MailctlError, match="currently being saved"):
        client(credentials, read_only=False).replace_dns_entries("example.nl", ())

    assert fake_transip.paths("PUT") == ["/domains/example.nl/dns"]


@pytest.mark.parametrize("request_", [("GET", "/domains/example.nl/dns"), ("POST", "/auth")])
def test_the_rate_limit_says_when_to_try_again(credentials, fake_transip, request_):
    fake_transip.answers[request_] = [(429, {"error": "Too many requests"})]

    with pytest.raises(MailctlError, match="rate limit") as problem:
        client(credentials).dns_entries("example.nl")

    assert "15 minutes" in problem.value.hint


def test_an_answer_that_isnt_json_is_an_error(credentials, fake_transip, monkeypatch):
    monkeypatch.setattr(transip, "_send", lambda *request: (200, b"<html>maintenance</html>"))

    with pytest.raises(MailctlError, match="answer mailctl doesn't understand"):
        client(credentials).dns_entries("example.nl")


@pytest.mark.parametrize("entry", [{"name": "@", "type": "A"}, {"name": "@", "expire": 300, "type": "A", "content": None}])
def test_dns_entries_that_arent_understood_are_an_error(credentials, fake_transip, entry):
    fake_transip.zones["example.nl"] = [entry]

    with pytest.raises(MailctlError, match="answer mailctl doesn't understand"):
        client(credentials).dns_entries("example.nl")


def test_nameservers_that_arent_understood_are_an_error(credentials, fake_transip):
    fake_transip.answers[("GET", "/domains/example.nl/nameservers")] = [(200, {"nameservers": None})]

    with pytest.raises(MailctlError, match="answer mailctl doesn't understand"):
        client(credentials).nameservers("example.nl")


def key_text(transip_key_pair):
    return transip_key_pair[0].read_text()


def test_saved_credentials_are_none_before_they_are_entered(config):
    assert transip.saved_credentials(config) is None


def test_save_credentials_logs_in_before_it_saves_them_for_root_only(config, transip_key_pair, fake_transip):
    saved = transip.save_credentials(config, "mailadmin", key_text(transip_key_pair))

    assert saved == transip.Credentials("mailadmin", config.transip_key, global_key=False)
    assert transip.saved_credentials(config) == saved
    assert config.transip_key.read_text() == key_text(transip_key_pair)
    for path in (config.transip_key, config.transip_settings):
        assert path.stat().st_mode & 0o777 == 0o600
    assert fake_transip.requests[0][2]["read_only"] is True
    # No temporary files are left behind.
    assert sorted(config.transip_key.parent.iterdir()) == sorted([config.transip_settings, config.transip_key])


@pytest.mark.parametrize("refused_on_use", [False, True])
def test_a_key_that_works_from_anywhere_is_saved_as_such(config, transip_key_pair, fake_transip, refused_on_use):
    fake_transip.whitelisted = False
    fake_transip.refused_on_use = refused_on_use

    saved = transip.save_credentials(config, "mailadmin", key_text(transip_key_pair))

    assert saved.global_key is True
    assert [body["global_key"] for _, path, body, _ in fake_transip.requests if path == "/auth"] == [False, True]
    assert transip.saved_credentials(config).global_key is True


def test_a_refused_token_points_to_entering_the_credentials_again(credentials, fake_transip):
    fake_transip.whitelisted = False
    fake_transip.refused_on_use = True

    with pytest.raises(transip.LoginRefused, match="not whitelisted") as problem:
        client(credentials).dns_entries("example.nl")

    assert "mailctl dns credentials" in problem.value.hint


def test_an_outage_at_login_isnt_blamed_on_the_key(credentials, fake_transip):
    fake_transip.answers[("POST", "/auth")] = [(503, {"error": "Service unavailable"})]

    with pytest.raises(MailctlError) as problem:
        client(credentials).dns_entries("example.nl")

    assert not isinstance(problem.value, transip.LoginRefused)
    assert problem.value.message == "TransIP: Service unavailable"


def test_credentials_transip_refuses_are_not_saved(config, transip_key_pair, fake_transip, tmp_path):
    other_key = tmp_path / "other.key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", str(other_key)], check=True, capture_output=True)

    with pytest.raises(transip.LoginRefused, match="TransIP refused to log in as mailadmin: Signature invalid"):
        transip.save_credentials(config, "mailadmin", other_key.read_text())

    assert transip.saved_credentials(config) is None
    assert list(config.transip_key.parent.iterdir()) == []


def test_replacing_credentials_keeps_the_old_ones_until_the_new_ones_work(config, transip_key_pair, fake_transip):
    transip.save_credentials(config, "mailadmin", key_text(transip_key_pair))
    fake_transip.answers[("POST", "/auth")] = [(401, {"error": "Invalid login"}), (401, {"error": "Invalid login"})]

    with pytest.raises(transip.LoginRefused):
        transip.save_credentials(config, "someone-else", key_text(transip_key_pair))

    assert transip.saved_credentials(config).login == "mailadmin"


def test_a_key_copied_with_other_line_breaks_is_repaired(config, transip_key_pair, fake_transip):
    lines = key_text(transip_key_pair).splitlines()
    pasted = f"  {lines[0]}{''.join(lines[1:-1])}\r\n{lines[-1]}  \n"

    transip.save_credentials(config, "mailadmin", pasted)

    assert config.transip_key.read_text() == key_text(transip_key_pair)


@pytest.mark.parametrize("pasted", ["mailadmin", "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----\n",
                                    "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIB\n-----END ENCRYPTED PRIVATE KEY-----\n"])
def test_something_else_than_a_private_key_is_refused(config, pasted):
    with pytest.raises(MailctlError, match="isn't a private key without a passphrase"):
        transip.save_credentials(config, "mailadmin", pasted)


def test_a_damaged_key_is_refused(config):
    with pytest.raises(MailctlError, match="openssl pkey"):
        transip.save_credentials(config, "mailadmin", "-----BEGIN PRIVATE KEY-----\nMIIBroken\n-----END PRIVATE KEY-----\n")

    assert list(config.transip_key.parent.iterdir()) == []


@pytest.mark.parametrize("settings", ["not json", '{"login": 5, "global_key": false}', "[]"])
def test_damaged_saved_credentials_say_how_to_enter_them_again(config, transip_key_pair, settings):
    config.transip_settings.parent.mkdir()
    config.transip_settings.write_text(settings)
    config.transip_key.write_text(key_text(transip_key_pair))

    with pytest.raises(MailctlError) as problem:
        transip.saved_credentials(config)

    assert "mailctl dns credentials" in problem.value.hint


@pytest.mark.parametrize("failure", [urllib.error.URLError("Name or service not known"),
                                     http.client.RemoteDisconnected("Remote end closed connection"),
                                     http.client.IncompleteRead(b"")])
def test_an_unreachable_api_is_explained(monkeypatch, failure):
    def unreachable(request, timeout):
        raise failure

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)

    with pytest.raises(MailctlError, match="^Can't reach TransIP's API: "):
        transip._send("GET", transip.API + "/domains", {}, None)


@pytest.mark.parametrize(
    "nameservers, at_transip",
    [
        (["ns0.transip.net", "ns1.transip.nl", "NS2.TRANSIP.EU."], True),
        (["ns0.transip.net", "ns1.cloudflare.com"], False),
        ([], False),
    ],
)
def test_uses_transip_nameservers(nameservers, at_transip):
    assert transip.uses_transip_nameservers(nameservers) is at_transip
