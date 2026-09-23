"""The commands end to end, against a temporary server: nothing here runs as root or reaches the network."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import pytest
from typer.testing import CliRunner

from domainctl import cli, session
from domainctl.core import acl, certificates, dnsnames, layout, serving, sites, users
from domainctl.core.sites import Site
from serverctl import certbot, openlitespeed, system

HERE = {ip_address("81.4.127.10")}


@pytest.fixture
def server(config, tmp_path, monkeypatch):
    """A server domainctl can work on: no root, no restarts, no certbot, no DNS, no useradd."""
    settings = tmp_path / "config.json"
    settings.write_text(json.dumps({key: str(value) for key, value in asdict(config).items()}))
    monkeypatch.setenv("DOMAINCTL_CONFIG", str(settings))
    monkeypatch.setattr(system, "require_root", lambda tool: None)
    monkeypatch.setattr(openlitespeed, "restart", lambda root: None)
    monkeypatch.setattr(system, "server_ips", lambda: HERE)
    monkeypatch.setattr(session.Session, "resolver", property(lambda self: _Resolver()))
    monkeypatch.setattr(acl, "read", lambda path: {})
    monkeypatch.setattr(acl, "apply", lambda path, entries: True)
    monkeypatch.setattr(layout, "_own", lambda path, user: None)
    monkeypatch.setattr(users, "exists", lambda name: False)
    monkeypatch.setattr(users, "create", lambda name, home, shell: home.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(serving, "wait_until_served", lambda docroot, name, addresses, timeout=0: None)
    return config


class _Resolver:
    answers = {"example.nl": HERE, "www.example.nl": HERE}

    def addresses(self, name):
        return set(self.answers.get(name, set()))

    def txt(self, name): return []
    def mx(self, name): return []
    def srv(self, name): return []
    def ptr(self, address): return []


def run(*args):
    result = CliRunner().invoke(cli.app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def certificate(config, user, names, days=60):
    """A certbot-shaped certificate directory, so the commands find one without running certbot."""
    live = config.letsencrypt_dir / "live" / user
    live.mkdir(parents=True, exist_ok=True)
    (live / "fullchain.pem").write_text("certificate")
    (live / "privkey.pem").write_text("key")
    return certbot.Certificate(tuple(names), datetime.now(UTC) + timedelta(days=days))


def test_list_says_so_when_there_are_no_sites(server):
    result = run("list")
    assert result.exit_code == 0
    assert "no sites on this server yet" in result.output


def test_add_sets_a_site_up_and_gets_a_certificate(server, monkeypatch):
    obtained = []
    monkeypatch.setattr(certbot, "obtain",
                        lambda *args, **kwargs: obtained.append((args[2], tuple(args[4]), kwargs.get("dry_run"))))
    result = run("add", "example.nl")
    assert result.exit_code == 0, result.output
    assert sites.list_sites(server) == [Site("example", "example.nl", ("www.example.nl",))]
    assert server.docroot_of("example").is_dir() and server.logs_of("example").is_dir()
    assert [name for _, name, _ in obtained] == [("example.nl", "www.example.nl")] * 2
    assert "https://example.nl is live" in result.output


def test_add_leaves_www_out_when_it_does_not_point_here(server, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {"example.nl": HERE})
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    result = run("add", "example.nl", "--no-dns")
    assert result.exit_code == 0, result.output
    assert sites.find(server, "example").aliases == ()
    assert "www.example.nl has no A or AAAA record" in result.output


def test_add_stops_when_the_domain_points_somewhere_else(server, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {"example.nl": {ip_address("45.87.2.9")}})
    result = run("add", "example.nl", "--no-dns")
    assert result.exit_code == 1
    assert "isn't this server" in result.output
    assert sites.list_sites(server) == []


def test_a_failed_certbot_leaves_the_site_up_over_http(server, monkeypatch):
    monkeypatch.setattr(certbot, "obtain", _fails)
    result = run("add", "example.nl")
    assert result.exit_code == 0, result.output
    assert sites.find(server, "example") is not None
    assert "The site is up over HTTP" in result.output


def test_add_can_be_run_again_to_finish_what_failed(server, monkeypatch):
    monkeypatch.setattr(certbot, "obtain", _fails)
    run("add", "example.nl")
    obtained = []
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: obtained.append(args[2]))
    result = run("add", "example.nl")
    assert result.exit_code == 0, result.output
    assert obtained == ["example", "example"]


def test_add_does_nothing_when_the_certificate_already_covers_everything(server, monkeypatch):
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    run("add", "example.nl")
    found = certificate(server, "example", ("example.nl", "www.example.nl"))
    monkeypatch.setattr(certbot, "read", lambda path: found)
    monkeypatch.setattr(certbot, "obtain", _fails)
    result = run("add", "example.nl")
    assert result.exit_code == 0, result.output
    assert "already covers" in result.output


def test_add_refuses_a_domain_another_site_already_serves(server, monkeypatch):
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    run("add", "example.nl")
    result = run("add", "example.nl", "--user", "second")
    assert result.exit_code == 1
    assert "already served by the site example" in result.output


def test_list_shows_the_domain_and_the_certificate(server, monkeypatch):
    sites.save(server, Site("example", "example.nl", ("www.example.nl",)))
    found = certificate(server, "example", ("example.nl", "www.example.nl"))
    monkeypatch.setattr(certbot, "read", lambda path: found)
    result = run("list")
    assert result.exit_code == 0, result.output
    assert "example.nl" in result.output and "until" in result.output


def test_list_says_a_site_has_no_certificate_yet(server):
    sites.save(server, Site("example", "example.nl"))
    assert "none" in run("list").output


def test_check_reports_a_site_without_a_certificate(server):
    sites.save(server, Site("example", "example.nl"))
    result = run("check")
    assert result.exit_code == 0, result.output
    assert "points to this server" in result.output
    assert "There is no certificate" in result.output


def test_check_reports_an_expired_certificate(server, monkeypatch):
    sites.save(server, Site("example", "example.nl"))
    found = certificate(server, "example", ("example.nl",), days=-1)
    monkeypatch.setattr(certbot, "read", lambda path: found)
    result = run("check")
    assert "expired" in result.output and "certbot renew" in result.output


def test_check_reports_a_name_that_stopped_pointing_here(server, monkeypatch):
    sites.save(server, Site("example", "example.nl"))
    monkeypatch.setattr(_Resolver, "answers", {"example.nl": {ip_address("45.87.2.9")}})
    assert "isn't this server" in run("check").output


def test_sync_adds_the_template_block_and_reports_the_change(server):
    result = run("sync")
    assert result.exit_code == 0, result.output
    assert "Changed the web sites" in result.output
    assert run("sync").output.strip().endswith("Nothing changed.")


def test_sync_switches_a_redirect_that_was_left_off_back_on(server):
    from domainctl.core import template
    template.set_redirect(server, False)
    result = run("sync")
    assert "it is on again" in result.output
    assert template.redirect_is_on(template.read(server))


def test_sync_still_works_on_a_server_whose_listeners_are_not_set_up(server):
    path = server.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("*:80", "*:8080"))
    result = run("sync")
    assert result.exit_code == 0, result.output
    assert "no HTTP listener" in result.output


def test_delete_removes_the_site_but_keeps_the_files(server, monkeypatch):
    sites.save(server, Site("example", "example.nl"))
    server.docroot_of("example").mkdir(parents=True)
    monkeypatch.setattr(certificates, "delete", lambda config, user: True)
    result = run("delete", "example", "--yes")
    assert result.exit_code == 0, result.output
    assert sites.list_sites(server) == []
    assert server.docroot_of("example").is_dir()
    assert "still there" in result.output


def test_delete_says_which_sites_there_are_when_the_name_is_wrong(server):
    sites.save(server, Site("example", "example.nl"))
    result = run("delete", "typo", "--yes")
    assert result.exit_code == 1
    assert "no site typo" in result.output and "example" in result.output


def test_publishing_a_missing_record_is_offered_and_then_waited_for(server, monkeypatch):
    """The whole path: no record, publish at TransIP, wait for the nameservers, then certbot."""
    monkeypatch.setattr(_Resolver, "answers", {"example.nl": HERE})
    published = []

    def publish(client, name, ips):
        published.append(name)
        _Resolver.answers = {**_Resolver.answers, name: HERE}
        return "example.nl", ()

    monkeypatch.setattr(dnsnames, "publish", publish)
    monkeypatch.setattr("domainctl.commands.dns.client", lambda session, read_only: object())
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    result = run("add", "example.nl", "--yes")
    assert result.exit_code == 0, result.output
    assert published == ["www.example.nl"]
    assert sites.find(server, "example").aliases == ("www.example.nl",)


def _fails(*args, **kwargs):
    from serverctl.errors import CtlError
    raise CtlError("Let's Encrypt refused it.")
