import json
import re
from ipaddress import ip_address

import pytest
from conftest import FAKE_KEY_RECORD_START, make_certificate

from mailctl import ui
from mailctl.core import dns_check, system
from mailctl.core.dns_check import Srv

PUBLIC_IP = "93.184.216.34"
TRANSIP_DEFAULTS = [
    {"name": "@", "expire": 300, "type": "A", "content": "198.51.100.80"},
    {"name": "@", "expire": 86400, "type": "MX", "content": "10 mx.transip.email."},
    {"name": "@", "expire": 300, "type": "TXT", "content": "v=spf1 include:_spf.transip.email ~all"},
    {"name": "mail", "expire": 300, "type": "CNAME", "content": "@"},
    {"name": "_dmarc", "expire": 300, "type": "TXT", "content": "v=DMARC1; p=none"},
]


@pytest.fixture
def public_server(monkeypatch):
    """This server, at a public address."""
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address(PUBLIC_IP)})


@pytest.fixture
def terminal(monkeypatch):
    """Makes mailctl ask for missing values, as it does in a terminal."""
    monkeypatch.setattr(ui, "interactive", lambda: True)


@pytest.fixture
def pasted_key(transip_key_pair, monkeypatch):
    """The private key, pasted when mailctl asks for it."""
    monkeypatch.setattr(ui, "ask_secret_lines", lambda prompt, last_line: transip_key_pair[0].read_text())


@pytest.fixture
def transip_zone(mailctl, public_server, fake_transip):
    """TransIP with TransIP's default records for example.nl, which is on this server."""
    mailctl.ok("domain", "add", "example.nl")
    fake_transip.zones["example.nl"] = [dict(entry) for entry in TRANSIP_DEFAULTS]
    return fake_transip


@pytest.fixture
def transip_account(mailctl, transip_zone, transip_key_pair):
    """transip_zone, with mailctl's TransIP login and key entered."""
    mailctl.ok("dns", "credentials", "--login", "mailadmin", "--key-stdin", stdin=transip_key_pair[0].read_text())
    return transip_zone


class FakeDns:
    """DNS as it is once everything is set up for example.nl on server.hosting.example."""

    def __init__(self, dkim_value=None):
        self.dkim_value = dkim_value

    def txt(self, name):
        records = {"example.nl": ["v=spf1 mx ~all"], "_dmarc.example.nl": ["v=DMARC1; p=reject"]}
        if self.dkim_value:
            records["mail._domainkey.example.nl"] = [self.dkim_value]
        return records.get(name, [])

    def mx(self, name):
        return ["mail.example.nl"] if name == "example.nl" else []

    def addresses(self, name):
        known = name in ("mail.example.nl", "server.hosting.example")
        return {ip_address(PUBLIC_IP)} if known else set()

    def srv(self, name):
        ports = {"_imaps": 993, "_imap": 143, "_submissions": 465, "_submission": 587}
        service, _, domain = name.partition("._tcp.")
        return [Srv(0, 1, ports[service], "mail.example.nl")] if domain == "example.nl" else []

    def ptr(self, address):
        return ["server.hosting.example"]


@pytest.fixture
def healthy_dns(mailctl, public_server, monkeypatch, db_config, tmp_path):
    """example.nl on this server, with its DNS and the certificate set up right."""
    mailctl.ok("domain", "add", "example.nl")
    dkim_value = re.search(r"v=DKIM1; [^\n]+", mailctl.ok("dkim", "show", "example.nl")).group()
    monkeypatch.setattr(dns_check, "SystemResolver", lambda: FakeDns(dkim_value))
    make_certificate(db_config.certificate(), "server.hosting.example", "mail.example.nl")


