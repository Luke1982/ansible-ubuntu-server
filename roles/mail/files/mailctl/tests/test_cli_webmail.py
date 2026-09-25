import re
from dataclasses import replace
from ipaddress import ip_address
from pathlib import Path

import pytest
from conftest import FakeCommand, make_certificate
from test_openlitespeed import HTTPD_CONFIG
from test_webmail import FAKE_CERTBOT, REFUSING_CERTBOT
from test_cli_dns import PUBLIC_IP, FakeDns as HealthyDns

from mailctl.commands import webmail as webmail_command
from mailctl.core import certificate, dns_check, openlitespeed, system, transip, webmail


class FakeDns:
    """webmail.example.nl points to this server (203.0.113.5, see the mailctl fixture); other names don't exist."""

    def addresses(self, name):
        return {ip_address("203.0.113.5")} if name == "webmail.example.nl" else set()


@pytest.fixture
def config(config, tmp_path):
    return replace(config, webmail_root=tmp_path / "www")


@pytest.fixture
def webmail_ready(mailctl, db_config, fake_command, monkeypatch):
    """mailctl with example.nl, whose webmail name points here, and certbot, lswsctrl and a web server that work."""
    fake_command("certbot", FAKE_CERTBOT)
    (db_config.ols_root / "conf").mkdir(parents=True)
    openlitespeed.config_file(db_config.ols_root).write_text(HTTPD_CONFIG)
    (db_config.ols_root / "bin").mkdir()
    FakeCommand(db_config.ols_root / "bin", "lswsctrl", "")
    monkeypatch.setattr(dns_check, "SystemResolver", FakeDns)
    monkeypatch.setattr(certificate, "serves", lambda address, name, file_name, token: certificate.SERVED)
    mailctl.ok("domain", "add", "example.nl")
    return mailctl


class DoctorDns(HealthyDns):
    """The DNS of a domain that is set up, on this server's address, with webmail.example.nl pointing here too."""

    def addresses(self, name):
        here = ("mail.example.nl", "server.hosting.example", "webmail.example.nl")
        return {ip_address("203.0.113.5")} if name in here else set()


def certificate_of(config, name):
    return config.letsencrypt_dir / "live" / name / "fullchain.pem"


def test_webmail_sync_explains_itself_with_an_example(mailctl):
    output = mailctl.ok("webmail", "sync", "--help")

    assert "Usage" in output
    assert "Example" in output


def test_webmail_sync_sets_up_the_site_and_says_what_changed(webmail_ready):
    output = webmail_ready.ok("webmail", "sync")

    assert "https://webmail.example.nl is live, with a new certificate." in output
    assert "Changed the webmail sites." in output

    output = webmail_ready.ok("webmail", "sync")

    assert "https://webmail.example.nl" in output
    assert "new certificate" not in output
    assert "Changed the webmail sites." not in output
    assert "Nothing changed." in output


def test_webmail_sync_says_which_domains_wait_for_their_webmail_name(webmail_ready):
    webmail_ready.ok("domain", "add", "other.nl")

    output = webmail_ready.ok("webmail", "sync")

    assert "No webmail for other.nl yet: webmail.other.nl has no A or AAAA record." in output


def test_webmail_sync_shows_why_a_certificate_was_refused(webmail_ready, fake_command):
    fake_command("certbot", REFUSING_CERTBOT)

    output = webmail_ready.ok("webmail", "sync")

    assert "No certificate for webmail.example.nl: 203.0.113.5: Invalid response from" in output


def test_webmail_sync_says_what_to_do_when_openlitespeed_doesnt_serve_the_site(webmail_ready, monkeypatch):
    monkeypatch.setattr(certificate, "serves", lambda address, name, file_name, token: certificate.NO_ANSWER)
    monkeypatch.setattr(webmail, "SERVE_TIMEOUT", 0)

    output = webmail_ready.ok("webmail", "sync")

    assert "OpenLiteSpeed doesn't serve webmail.example.nl on port 80 at 203.0.113.5." in output
    assert "Run the Ansible playbook" in output


class NoWebmailRecord:
    """DNS as the server's cache has it before the webmail record is published: no such name."""

    def addresses(self, name):
        return set()


@pytest.fixture
def publishing(webmail_ready, fake_transip, transip_key_pair, monkeypatch):
    """webmail_ready on a server at a public address, with example.nl at TransIP without a webmail record, and
    TransIP's nameservers serving what's in the fake zone."""
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address(PUBLIC_IP)})
    monkeypatch.setattr(dns_check, "SystemResolver", NoWebmailRecord)
    monkeypatch.setattr(webmail_command, "PUBLISH_POLL", 0)
    fake_transip.zones["example.nl"] = [{"name": "@", "expire": 300, "type": "A", "content": "198.51.100.80"}]

    def nameserver_addresses(zone_name, name):
        relative = name.removesuffix(f".{zone_name}")
        return [{ip_address(entry["content"]) for entry in fake_transip.zones.get(zone_name, [])
                 if entry["name"] == relative and entry["type"] in ("A", "AAAA")}] * 3

    monkeypatch.setattr(transip, "nameserver_addresses", nameserver_addresses)
    webmail_ready.ok("dns", "credentials", "--login", "mailadmin", "--key-stdin",
                     stdin=transip_key_pair[0].read_text())
    return fake_transip


