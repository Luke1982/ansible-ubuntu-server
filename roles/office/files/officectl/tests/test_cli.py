"""The commands end to end, against a temporary server: nothing here runs as root or reaches the network."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import pytest
from fakeserver import VERSION, FakeServer, downloads, release
from typer.testing import CliRunner

from officectl import cli, session
from officectl.core import collabora, layout, nextcloud, php, tenants
from serverctl import acl, certbot, dnsnames, openlitespeed, serving, system, users

HERE = {ip_address("81.4.127.10")}
ELSEWHERE = {ip_address("45.87.2.9")}


class _Resolver:
    answers = {"office.example.nl": HERE, "acme.office.example.nl": HERE}

    def addresses(self, name):
        return set(self.answers.get(name, set()))

    def txt(self, name): return []
    def mx(self, name): return []
    def srv(self, name): return []
    def ptr(self, address): return []


@pytest.fixture
def server(config, tmp_path, monkeypatch):
    """A server officectl can work on: no root, no restarts, no certbot, no DNS, no useradd, no downloads."""
    settings = tmp_path / "officectl.json"
    settings.write_text(json.dumps({key: str(value) if isinstance(value, Path) else value
                                    for key, value in asdict(config).items()}))
    monkeypatch.setenv("OFFICECTL_CONFIG", str(settings))
    monkeypatch.setattr(system, "require_root", lambda tool: None)
    monkeypatch.setattr(system, "server_ips", lambda: HERE)
    monkeypatch.setattr(openlitespeed, "restart", lambda root: None)
    monkeypatch.setattr(session.Session, "resolver", property(lambda self: _Resolver()))
    monkeypatch.setattr(acl, "read", lambda path: {})
    # setfacl changes a path's entries once and nothing the next time, which is what a second run depends on.
    corrected: set = set()
    monkeypatch.setattr(acl, "apply", lambda path, entries: path not in corrected and not corrected.add(path))
    monkeypatch.setattr(layout, "own", lambda path, user, group: None)
    monkeypatch.setattr(serving, "wait_until_served", lambda docroot, name, addresses, timeout=0: [])
    monkeypatch.setattr(collabora, "installed", lambda settings: True)
    monkeypatch.setattr(php, "stop_pools", lambda settings: 0)
    made: set[str] = set()
    monkeypatch.setattr(users, "exists", lambda name: name in made)
    monkeypatch.setattr(users, "create",
                        lambda name, home, shell: (made.add(name), home.mkdir(parents=True, exist_ok=True)))
    monkeypatch.setattr(users, "delete", lambda name, remove_home: made.discard(name))
    monkeypatch.setattr(certbot, "read",
                        lambda path: certbot.Certificate((Path(path).parent.name,),
                                                         datetime.now(UTC) + timedelta(days=60)))
    return config


@pytest.fixture
def fake(server, monkeypatch, tmp_path):
    pretend = FakeServer(server)
    monkeypatch.setattr(system, "run", pretend.run)
    monkeypatch.setattr(system, "run_fed", pretend.run_fed)
    monkeypatch.setattr(certbot, "obtain", _issuing(server))
    downloads(monkeypatch, release(tmp_path))
    return pretend


def _issuing(config):
    """certbot, as far as officectl can tell: it leaves a certificate where the real one would be."""
    def obtain(letsencrypt_dir, email, cert_name, webroot, names, dry_run=False):
        if not dry_run:
            certificate(config, cert_name)
    return obtain


def certificate(config, name):
    live = config.letsencrypt_dir / "live" / name
    live.mkdir(parents=True, exist_ok=True)
    (live / "fullchain.pem").write_text("certificate")
    (live / "privkey.pem").write_text("key")


def run(*args):
    result = CliRunner().invoke(cli.app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


# --- Collabora's own site ------------------------------------------------------------------------------------

def test_the_endpoint_is_set_up_with_its_settings_and_a_certificate(server, fake):
    result = run("endpoint")

    assert result.exit_code == 0, result.output
    assert ">loopback</listen>" in server.coolwsd_config.read_text()
    assert openlitespeed.virtual_hosts(openlitespeed.read(server.ols_root))["office.example.nl"]
    assert "https://office.example.nl is live" in result.output
    assert (server.letsencrypt_dir / "live" / "office.example.nl" / "fullchain.pem").is_file()


def test_the_endpoint_is_served_over_https_once_it_has_its_certificate(server, fake):
    run("endpoint")
    assert openlitespeed.maps(openlitespeed.read(server.ols_root), "https") == \
        {"office.example.nl": ["office.example.nl"]}


def test_setting_the_endpoint_up_again_says_the_certificate_is_there(server, fake):
    run("endpoint")
    result = run("endpoint")
    assert result.exit_code == 0, result.output
    assert "has its certificate already" in result.output


def test_the_endpoint_needs_its_name_to_point_here(server, fake, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {"office.example.nl": ELSEWHERE})
    result = run("endpoint", "--no-dns")
    assert result.exit_code == 1
    assert "isn't this server" in result.output and "Point office.example.nl at 81.4.127.10" in result.output


def test_without_collabora_nothing_is_set_up(server, fake, monkeypatch):
    monkeypatch.setattr(collabora, "installed", lambda settings: False)
    result = run("endpoint")
    assert result.exit_code == 1 and "isn't installed" in result.output


def test_a_missing_dns_record_is_published_at_transip(server, fake, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {})
    monkeypatch.setattr("officectl.commands.shared.transip_client", lambda session: None)
    published = []
    monkeypatch.setattr(dnsnames, "publish",
                        lambda client, name, ips: (published.append(name), ("example.nl", ()))[1])
    monkeypatch.setattr(dnsnames, "wait_until_resolving",
                        lambda resolver, name, ips, **kwargs: dnsnames.NameCheck(
                            name, dnsnames.State.HERE, "", frozenset(HERE)))

    result = run("endpoint", "--yes")

    assert result.exit_code == 0, result.output
    assert published == ["office.example.nl"]


# --- A Nextcloud ---------------------------------------------------------------------------------------------

@pytest.fixture
def office(server, fake):
    """A server whose Collabora is already set up, so a Nextcloud can be added to it."""
    run("endpoint")
    return fake


def test_add_sets_a_nextcloud_up_from_nothing(server, office):
    result = run("add", "acme")

    assert result.exit_code == 0, result.output
    assert tenants.list_tenants(server) == [tenants.of(server, "acme")]
    assert server.docroot_of("ncacme").is_dir() and server.data_of("ncacme").is_dir()
    assert php.settings_path(server, "ncacme").is_file()
    assert "ncacme" in office.databases and "ncacme" in office.db_users
    assert nextcloud.version(server, "ncacme") == VERSION
    assert "ncacme" in office.installed
    assert nextcloud.cron_path(server, tenants.of(server, "acme")).is_file()
    assert "https://acme.office.example.nl is ready" in result.output


def test_add_says_the_first_password_once(server, office):
    result = run("add", "acme")
    assert "user      admin" in result.output and "only time it is shown" in result.output


def test_the_first_administrator_can_be_given_another_name(server, office):
    run("add", "acme", "--admin", "guido")
    installed = next(call for call in office.occ_calls if call[1] == "maintenance:install")
    assert "--admin-user=guido" in installed


def test_add_tells_nextcloud_where_it_is_reached_and_what_to_cache_with(server, office):
    run("add", "acme")
    settings = office.system["ncacme"]
    assert settings["trusted_domains"] == ["acme.office.example.nl"]
    assert settings["overwrite.cli.url"] == "https://acme.office.example.nl"
    assert settings["memcache.locking"] == "\\OC\\Memcache\\Redis"
    assert settings["mail_smtphost"] == "localhost"


def test_add_lets_collabora_open_documents_for_it(server, office):
    run("add", "acme")
    assert collabora.allowed_hosts(collabora.read(server.coolwsd_config)) == \
        [r"https://acme\.office\.example\.nl:443"]


def test_add_switches_collabora_s_editor_on_in_it(server, office):
    run("add", "acme")
    assert office.apps["ncacme"][nextcloud.APP] == "enabled"
    assert office.app_settings["ncacme"][nextcloud.APP]["wopi_url"] == "https://office.example.nl"
    assert office.app_settings["ncacme"][nextcloud.APP]["wopi_allowlist"] == "81.4.127.10,127.0.0.1"


def test_add_gets_it_a_certificate_of_its_own_and_serves_it_over_https(server, office):
    run("add", "acme")
    assert (server.letsencrypt_dir / "live" / "acme.office.example.nl" / "fullchain.pem").is_file()
    assert openlitespeed.maps(openlitespeed.read(server.ols_root), "https")["ncacme"] == \
        ["acme.office.example.nl"]


def test_adding_it_again_changes_nothing_and_says_no_new_password(server, office):
    run("add", "acme")
    result = run("add", "acme")
    assert result.exit_code == 0, result.output
    assert "only time it is shown" not in result.output
    assert "has its certificate already" in result.output
    assert len(tenants.list_tenants(server)) == 1
    assert len([call for call in office.occ_calls if call[1] == "maintenance:install"]) == 1


def test_a_nextcloud_is_not_added_before_collabora_works(server, fake):
    result = run("add", "acme")
    assert result.exit_code == 1
    assert "has no certificate yet" in result.output and "officectl endpoint" in result.output
    assert not server.home("ncacme").exists()


def test_a_nextcloud_needs_its_own_name_to_point_here(server, office, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {"office.example.nl": HERE,
                                              "acme.office.example.nl": ELSEWHERE})
    result = run("add", "acme", "--no-dns")
    assert result.exit_code == 1
    assert "isn't this server" in result.output
    assert not server.home("ncacme").exists()


def test_a_name_another_site_answers_for_is_refused(server, office):
    path = server.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace(
        "  secure                  0\n", "  secure                  0\n  map other acme.office.example.nl\n"))
    result = run("add", "acme")
    assert result.exit_code == 1 and "already served by other" in result.output


def test_list_shows_each_one_with_its_version_and_certificate(server, office):
    run("add", "acme")
    result = run("list")
    assert result.exit_code == 0, result.output
    assert "acme" in result.output and VERSION in result.output
    assert "https://acme.office.example.nl" in result.output
    assert "office.example.nl" in result.output


def test_list_says_so_when_there_are_none(server, fake):
    result = run("list")
    assert result.exit_code == 0 and "no Nextclouds on this server yet" in result.output


def test_the_doctor_is_happy_with_one_that_was_just_set_up(server, office, monkeypatch):
    run("add", "acme")
    office.last_cron["ncacme"] = int(datetime.now(UTC).timestamp())
    # The permissions really are the wanted ones: the server fixture answers getfacl with nothing at all.
    monkeypatch.setattr(acl, "read", lambda path: dict(layout.permissions(server, "ncacme").get(path, {})))

    result = run("doctor")

    assert result.exit_code == 0, result.output
    assert "✗" not in result.output, result.output
    assert "Collabora" in result.output and "acme — acme.office.example.nl" in result.output


def test_the_doctor_says_when_the_background_jobs_never_ran(server, office):
    run("add", "acme")
    result = run("doctor", "acme")
    assert "background jobs have never run" in result.output


def test_the_doctor_says_when_a_setting_was_changed_by_hand(server, office):
    run("add", "acme")
    office.system["ncacme"]["overwriteprotocol"] = "http"
    result = run("doctor", "acme")
    assert "isn't what it should be" in result.output and "overwriteprotocol" in result.output


def test_the_doctor_says_when_collabora_s_list_is_not_the_nextclouds_here(server, office):
    run("add", "acme")
    collabora.configure(server, [])
    result = run("doctor")
    assert "aren't the ones on this server" in result.output


def test_occ_hands_everything_after_the_name_to_nextcloud(server, office):
    run("add", "acme")
    result = run("occ", "acme", "user:list", "--output=json")
    assert result.exit_code == 0, result.output
    assert ("ncacme", "user:list", "--output=json") in office.occ_calls


def test_occ_needs_a_nextcloud_that_is_there(server, office):
    result = run("occ", "acme", "status")
    assert result.exit_code == 1 and "no Nextcloud called acme" in result.output


def test_remove_takes_it_off_the_server_and_keeps_its_files(server, office):
    run("add", "acme")

    result = run("remove", "acme", "--yes")

    assert result.exit_code == 0, result.output
    assert tenants.list_tenants(server) == []
    assert collabora.allowed_hosts(collabora.read(server.coolwsd_config)) == []
    assert not nextcloud.cron_path(server, tenants.of(server, "acme")).exists()
    assert not php.settings_path(server, "ncacme").exists()
    assert server.docroot_of("ncacme").is_dir() and "ncacme" in office.databases
    assert "still there" in result.output


def test_remove_with_purge_takes_the_database_and_the_files_too(server, office):
    run("add", "acme")
    result = run("remove", "acme", "--purge", "--yes")
    assert result.exit_code == 0, result.output
    assert "ncacme" not in office.databases
    assert "Removed the Linux user ncacme" in result.output


def test_remove_needs_a_nextcloud_that_is_there(server, office):
    result = run("remove", "acme", "--yes")
    assert result.exit_code == 1 and "no Nextcloud called acme" in result.output


def test_update_puts_the_newest_release_in_place_and_upgrades_the_database(server, office, monkeypatch,
                                                                           tmp_path):
    run("add", "acme")
    downloads(monkeypatch, release(tmp_path, "32.0.2"))

    result = run("update", "acme", "--yes")

    assert result.exit_code == 0, result.output
    assert nextcloud.version(server, "ncacme") == "32.0.2"
    assert office.upgraded == ["ncacme"]
    assert "ncacme" not in office.maintenance
    assert "running Nextcloud 32.0.2" in result.output


def test_update_says_so_when_the_newest_release_is_the_one_in_place(server, office):
    run("add", "acme")
    result = run("update", "acme", "--yes")
    assert result.exit_code == 0, result.output
    assert "is the one in place" in result.output and office.upgraded == []


def test_update_leaves_a_nextcloud_that_is_not_set_up_alone(server, office):
    run("add", "acme")
    office.installed.discard("ncacme")
    result = run("update", "acme", "--yes")
    assert result.exit_code == 0, result.output
    assert "nothing to update" in result.output


# --- What the playbook runs ----------------------------------------------------------------------------------

def test_sync_writes_everything_officectl_owns(server, office):
    run("add", "acme")
    php.settings_path(server, "ncacme").write_text("; changed by hand\n")
    nextcloud.cron_path(server, tenants.of(server, "acme")).unlink()
    collabora.configure(server, [])

    result = run("sync")

    assert result.exit_code == 0, result.output
    assert "Changed Collabora and the Nextclouds." in result.output
    assert "memory_limit" in php.settings_path(server, "ncacme").read_text()
    assert nextcloud.cron_path(server, tenants.of(server, "acme")).is_file()
    assert collabora.allowed_hosts(collabora.read(server.coolwsd_config)) == \
        [r"https://acme\.office\.example\.nl:443"]


def test_sync_says_when_nothing_changed(server, office):
    run("add", "acme")
    run("sync")
    result = run("sync")
    assert result.exit_code == 0, result.output
    assert "Nothing changed." in result.output


def test_sync_leaves_nextcloud_s_own_settings_alone(server, office):
    run("add", "acme")
    office.system["ncacme"]["overwriteprotocol"] = "http"
    run("sync")
    assert office.system["ncacme"]["overwriteprotocol"] == "http"


def test_sync_works_on_a_server_that_has_no_nextclouds_yet(server, fake):
    result = run("sync")
    assert result.exit_code == 0, result.output
    assert ">loopback</listen>" in server.coolwsd_config.read_text()
