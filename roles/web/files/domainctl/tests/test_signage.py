"""domainctl signage end to end, against a temporary server: nothing here runs as root, starts a service or reaches
the network."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import pytest
from typer.testing import CliRunner

from domainctl import cli, session
from domainctl.core import serving, signage, sites, users
from domainctl.core.signage import Signage
from domainctl.core.sites import Site
from serverctl import certbot, openlitespeed, system

HERE = {ip_address("81.4.127.10")}
ELSEWHERE = {ip_address("178.21.114.195")}


class _Resolver:
    answers = {"andre.milocast.nl": HERE, "bert.milocast.nl": HERE, "example.nl": HERE}

    def addresses(self, name):
        return set(self.answers.get(name, set()))

    def txt(self, name): return []
    def mx(self, name): return []
    def srv(self, name): return []
    def ptr(self, address): return []


@pytest.fixture
def started():
    """The servers started, each with whether it was restarted."""
    return []


@pytest.fixture
def server(config, tmp_path, monkeypatch, started):
    """A server with piSignage installed, where nothing is started and every port is free."""
    config = replace(config, signage_settings=tmp_path / "pisignage")
    settings = tmp_path / "config.json"
    settings.write_text(json.dumps({key: str(value) for key, value in asdict(config).items()}))
    monkeypatch.setenv("DOMAINCTL_CONFIG", str(settings))
    unit = tmp_path / "pisignage@.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(signage, "UNIT_FILE", unit)
    monkeypatch.setattr(system, "require_root", lambda tool: None)
    monkeypatch.setattr(system, "server_ips", lambda: HERE)
    monkeypatch.setattr(session.Session, "resolver", property(lambda self: _Resolver()))
    monkeypatch.setattr(openlitespeed, "restart", lambda root: None)
    made = set()
    monkeypatch.setattr(users, "exists", lambda name: name in made)
    monkeypatch.setattr(users, "create", lambda name, home, shell: (made.add(name), home.mkdir(parents=True)))
    monkeypatch.setattr(users, "delete", lambda name, remove_home: made.discard(name))
    monkeypatch.setattr(signage, "install",
                        lambda config, user: signage.code_dir(config, user).mkdir(exist_ok=True) or [])
    monkeypatch.setattr(signage, "listening", lambda port: False)
    monkeypatch.setattr(signage, "wait_until_answering", lambda port: True)
    monkeypatch.setattr(signage, "running", lambda user: True)
    monkeypatch.setattr(signage, "stop", lambda user: None)
    monkeypatch.setattr(signage, "drop_database", lambda user: config.signage_settings.joinpath(f"{user}.dropped").touch())
    monkeypatch.setattr(serving, "wait_until_served", lambda docroot, name, addresses, timeout=0: [])
    monkeypatch.setattr(certbot, "obtain", _certify)
    monkeypatch.setattr(certbot, "delete", lambda letsencrypt_dir, cert_name: system.remove_tree(
        certbot.live(letsencrypt_dir, cert_name)))
    monkeypatch.setattr(signage, "start", lambda server, restart: started.append((server.user, restart)))
    return config


def _certify(letsencrypt_dir, email, cert_name, webroot, names, dry_run=False):
    """certbot as far as domainctl can tell: the certificate is there afterwards, with the names in it."""
    if not dry_run:
        live = letsencrypt_dir / "live" / cert_name
        live.mkdir(parents=True, exist_ok=True)
        (live / "fullchain.pem").write_text(" ".join(names))
        (live / "privkey.pem").write_text("key")


@pytest.fixture(autouse=True)
def certificates_hold_their_names(monkeypatch):
    monkeypatch.setattr(certbot, "read", lambda path: certbot.Certificate(
        tuple(path.read_text().split()), datetime.now(UTC) + timedelta(days=60)))


def run(*args):
    result = CliRunner().invoke(cli.app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def test_add_sets_up_a_server_behind_https(server, started):
    result = run("signage", "add", "andre.milocast.nl")

    assert result.exit_code == 0, result.output
    assert signage.all_servers(server) == [Signage("andremilocast", "andre.milocast.nl", 3000)]
    assert started == [("andremilocast", True)]
    assert "PORT=3000" in signage.settings_file(server, "andremilocast").read_text()
    assert "MONGOLAB_URI=mongodb://127.0.0.1:27017/pisignage-andremilocast" in \
        signage.settings_file(server, "andremilocast").read_text()
    lines = openlitespeed.read(server.ols_root)
    assert openlitespeed.virtual_hosts(lines)["andremilocast"] == signage.NOTE
    assert openlitespeed.maps(lines, "http")["andremilocast"] == ["andre.milocast.nl"]
    assert openlitespeed.maps(lines, "https")["andremilocast"] == ["andre.milocast.nl"]
    vhost = signage.site_file(server, "andremilocast").read_text()
    assert "address                 127.0.0.1:3000" in vhost and "websocket / {" in vhost and "vhssl" in vhost
    assert "https://andre.milocast.nl is live with a Let's Encrypt certificate" in result.output
    assert "password pi" in result.output


def test_the_next_customer_gets_the_next_port(server):
    run("signage", "add", "andre.milocast.nl")
    result = run("signage", "add", "bert.milocast.nl")

    assert result.exit_code == 0, result.output
    assert [found.port for found in signage.all_servers(server)] == [3000, 3001]
    assert "127.0.0.1:3001" in signage.site_file(server, "bertmilocast").read_text()


def test_each_server_has_an_external_processor_of_its_own_name(server):
    """OpenLiteSpeed keeps external processors in one list for the whole server. Two virtual hosts that call
    theirs the same thing both end up at whichever was read first, so one customer's screens show another
    customer's players."""
    run("signage", "add", "andre.milocast.nl")
    run("signage", "add", "bert.milocast.nl")

    for user, port in (("andremilocast", 3000), ("bertmilocast", 3001)):
        vhost = signage.site_file(server, user).read_text()
        assert f"extprocessor pisignage-{user} {{" in vhost
        assert f"handler                 pisignage-{user}" in vhost
        assert f"127.0.0.1:{port}" in vhost
    assert "extprocessor pisignage {" not in signage.site_file(server, "bertmilocast").read_text()