def test_dns_show_prints_the_records_with_the_srv_records(mailctl, public_server):
    mailctl.ok("domain", "add", "example.nl")

    output = mailctl.ok("dns", "show", "example.nl")

    assert re.search(r"MX\s+example\.nl\n\s+10 mail\.example\.nl\n", output)
    assert re.search(rf"A\s+mail\.example\.nl\n\s+{re.escape(PUBLIC_IP)}\n", output)
    assert re.search(r"SRV\s+_imaps\._tcp\.example\.nl\n\s+0 1 993 mail\.example\.nl\n", output)
    assert re.search(r"SRV\s+_submission\._tcp\.example\.nl\n\s+10 1 587 mail\.example\.nl\n", output)
    assert FAKE_KEY_RECORD_START in output


def test_dns_show_refuses_a_domain_that_isnt_on_this_server(mailctl):
    assert "other.nl isn't a domain on this server" in mailctl.fails("dns", "show", "other.nl")


def test_dns_publish_without_credentials_or_a_terminal_says_how_to_enter_them(mailctl, transip_zone):
    output = mailctl.fails("dns", "publish", "example.nl", "--yes")

    assert "mailctl has no TransIP login and key yet" in output
    assert "mailctl dns credentials --login LOGIN --key-stdin < transip.key" in output


def test_dns_publish_asks_for_the_credentials_the_first_time(mailctl, transip_zone, terminal, pasted_key):
    output = mailctl.ok("dns", "publish", "example.nl", stdin="mailadmin\ny\n")

    assert "mailctl has no TransIP login and key yet" in output
    assert f"put this server's addresses on its whitelist: {PUBLIC_IP}" in output
    assert "TransIP accepts the key. mailctl logs in as mailadmin from now on." in output
    assert "Published the mail records of example.nl at TransIP" in output
    assert "has no TransIP login" not in mailctl.ok("dns", "publish", "example.nl", "--dry-run")


def test_dns_credentials_in_a_terminal_replaces_them(mailctl, transip_account, terminal, pasted_key, db_config):
    output = mailctl.ok("dns", "credentials", stdin="other-account\n")

    assert "mailctl logs in as other-account from now on" in output
    assert json.loads(db_config.transip_settings.read_text())["login"] == "other-account"


def test_dns_credentials_notes_a_key_that_works_from_anywhere(mailctl, transip_zone, transip_key_pair):
    transip_zone.whitelisted = False

    output = mailctl.ok("dns", "credentials", "--login", "mailadmin", "--key-stdin", stdin=transip_key_pair[0].read_text())

    assert "isn't limited to the addresses on the whitelist" in output


def test_dns_credentials_without_a_terminal_need_the_key_on_standard_input(mailctl):
    assert "Pipe it in with --key-stdin" in mailctl.fails("dns", "credentials", "--login", "mailadmin")


def test_dns_credentials_transip_refuses_are_explained(mailctl, transip_zone, transip_key_pair):
    transip_zone.whitelisted = False
    transip_zone.answers[("POST", "/auth")] = [(403, {"error": "Remote IP 203.0.113.5 is not authorized"})] * 2

    output = mailctl.fails("dns", "credentials", "--login", "mailadmin", "--key-stdin",
                           stdin=transip_key_pair[0].read_text())

    assert "TransIP refused to log in as mailadmin: Remote IP 203.0.113.5 is not authorized" in output
    assert "whitelist" in output


def test_dns_publish_dry_run_shows_the_changes_and_changes_nothing(mailctl, transip_account):
    output = mailctl.ok("dns", "publish", "example.nl", "--dry-run")

    assert re.search(r"- MX example\.nl\n\s+10 mx\.transip\.email\.\n", output)
    assert re.search(r"- CNAME mail\.example\.nl\n\s+@\n", output)
    assert re.search(r"\+ MX example\.nl\n\s+10 mail\.example\.nl\.\n", output)
    assert re.search(r"\+ TXT example\.nl\n\s+v=spf1 ip4:93\.184\.216\.34 include:_spf\.transip\.email ~all\n", output)
    assert re.search(r"\+ SRV _imaps\._tcp\.example\.nl\n\s+0 1 993 mail\.example\.nl\.\n", output)
    assert "1 record is already right" in output  # the DMARC policy
    assert "Nothing was changed" in output
    assert transip_account.entries() == [tuple(entry.values()) for entry in TRANSIP_DEFAULTS]
    assert all(body["read_only"] for _, path, body, _ in transip_account.requests if path == "/auth")
    assert "PUT" not in (method for method, *_ in transip_account.requests)


