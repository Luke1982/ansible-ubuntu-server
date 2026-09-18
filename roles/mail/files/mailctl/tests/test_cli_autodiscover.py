import re

import pytest
from conftest import FakeCommand, make_certificate
from test_cli_dns import PUBLIC_IP, public_server, transip_account, transip_zone  # noqa: F401 (fixtures)

from mailctl import ui
from mailctl.core import autodiscover, openlitespeed

HTTPD_CONFIG = """\
listener HTTP {
  address                 *:80
  secure                  0
}

listener HTTPS {
  address                 *:443
  secure                  1
}
"""


@pytest.fixture
def ols(db_config):
    """OpenLiteSpeed with listeners for HTTP and HTTPS, the templates Ansible installs, and a fake lswsctrl."""
    conf = db_config.ols_root / "conf"
    (conf / "templates").mkdir(parents=True)
    (conf / "httpd_config.conf").write_text(HTTPD_CONFIG)
    for template in (autodiscover.TEMPLATE, autodiscover.WAITING_TEMPLATE):
        (conf / "templates" / f"{template}.conf").write_text("# installed by Ansible\n")
    (db_config.ols_root / "bin").mkdir()
    return FakeCommand(db_config.ols_root / "bin", "lswsctrl", "")


def members(config):
    lines = openlitespeed.read(config.ols_root)
    return {template: openlitespeed.members(lines, template)
            for template in (autodiscover.TEMPLATE, autodiscover.WAITING_TEMPLATE)}


def certify(config, *names):
    path = config.letsencrypt_dir / "live" / "autodiscover.example.nl" / "fullchain.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    make_certificate(path, *names)


def test_publish_without_a_certificate_serves_http_and_says_how_to_get_one(mailctl, transip_account, ols, db_config):
    output = mailctl.ok("autodiscover", "publish", "example.nl", "--yes")

    assert members(db_config) == {"mailautodiscover": [], "mailautodiscover-http": ["autodiscover.example.nl"]}
    assert ols.calls == [["restart"]]
    assert "goes on HTTP only, until it has a certificate" in output
    for name in ("autoconfig", "autodiscover"):
        assert (name, 3600, "A", PUBLIC_IP) in transip_account.entries()
    assert f"Certbot's web root for autodiscover.example.nl and autoconfig.example.nl is {db_config.autodiscover_root}" \
        in output
    assert (f"certbot certonly --webroot -w {db_config.autodiscover_root} --cert-name autodiscover.example.nl"
            " -d autodiscover.example.nl -d autoconfig.example.nl") in output
    assert "There is no certificate at" in output


def test_publish_with_a_certificate_moves_the_site_to_https(mailctl, transip_account, ols, db_config):
    mailctl.ok("autodiscover", "publish", "example.nl", "--yes")
    certify(db_config, "autodiscover.example.nl", "autoconfig.example.nl")

    output = mailctl.ok("autodiscover", "publish", "example.nl", "--yes")

    assert members(db_config) == {"mailautodiscover": ["autodiscover.example.nl"], "mailautodiscover-http": []}
    assert "goes on HTTP and HTTPS" in output
    assert "Mail programs find the settings at https://autodiscover.example.nl and https://autoconfig.example.nl" \
        in output
    assert ols.calls == [["restart"], ["restart"]]
    assert transip_account.paths("PUT") == ["/domains/example.nl/dns"]

    assert "already set up" in mailctl.ok("autodiscover", "publish", "example.nl", "--yes")
    assert len(ols.calls) == 2


def test_a_certificate_without_both_names_keeps_the_site_on_http(mailctl, transip_account, ols, db_config):
    certify(db_config, "autodiscover.example.nl")

    output = mailctl.ok("autodiscover", "publish", "example.nl", "--yes")

    assert "doesn't include autoconfig.example.nl" in output
    assert members(db_config)["mailautodiscover-http"] == ["autodiscover.example.nl"]


def test_publish_dry_run_changes_nothing(mailctl, transip_account, ols, db_config):
    output = mailctl.ok("autodiscover", "publish", "example.nl", "--dry-run")

    assert "Nothing was changed" in output
    assert (db_config.ols_root / "conf" / "httpd_config.conf").read_text() == HTTPD_CONFIG
    assert ols.calls == []
    assert transip_account.paths("PUT") == []


