from ipaddress import ip_address

import pytest
from conftest import FakeCommand, make_certificate
from test_cli_autodiscover import HTTPD_CONFIG
from test_cli_dns import PUBLIC_IP, FakeDns, public_server  # noqa: F401 (fixtures)

from mailctl.core import dns_check, mailcert, openlitespeed


@pytest.fixture
def ols(db_config):
    """OpenLiteSpeed with its listeners, the template Ansible installs, and a fake lswsctrl."""
    conf = db_config.ols_root / "conf"
    (conf / "templates").mkdir(parents=True)
    (conf / "httpd_config.conf").write_text(HTTPD_CONFIG)
    (conf / "templates" / f"{mailcert.TEMPLATE}.conf").write_text("# installed by Ansible\n")
    (db_config.ols_root / "bin").mkdir()
    return FakeCommand(db_config.ols_root / "bin", "lswsctrl", "")


@pytest.fixture
def server(mailctl, public_server, ols, db_config, monkeypatch, fake_command):
    """example.nl on this server, with mail.example.nl pointing here and a certificate for the hostname alone."""
    mailctl.ok("domain", "add", "example.nl")
    monkeypatch.setattr(dns_check, "SystemResolver", lambda: FakeDns())
    make_certificate(db_config.certificate(), "server.hosting.example")
    fake_command("postfix")
    return mailctl


def members(config):
    return openlitespeed.members(openlitespeed.read(config.ols_root), mailcert.TEMPLATE)


def test_sync_answers_lets_encrypt_for_the_names_and_asks_for_the_certificate(server, db_config, fake_command, ols):
    certbot = fake_command("certbot")
    doveadm = fake_command("doveadm")

    output = server.ok("certificate", "sync", "--yes")

    assert members(db_config) == ["server.hosting.example", "mail.example.nl"]
    assert ols.calls == [["restart"]]
    assert certbot.calls[0][:7] == ["certonly", "--webroot", "--webroot-path", str(db_config.mailcert_root),
                                    "--cert-name", "server.hosting.example", "--expand"]
    assert certbot.calls[0][7:11] == ["-d", "server.hosting.example", "-d", "mail.example.nl"]
    assert doveadm.calls == [["reload"]]
    assert "The certificate doesn't include mail.example.nl." in output
    assert "Postfix and Dovecot serve a certificate for 2 names." in output


def test_sync_leaves_the_certificate_alone_when_it_has_every_name(server, db_config, fake_command, ols):
    certbot = fake_command("certbot")
    make_certificate(db_config.certificate(), "server.hosting.example", "mail.example.nl")
    server.ok("certificate", "sync", "--yes")

    output = server.ok("certificate", "sync", "--yes")

    assert "The certificate has 2 names: server.hosting.example, mail.example.nl." in output
    assert certbot.calls == [] and ols.calls == [["restart"]]  # only the first run changed the sites


def test_sync_says_which_domains_are_left_out(server, db_config, fake_command):
    fake_command("certbot")
    server.ok("domain", "add", "other.nl")

    output = server.ok("certificate", "sync", "--yes")

    assert "Left out of the certificate: mail.other.nl has no A or AAAA record yet." in output
    assert members(db_config) == ["server.hosting.example", "mail.example.nl"]


def test_a_dry_run_changes_nothing(server, db_config, fake_command, ols):
    certbot = fake_command("certbot")

    output = server.ok("certificate", "sync", "--dry-run")

    assert "Nothing was changed (--dry-run)." in output
    assert members(db_config) == [] and certbot.calls == [] and ols.calls == []


def test_sync_says_what_lets_encrypt_refused(server, fake_command):
    fake_command("certbot", "echo '  Detail: 203.0.113.5: Invalid response from http://mail.example.nl' >&2; exit 1")

    output = server.fails("certificate", "sync", "--yes")

    assert "No certificate for the mail names" in output and "Invalid response" in output


def test_sync_warns_about_a_name_another_site_answers_for(server, db_config, fake_command):
    fake_command("certbot")
    lines = openlitespeed.read(db_config.ols_root)
    openlitespeed.write(db_config.ols_root, lines + ["virtualhost mail.example.nl {", "  vhRoot /var/www/", "}", ""])

    output = server.ok("certificate", "sync", "--yes")

    assert "OpenLiteSpeed has a site of its own for mail.example.nl" in output


def test_sync_needs_the_template_ansible_installs(server, db_config, fake_command):
    fake_command("certbot")
    (db_config.ols_root / "conf" / "templates" / f"{mailcert.TEMPLATE}.conf").unlink()

    output = server.fails("certificate", "sync", "--yes")

    assert "is missing" in output and "Run the Ansible playbook" in output


def test_sync_waits_for_a_listener_instead_of_stopping_a_playbook_run(server, db_config, fake_command):
    certbot = fake_command("certbot")
    # OpenLiteSpeed as it comes: one listener on 8088, none on 80, so Let's Encrypt can't check a name here.
    openlitespeed.config_file(db_config.ols_root).write_text(
        "listener Default{\n  address                 *:8088\n  secure                  0\n}\n"
    )

    output = server.ok("certificate", "sync", "--yes")

    assert "OpenLiteSpeed has no listener on port 80 yet" in output
    assert certbot.calls == [] and members(db_config) == []