def test_webmail_sync_publishes_a_missing_record_and_then_gets_the_certificate(webmail_ready, publishing):
    output = webmail_ready.ok("webmail", "sync")

    assert "Published webmail.example.nl at TransIP." in output
    assert "https://webmail.example.nl is live, with a new certificate." in output
    assert publishing.entries() == [("@", 300, "A", "198.51.100.80"), ("webmail", 3600, "A", PUBLIC_IP)]

    assert "Published" not in webmail_ready.ok("webmail", "sync")
    assert publishing.paths("PUT") == ["/domains/example.nl/dns"]


def test_webmail_sync_leaves_a_webmail_record_that_is_there(webmail_ready, publishing):
    publishing.zones["example.nl"].append({"name": "webmail", "expire": 300, "type": "CNAME", "content": "@"})

    output = webmail_ready.ok("webmail", "sync")

    assert "Published" not in output
    assert "No webmail for example.nl yet" in output
    assert publishing.paths("PUT") == []


def test_webmail_sync_says_when_the_nameservers_dont_serve_the_new_record_yet(webmail_ready, publishing, monkeypatch):
    monkeypatch.setattr(transip, "nameserver_addresses", lambda zone_name, name: [set()] * 3)
    monkeypatch.setattr(webmail_command, "PUBLISH_WAIT", 0)

    output = webmail_ready.ok("webmail", "sync")

    assert "Published webmail.example.nl at TransIP." in output
    assert "TransIP's nameservers don't serve webmail.example.nl yet" in output
    assert "No webmail for example.nl yet" in output


def test_webmail_sync_without_dns_or_without_a_transip_login_publishes_nothing(webmail_ready, fake_transip,
                                                                              monkeypatch):
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address(PUBLIC_IP)})
    monkeypatch.setattr(dns_check, "SystemResolver", NoWebmailRecord)

    assert "mailctl has no TransIP login" in webmail_ready.ok("webmail", "sync")
    assert "TransIP" not in webmail_ready.ok("webmail", "sync", "--no-dns")
    assert fake_transip.requests == []


def test_webmail_sync_skips_domains_outside_the_transip_account(webmail_ready, publishing):
    del publishing.zones["example.nl"]

    output = webmail_ready.ok("webmail", "sync")

    assert "Published" not in output
    assert publishing.paths("PUT") == []


def test_the_playbook_leaves_the_webmail_sites_to_a_run_by_hand():
    """A playbook run publishes no DNS records and takes no site away; it says to run the sync itself."""
    task = (Path(__file__).resolve().parents[3] / "tasks" / "configure-webmail.yml").read_text()

    assert not re.search(r"^\s*(shell|command):.*mailctl webmail sync", task, re.MULTILINE)
    assert "mailctl webmail sync" in task


def test_doctor_says_where_a_domains_webmail_is(webmail_ready, db_config, monkeypatch):
    webmail_ready.ok("webmail", "sync")
    make_certificate(certificate_of(db_config, "webmail.example.nl"), "webmail.example.nl")
    monkeypatch.setattr(dns_check, "SystemResolver", DoctorDns)

    assert "Webmail is at https://webmail.example.nl." in webmail_ready.fails("doctor", "example.nl", "--all")


def test_doctor_says_a_domain_has_no_webmail_site_yet(webmail_ready, monkeypatch):
    monkeypatch.setattr(dns_check, "SystemResolver", DoctorDns)

    assert "has no webmail site. Give it one with: mailctl webmail sync" in webmail_ready.fails("doctor",
                                                                                                "example.nl")


def test_doctor_shows_the_records_that_keep_a_site_whose_name_stopped_pointing_here(webmail_ready, monkeypatch):
    webmail_ready.ok("webmail", "sync")
    monkeypatch.setattr(dns_check, "SystemResolver", HealthyDns)  # the same DNS, without the webmail record

    output = webmail_ready.fails("doctor", "example.nl")

    assert "webmail.example.nl has no A or AAAA record." in output
    assert "_caldavs._tcp.example.nl" in output


def test_domain_add_says_how_to_give_the_new_domain_webmail(webmail_ready):
    output = webmail_ready.ok("domain", "add", "other.nl")

    assert "Once webmail.other.nl points to this server, give it webmail with: mailctl webmail sync" in output


def test_domain_delete_removes_the_domains_webmail_site(webmail_ready, db_config):
    webmail_ready.ok("webmail", "sync")

    output = webmail_ready.ok("domain", "delete", "example.nl", "--yes", "--keep-mail")

    assert "Removed its webmail site webmail.example.nl, with that site's certificate." in output
    assert webmail.sites(db_config) == []
    assert not webmail.has_certificate(db_config, "webmail.example.nl")
    assert "Nothing changed." in webmail_ready.ok("webmail", "sync")  # the next sync has nothing left to do


def test_domain_delete_says_nothing_about_webmail_for_a_domain_without_a_site(webmail_ready):
    output = webmail_ready.ok("domain", "delete", "example.nl", "--yes", "--keep-mail")

    assert "webmail site" not in output
