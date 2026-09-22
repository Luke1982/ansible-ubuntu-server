from dataclasses import replace
from datetime import datetime, timedelta
from ipaddress import ip_address

import pytest
from conftest import make_certificate
from test_openlitespeed import HTTPD_CONFIG

from mailctl.core import certificate, mailcert, openlitespeed
from mailctl.core.dns_check import LookupFailed
from mailctl.core.errors import MailctlError

NOW = datetime.now().astimezone()
SERVER_IPS = {ip_address("203.0.113.5")}


class FakeDns:
    """mail.example.nl points here, mail.other.nl elsewhere, mail.new.nl nowhere, and mail.slow.nl times out."""

    def addresses(self, name):
        if name == "mail.slow.nl":
            raise LookupFailed(f"The A lookup for {name} timed out.")
        return {"mail.example.nl": SERVER_IPS, "mail.other.nl": {ip_address("198.51.100.9")}}.get(name, set())


@pytest.fixture
def server(config):
    """OpenLiteSpeed with the template Ansible installs, and a certificate for the hostname alone."""
    (config.ols_root / "conf" / "templates").mkdir(parents=True)
    openlitespeed.config_file(config.ols_root).write_text(HTTPD_CONFIG)
    (config.ols_root / "conf" / "templates" / "mailnames.conf").write_text("# installed by Ansible\n")
    make_certificate(config.certificate(), config.hostname)
    return config


def plan(config, *domains):
    return mailcert.plan(config, list(domains), SERVER_IPS, FakeDns(), NOW)


def test_the_certificate_is_for_the_hostname_and_the_mail_hosts_that_point_here(server):
    made = plan(server, "example.nl", "other.nl", "new.nl", "slow.nl")

    assert made.names == ["server.hosting.example", "mail.example.nl"]
    assert made.left_out == [
        "mail.new.nl has no A or AAAA record yet.",
        "mail.other.nl points to 198.51.100.9, not to this server.",
        "mail.slow.nl: The A lookup for mail.slow.nl timed out.",
    ]
    assert made.reason == "The certificate doesn't include mail.example.nl."


def test_a_certificate_with_every_name_needs_nothing(server):
    make_certificate(server.certificate(), server.hostname, "mail.example.nl")

    assert plan(server, "example.nl").reason == ""


def test_a_certificate_about_to_expire_is_asked_for_again(server):
    make_certificate(server.certificate(), server.hostname, "mail.example.nl",
                     days=int(mailcert.RENEW_BEFORE / timedelta(days=1)) - 1)

    assert "expires on" in plan(server, "example.nl").reason


def test_a_missing_certificate_is_a_reason_of_its_own(server):
    server.certificate().unlink()

    assert "There is no certificate at" in plan(server, "example.nl").reason


def test_the_names_become_members_that_answer_lets_encrypt(server):
    names = plan(server, "example.nl").names

    before, after = mailcert.planned_config(server, names)

    assert openlitespeed.members(before, mailcert.TEMPLATE) == []
    assert openlitespeed.members(after, mailcert.TEMPLATE) == names
    template = next(block for block in openlitespeed.blocks(after) if block.name == mailcert.TEMPLATE)
    assert openlitespeed.value(after, template, "listeners") == "HTTP"  # Let's Encrypt checks over HTTP


def test_a_name_that_is_gone_loses_its_member(server):
    _, with_both = mailcert.planned_config(server, ["server.hosting.example", "mail.example.nl"])
    openlitespeed.write(server.ols_root, with_both)

    _, after = mailcert.planned_config(server, ["server.hosting.example"])

    assert openlitespeed.members(after, mailcert.TEMPLATE) == ["server.hosting.example"]


def test_the_template_has_to_be_installed(config):
    (config.ols_root / "conf").mkdir(parents=True)
    openlitespeed.config_file(config.ols_root).write_text(HTTPD_CONFIG)

    with pytest.raises(MailctlError, match="template .* is missing"):
        mailcert.planned_config(config, ["server.hosting.example"])


def test_names_another_site_answers_for_are_named(server):
    # test_openlitespeed's config has a virtual host "shop" mapped to shop.example.nl.
    assert mailcert.served_elsewhere(server, ["mail.example.nl", "shop.example.nl", "shop"]) == \
        ["shop.example.nl", "shop"]


def test_request_asks_certbot_for_every_name_under_the_hostname(server, fake_command):
    certbot = fake_command("certbot")

    mailcert.request(server, ["server.hosting.example", "mail.example.nl"])

    assert certbot.calls[0][:8] == [
        "certonly", "--webroot", "--webroot-path", str(server.mailcert_root),
        "--cert-name", "server.hosting.example", "--expand", "-d",
    ]
    assert certbot.calls[0][8:11] == ["server.hosting.example", "-d", "mail.example.nl"]


def test_request_says_what_lets_encrypt_refused(server, fake_command):
    fake_command("certbot", "echo '  Detail: 203.0.113.5: Invalid response from http://mail.example.nl' >&2; exit 1")

    with pytest.raises(MailctlError, match="No certificate for the mail names: .*Invalid response"):
        mailcert.request(server, ["mail.example.nl"])


def test_reload_makes_postfix_and_dovecot_read_the_certificate(fake_command):
    doveadm, postfix = fake_command("doveadm"), fake_command("postfix")

    mailcert.reload_mail_services()

    assert doveadm.calls == [["reload"]] and postfix.calls == [["reload"]]


def test_certbot_runs_with_the_account_and_directory_of_this_server(config, fake_command):
    certbot = fake_command("certbot")

    certificate.run_certbot(config, "certonly")

    assert certbot.calls == [["certonly", "--agree-tos", "--register-unsafely-without-email", "--non-interactive",
                              "--config-dir", str(config.letsencrypt_dir)]]


def test_certbot_registers_the_address_this_server_was_set_up_with(config, fake_command):
    certbot = fake_command("certbot")

    certificate.run_certbot(replace(config, letsencrypt_email="admin@example.nl"), "renew")

    assert certbot.calls[0][:4] == ["renew", "--agree-tos", "--email", "admin@example.nl"]