def test_dns_publish_without_a_terminal_needs_yes(mailctl, transip_account):
    assert "--yes" in mailctl.fails("dns", "publish", "example.nl")
    assert transip_account.entries() == [tuple(entry.values()) for entry in TRANSIP_DEFAULTS]


def test_dns_publish_asks_before_changing_anything(mailctl, transip_account, monkeypatch):
    monkeypatch.setattr(ui, "interactive", lambda: True)

    output = mailctl.fails("dns", "publish", "example.nl", stdin="n\n")

    assert "Cancelled" in output
    assert transip_account.entries() == [tuple(entry.values()) for entry in TRANSIP_DEFAULTS]


def test_dns_publish_replaces_the_mail_records_and_keeps_the_rest(mailctl, transip_account, db_config):
    make_certificate(db_config.certificate(), "server.hosting.example")

    output = mailctl.ok("dns", "publish", "example.nl", "--yes")

    assert "Published the mail records of example.nl at TransIP" in output
    assert "Other servers may still use the removed records for up to 1 day" in output
    assert "The certificate doesn't include mail.example.nl yet" in output
    assert "mailctl doctor example.nl" in output
    entries = transip_account.entries()
    assert ("@", 300, "A", "198.51.100.80") in entries
    assert ("_dmarc", 300, "TXT", "v=DMARC1; p=none") in entries
    for gone in [("@", 86400, "MX", "10 mx.transip.email."), ("mail", 300, "CNAME", "@")]:
        assert gone not in entries
    for added in [("@", 3600, "MX", "10 mail.example.nl."), ("mail", 3600, "A", PUBLIC_IP),
                  ("_submissions._tcp", 3600, "SRV", "0 1 465 mail.example.nl.")]:
        assert added in entries
    assert any(name == "mail._domainkey" and content.startswith(FAKE_KEY_RECORD_START)
               for name, _, _, content in entries)
    logins = [body["read_only"] for _, path, body, _ in transip_account.requests if path == "/auth"]
    assert logins == [True, False]  # checking the entered key, then publishing

    assert "already right" in mailctl.ok("dns", "publish", "example.nl", "--yes")
    assert transip_account.paths("PUT") == ["/domains/example.nl/dns"]


def test_dns_publish_changes_nothing_when_the_zone_changed_while_asking(mailctl, transip_account, monkeypatch):
    def someone_edits_the_zone(question, assume_yes):
        transip_account.zones["example.nl"].append({"name": "shop", "expire": 300, "type": "A", "content": "192.0.2.7"})

    monkeypatch.setattr(ui, "confirm", someone_edits_the_zone)

    output = mailctl.fails("dns", "publish", "example.nl")

    assert "changed in the meantime" in output
    assert transip_account.paths("PUT") == []


def test_dns_publish_puts_a_subdomain_in_its_parents_zone(mailctl, transip_account):
    mailctl.ok("domain", "add", "shop.example.nl")

    output = mailctl.ok("dns", "publish", "shop.example.nl", "--yes")

    assert re.search(r"\+ A mail\.shop\.example\.nl\n", output)
    assert ("mail.shop", 3600, "A", PUBLIC_IP) in transip_account.entries()
    assert ("shop", 3600, "MX", "10 mail.shop.example.nl.") in transip_account.entries()
    assert ("@", 86400, "MX", "10 mx.transip.email.") in transip_account.entries()
    assert "/domains/shop.example.nl/dns" in transip_account.paths("GET")