def test_publish_without_a_terminal_needs_yes_before_changing_anything(mailctl, transip_account, ols, db_config):
    assert "--yes" in mailctl.fails("autodiscover", "publish", "example.nl")
    assert (db_config.ols_root / "conf" / "httpd_config.conf").read_text() == HTTPD_CONFIG


def test_publish_for_a_domain_outside_transip_shows_the_records_and_sets_up_the_site(
    mailctl, transip_account, ols, db_config
):
    del transip_account.zones["example.nl"]

    output = mailctl.ok("autodiscover", "publish", "example.nl", "--yes")

    assert "example.nl isn't in the TransIP account mailadmin" in output
    assert re.search(rf"A\s+autoconfig\.example\.nl\n\s+{re.escape(PUBLIC_IP)}\n", output)
    assert members(db_config)["mailautodiscover-http"] == ["autodiscover.example.nl"]


def test_dns_publish_keeps_the_records_of_the_domains_site(mailctl, transip_account, ols):
    mailctl.ok("autodiscover", "publish", "example.nl", "--yes")

    mailctl.ok("dns", "publish", "example.nl", "--yes")

    for name in ("autoconfig", "autodiscover"):
        assert (name, 3600, "A", PUBLIC_IP) in transip_account.entries()
    assert re.search(r"A\s+autoconfig\.example\.nl\n", mailctl.ok("dns", "show", "example.nl"))


def test_publish_needs_the_templates_from_ansible(mailctl, transip_account, ols, db_config):
    (db_config.ols_root / "conf" / "templates" / "mailautodiscover.conf").unlink()

    output = mailctl.fails("autodiscover", "publish", "example.nl", "--yes")

    assert "The OpenLiteSpeed template" in output
    assert "Run the Ansible playbook" in output


def test_publish_needs_an_http_listener(mailctl, transip_account, ols, db_config):
    (db_config.ols_root / "conf" / "httpd_config.conf").write_text(HTTPD_CONFIG.replace("*:80", "*:8088"))

    assert "no HTTP listener on port 80" in mailctl.fails("autodiscover", "publish", "example.nl", "--yes")


def test_publish_without_dns_leaves_transip_alone(mailctl, public_server, ols, db_config, fake_transip):
    mailctl.ok("domain", "add", "example.nl")

    output = mailctl.ok("autodiscover", "publish", "example.nl", "--no-dns", "--yes")

    assert "Publish these records by hand" in output
    assert re.search(rf"A\s+autodiscover\.example\.nl\n\s+{re.escape(PUBLIC_IP)}\n", output)
    assert fake_transip.requests == []
    assert members(db_config)["mailautodiscover-http"] == ["autodiscover.example.nl"]


def test_a_site_change_needs_confirmation_too(mailctl, public_server, ols, db_config):
    mailctl.ok("domain", "add", "example.nl")

    assert "--yes" in mailctl.fails("autodiscover", "publish", "example.nl", "--no-dns")
    assert (db_config.ols_root / "conf" / "httpd_config.conf").read_text() == HTTPD_CONFIG


def test_a_failed_restart_puts_the_old_config_back_to_try_again(mailctl, public_server, ols, db_config):
    mailctl.ok("domain", "add", "example.nl")
    FakeCommand(db_config.ols_root / "bin", "lswsctrl", "echo 'lswsctrl: not running' >&2; exit 1")

    assert "not running" in mailctl.fails("autodiscover", "publish", "example.nl", "--no-dns", "--yes")
    assert (db_config.ols_root / "conf" / "httpd_config.conf").read_text() == HTTPD_CONFIG

    FakeCommand(db_config.ols_root / "bin", "lswsctrl", "")
    assert "OpenLiteSpeed serves" in mailctl.ok("autodiscover", "publish", "example.nl", "--no-dns", "--yes")


def test_publish_changes_nothing_when_the_config_changed_meanwhile(mailctl, public_server, ols, db_config, monkeypatch):
    mailctl.ok("domain", "add", "example.nl")
    config = db_config.ols_root / "conf" / "httpd_config.conf"
    monkeypatch.setattr(ui, "confirm", lambda question, assume_yes: config.write_text(HTTPD_CONFIG + "\n# edited\n"))

    assert "changed in the meantime" in mailctl.fails("autodiscover", "publish", "example.nl", "--no-dns")
    assert config.read_text() == HTTPD_CONFIG + "\n# edited\n"
