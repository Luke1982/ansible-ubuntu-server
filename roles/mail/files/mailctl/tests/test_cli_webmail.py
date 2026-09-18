import re
from dataclasses import replace
from ipaddress import ip_address
from pathlib import Path

import pytest
from conftest import FakeCommand
from test_openlitespeed import HTTPD_CONFIG
from test_webmail import FAKE_CERTBOT, REFUSING_CERTBOT

from mailctl.core import dns_check, openlitespeed, webmail


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
    monkeypatch.setattr(webmail, "_serves", lambda address, name, file_name, token: True)
    mailctl.ok("domain", "add", "example.nl")
    return mailctl


def test_webmail_sync_explains_itself_with_an_example(mailctl):
    output = mailctl.ok("webmail", "sync", "--help")

    assert "Usage" in output
    assert "Example" in output


def ansible_change_message() -> str:
    """The output that the Ansible task running 'mailctl webmail sync' counts as a change."""
    task = Path(__file__).resolve().parents[3] / "tasks" / "configure-webmail.yml"
    return re.search(r"'([^']+)' in webmail_sync\.stdout", task.read_text()).group(1)


def test_webmail_sync_sets_up_the_site_and_says_what_changed(webmail_ready):
    output = webmail_ready.ok("webmail", "sync")

    assert "https://webmail.example.nl is live, with a new certificate." in output
    assert ansible_change_message() in output

    output = webmail_ready.ok("webmail", "sync")

    assert "https://webmail.example.nl" in output
    assert "new certificate" not in output
    assert ansible_change_message() not in output
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
    monkeypatch.setattr(webmail, "_serves", lambda address, name, file_name, token: False)
    monkeypatch.setattr(webmail, "SERVE_TIMEOUT", 0)

    output = webmail_ready.ok("webmail", "sync")

    assert "OpenLiteSpeed doesn't serve webmail.example.nl on port 80 at 203.0.113.5." in output
    assert "Run the Ansible playbook" in output


def test_webmail_sync_removes_the_site_of_a_deleted_domain(webmail_ready):
    webmail_ready.ok("webmail", "sync")
    webmail_ready.ok("domain", "delete", "example.nl", "--yes", "--keep-mail")

    output = webmail_ready.ok("webmail", "sync")

    assert "Removed https://webmail.example.nl: Its domain is no longer on this server." in output