def test_dns_publish_warns_when_the_domain_uses_other_nameservers(mailctl, transip_account):
    transip_account.nameservers["example.nl"] = ["ns1.cloudflare.com", "ns2.cloudflare.com"]

    output = mailctl.ok("dns", "publish", "example.nl", "--dry-run")

    assert "example.nl uses the nameservers ns1.cloudflare.com, ns2.cloudflare.com" in output


def test_dns_publish_explains_a_domain_outside_the_transip_account(mailctl, transip_account):
    del transip_account.zones["example.nl"]

    output = mailctl.fails("dns", "publish", "example.nl", "--yes")

    assert "example.nl isn't in the TransIP account mailadmin" in output
    assert "mailctl dns show example.nl" in output


def test_dns_publish_needs_a_public_address(mailctl, transip_account, monkeypatch):
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address("10.0.0.5")})

    assert "no public IP address to publish for mail.example.nl" in mailctl.fails("dns", "publish", "example.nl", "--yes")


def test_dns_publish_without_a_dkim_key_publishes_the_rest(mailctl, transip_account, db_config):
    mailctl.ok("dkim", "show", "example.nl")
    for key_file in db_config.dkim_keys.glob("*/mail.private"):
        key_file.unlink()

    output = mailctl.ok("dns", "publish", "example.nl", "--yes")

    assert "example.nl has no DKIM key" in output
    assert "mailctl dkim create example.nl" in output
    assert ("@", 3600, "MX", "10 mail.example.nl.") in transip_account.entries()


def test_domain_add_mentions_publishing_at_transip_the_certificate_and_the_doctor(mailctl, transip_account, db_config):
    make_certificate(db_config.certificate(), "server.hosting.example")

    output = mailctl.ok("domain", "add", "other.nl")

    assert "Publish them at TransIP with: mailctl dns publish other.nl" in output
    assert "The certificate doesn't include mail.other.nl yet" in output
    assert "Check the domain's setup with: mailctl doctor other.nl" in output


def test_doctor_passes_a_server_and_domain_that_are_set_up(mailctl, healthy_dns):
    output = mailctl.ok("doctor")

    assert "Server server.hosting.example" in output
    assert "server.hosting.example points to this server" in output
    assert "The certificate includes mail.example.nl" in output
    assert "Mail programs can look up mail.example.nl for IMAP and sending" in output
    assert "Everything is set up" in output


def test_doctor_ends_with_an_error_when_there_is_a_problem(mailctl, healthy_dns, db_config):
    make_certificate(db_config.certificate(), "server.hosting.example")

    output = mailctl.fails("doctor", "example.nl")

    assert "The certificate doesn't include mail.example.nl" in output
    assert "Found 1 problem." in output


def test_doctor_counts_warnings(mailctl, healthy_dns, monkeypatch):
    class WithoutSrv(FakeDns):
        def srv(self, name):
            return []

    monkeypatch.setattr(dns_check, "SystemResolver", lambda: WithoutSrv(None))

    output = mailctl.fails("doctor")

    assert "There are no SRV records" in output
    assert re.search(r"SRV\s+_imaps\._tcp\.example\.nl\n\s+0 1 993 mail\.example\.nl\n", output)
    assert "Found 1 problem and 1 warning." in output  # without a DKIM record


def test_doctor_explains_a_missing_certificate(mailctl, public_server, monkeypatch):
    monkeypatch.setattr(dns_check, "SystemResolver", FakeDns)

    output = mailctl.fails("doctor")

    assert "There is no certificate at" in output
    assert "There are no domains yet." in output


def test_doctor_refuses_a_domain_that_isnt_on_this_server(mailctl):
    assert "other.nl isn't a domain on this server" in mailctl.fails("doctor", "other.nl")


def test_status_of_a_domain_checks_the_certificate(mailctl, healthy_dns):
    output = mailctl.ok("status", "example.nl")

    assert "The certificate includes mail.example.nl" in output
    assert "Mail programs can look up mail.example.nl" in output