def test_a_port_something_else_listens_on_is_skipped(server, monkeypatch):
    monkeypatch.setattr(signage, "listening", lambda port: port == 3000)
    run("signage", "add", "andre.milocast.nl")
    assert signage.find(server, "andremilocast").port == 3001


def test_a_port_another_server_has_is_refused(server):
    run("signage", "add", "andre.milocast.nl")
    result = run("signage", "add", "bert.milocast.nl", "--port", "3000")
    assert result.exit_code == 1
    assert "Port 3000 is the piSignage server andremilocast's" in result.output


def test_until_the_name_points_here_it_is_only_served_on_port_80(server, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {"andre.milocast.nl": ELSEWHERE})

    result = run("signage", "add", "andre.milocast.nl", "--no-dns")

    assert result.exit_code == 0, result.output
    assert signage.find(server, "andremilocast") is not None
    lines = openlitespeed.read(server.ols_root)
    assert openlitespeed.maps(lines, "http")["andremilocast"] == ["andre.milocast.nl"]
    assert "andremilocast" not in openlitespeed.maps(lines, "https")
    assert "vhssl" not in signage.site_file(server, "andremilocast").read_text()
    assert "run the same command again" in result.output


def test_running_it_again_once_the_name_points_here_finishes_it(server, monkeypatch, started):
    monkeypatch.setattr(_Resolver, "answers", {"andre.milocast.nl": ELSEWHERE})
    run("signage", "add", "andre.milocast.nl", "--no-dns")
    monkeypatch.setattr(_Resolver, "answers", {"andre.milocast.nl": HERE})

    result = run("signage", "add", "andre.milocast.nl")

    assert result.exit_code == 0, result.output
    assert openlitespeed.maps(openlitespeed.read(server.ols_root), "https")["andremilocast"] == ["andre.milocast.nl"]
    # The same server again: same port, no restart, and no talk of a new login.
    assert started == [("andremilocast", True), ("andremilocast", False)]
    assert "password pi" not in result.output


def test_a_name_a_site_has_is_refused(server, monkeypatch):
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    sites.save(server, Site("andre", "andre.milocast.nl"))
    result = run("signage", "add", "andre.milocast.nl")
    assert result.exit_code == 1
    assert "already served by the site andre" in result.output


def test_a_site_cannot_take_a_piSignage_server_s_name(server):
    run("signage", "add", "andre.milocast.nl")
    result = run("add", "andre.milocast.nl", "--user", "andre")
    assert result.exit_code == 1
    assert "already the piSignage server andremilocast" in result.output


def test_a_linux_user_that_is_no_piSignage_server_is_refused(server, monkeypatch):
    monkeypatch.setattr(users, "exists", lambda name: True)
    result = run("signage", "add", "andre.milocast.nl")
    assert result.exit_code == 1
    assert "There is already a Linux user andremilocast" in result.output


def test_list_shows_each_server_with_its_port(server):
    run("signage", "add", "andre.milocast.nl")
    result = run("signage", "list")
    assert result.exit_code == 0, result.output
    assert "andremilocast" in result.output and "andre.milocast.nl" in result.output and "3000" in result.output
    assert "until" in result.output


def test_delete_keeps_the_media_and_the_database(server):
    run("signage", "add", "andre.milocast.nl")

    result = run("signage", "delete", "andre.milocast.nl", "--yes")

    assert result.exit_code == 0, result.output
    assert signage.all_servers(server) == []
    lines = openlitespeed.read(server.ols_root)
    assert "andremilocast" not in openlitespeed.virtual_hosts(lines)
    assert "andremilocast" not in openlitespeed.maps(lines, "http")
    assert not signage.site_file(server, "andremilocast").exists()
    assert signage.code_dir(server, "andremilocast").is_dir()
    assert not (server.signage_settings / "andremilocast.dropped").exists()


def test_adding_it_again_takes_the_media_and_the_database_back_up(server):
    run("signage", "add", "andre.milocast.nl")
    run("signage", "delete", "andremilocast", "--yes")

    result = run("signage", "add", "andre.milocast.nl")

    assert result.exit_code == 0, result.output
    assert signage.find(server, "andremilocast") == Signage("andremilocast", "andre.milocast.nl", 3000)
    assert "password pi" not in result.output


def test_what_is_left_after_delete_goes_with_the_second_delete(server):
    run("signage", "add", "andre.milocast.nl")
    run("signage", "delete", "andremilocast", "--yes")

    result = run("signage", "delete", "andremilocast", "--yes")

    assert result.exit_code == 0, result.output
    assert (server.signage_settings / "andremilocast.dropped").exists()


def test_add_says_what_to_do_when_piSignage_is_not_installed(server, tmp_path, monkeypatch):
    monkeypatch.setattr(signage, "UNIT_FILE", tmp_path / "missing")
    result = run("signage", "add", "andre.milocast.nl")
    assert result.exit_code == 1
    assert "web_pisignage: true" in result.output


def test_the_settings_say_what_piSignage_reads():
    text = signage.settings(Signage("andremilocast", "andre.milocast.nl", 3001))
    assert "PORT=3001\n" in text
    assert "MONGOLAB_URI=mongodb://127.0.0.1:27017/pisignage-andremilocast\n" in text


def test_restore_reads_the_old_database_in_under_the_new_name(server, monkeypatch, tmp_path, started):
    run("signage", "add", "andre.milocast.nl")
    fed, owned = [], []
    monkeypatch.setattr(system, "run_fed", lambda *args, source: fed.append((args, source.read())))
    monkeypatch.setattr(system, "run", lambda *args: owned.append(args) or "")
    signage.media_dir(server, "andremilocast").mkdir()
    archive = tmp_path / "andre.archive"
    archive.write_bytes(b"archive")

    result = run("signage", "restore", "andremilocast", "--from-database", "pisignage-server-dev",
                 "--dump", str(archive), "--yes")

    assert result.exit_code == 0, result.output
    (args, data), = fed
    assert data == b"archive"
    assert "--nsFrom=pisignage-server-dev.*" in args and "--nsTo=pisignage-andremilocast.*" in args
    assert "--drop" in args
    assert ("chown", "-hR", "andremilocast:andremilocast", str(signage.media_dir(server, "andremilocast"))) in owned
    assert ("systemctl", "stop", "pisignage@andremilocast.service") in owned
    assert started[-1] == ("andremilocast", True)


def test_restore_starts_piSignage_again_when_the_archive_is_refused(server, monkeypatch, tmp_path, started):
    run("signage", "add", "andre.milocast.nl")
    monkeypatch.setattr(system, "run", lambda *args: "")

    def refuse(*args, source):
        raise signage.CtlError("mongorestore failed: bad archive")
    monkeypatch.setattr(system, "run_fed", refuse)
    archive = tmp_path / "andre.archive"
    archive.write_bytes(b"junk")

    result = run("signage", "restore", "andremilocast", "--from-database", "olddb", "--dump", str(archive), "--yes")

    assert result.exit_code == 1
    assert "bad archive" in result.output
    assert started[-1] == ("andremilocast", True)


def test_restore_refuses_a_database_name_with_pattern_characters(server, tmp_path):
    run("signage", "add", "andre.milocast.nl")
    archive = tmp_path / "andre.archive"
    archive.write_bytes(b"archive")
    result = run("signage", "restore", "andremilocast", "--from-database", "a*", "--dump", str(archive), "--yes")
    assert result.exit_code == 1
    assert "isn't a database name" in result.output
