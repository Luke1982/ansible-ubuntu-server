"""Nextcloud itself: its own occ command, the settings officectl keeps, its jobs and its files."""

import pytest
from fakeserver import VERSION, FakeServer, downloads, release

from officectl.core import layout, nextcloud, php, tenants
from serverctl import system


@pytest.fixture
def tenant(config):
    return tenants.of(config, "acme")


@pytest.fixture
def fake(config, monkeypatch):
    pretend = FakeServer(config)
    monkeypatch.setattr(system, "run", pretend.run)
    monkeypatch.setattr(system, "run_fed", pretend.run_fed)
    monkeypatch.setattr(layout, "own", lambda path, user, group: None)
    return pretend


def test_occ_runs_as_the_user_the_pages_run_as(config, tenant, fake):
    nextcloud.occ(config, tenant, "status")
    args = fake.calls[-1]
    assert args[:4] == ("runuser", "-u", config.web_user, "--")
    assert f"--chdir={config.docroot_of('ncacme')}" in args
    assert str(config.php()) in args


def test_occ_runs_on_this_nextcloud_s_own_php_settings(config, tenant, fake):
    nextcloud.occ(config, tenant, "status")
    assert f"PHP_INI_SCAN_DIR={php.scan_dir(config, 'ncacme')}" in fake.calls[-1]
    assert f"HOME={config.tmp_of('ncacme')}" in fake.calls[-1]


def test_a_nextcloud_that_is_set_up_says_so(config, tenant, fake):
    assert not nextcloud.installed(config, tenant)
    fake.installed.add("ncacme")
    assert nextcloud.installed(config, tenant)


def test_a_nextcloud_that_does_not_answer_is_not_set_up(config, tenant, fake):
    fake.failing = "status"
    assert nextcloud.status(config, tenant) == {}
    assert not nextcloud.installed(config, tenant)


def test_a_warning_php_printed_in_front_of_the_answer_is_stepped_over(config, tenant, fake, monkeypatch):
    monkeypatch.setattr(system, "run", lambda *args, stdin=None: 'PHP Warning: something\n{"installed":true}')
    assert nextcloud.installed(config, tenant)


def test_the_settings_say_where_it_is_reached_and_what_it_caches_with(config, tenant):
    wanted = {".".join(setting.keys): setting.value for setting in nextcloud.settings(config, tenant)}
    assert wanted["trusted_domains.0"] == "acme.office.example.nl"
    assert wanted["overwrite.cli.url"] == "https://acme.office.example.nl"
    assert wanted["overwriteprotocol"] == "https"
    assert wanted["memcache.locking"] == "\\OC\\Memcache\\Redis"
    assert wanted["redis.host"] == "localhost"


def test_the_settings_that_are_already_right_are_not_set_again(config, tenant, fake):
    nextcloud.apply_settings(config, tenant, nextcloud.settings(config, tenant))
    assert nextcloud.settings_to_change(nextcloud.settings(config, tenant),
                                        nextcloud.current_settings(config, tenant)) == []


def test_a_setting_that_is_wrong_is_the_one_that_is_changed(config, tenant, fake):
    nextcloud.apply_settings(config, tenant, nextcloud.settings(config, tenant))
    nextcloud.apply_settings(config, tenant, [nextcloud.Setting(("redis", "port"), "1234", "integer")])
    to_change = nextcloud.settings_to_change(nextcloud.settings(config, tenant),
                                             nextcloud.current_settings(config, tenant))
    assert [setting.keys for setting in to_change] == [("redis", "port")]


def test_a_number_is_set_as_a_number(config, tenant, fake):
    nextcloud.apply_settings(config, tenant, [nextcloud.Setting(("redis", "port"), "6379", "integer")])
    assert nextcloud.current_settings(config, tenant)["redis"]["port"] == 6379


def test_a_setting_that_is_not_there_at_all_is_one_to_change(config, tenant, fake):
    assert len(nextcloud.settings_to_change(nextcloud.settings(config, tenant), {})) == \
        len(nextcloud.settings(config, tenant))


def test_how_a_new_nextcloud_sends_its_mail(config):
    wanted = {".".join(setting.keys): setting.value for setting in nextcloud.mail_settings(config)}
    assert wanted["mail_smtpmode"] == "smtp" and wanted["mail_smtphost"] == "localhost"
    assert wanted["mail_from_address"] == "nextcloud" and wanted["mail_domain"] == "example.nl"


def test_collabora_s_editor_is_switched_on_if_it_is_off(config, tenant, fake):
    assert nextcloud.add_app(config, tenant, nextcloud.APP, {})
    assert nextcloud.apps(config, tenant) == {nextcloud.APP: "enabled"}
    assert not nextcloud.add_app(config, tenant, nextcloud.APP, nextcloud.apps(config, tenant))


def test_an_app_that_is_there_but_off_is_enabled_and_not_installed_again(config, tenant, fake):
    fake.apps["ncacme"] = {nextcloud.APP: "disabled"}
    nextcloud.add_app(config, tenant, nextcloud.APP, nextcloud.apps(config, tenant))
    assert ("ncacme", "app:enable", nextcloud.APP) in fake.occ_calls


def test_it_is_told_where_collabora_is(config, tenant, fake):
    wanted = nextcloud.collabora_settings(config, ["81.4.127.10", "127.0.0.1"])
    assert wanted["wopi_url"] == "https://office.example.nl"
    assert wanted["wopi_allowlist"] == "81.4.127.10,127.0.0.1"
    assert nextcloud.set_app_settings(config, tenant, nextcloud.APP, wanted)
    assert nextcloud.app_settings(config, tenant, nextcloud.APP) == wanted


def test_telling_it_again_changes_nothing(config, tenant, fake):
    wanted = nextcloud.collabora_settings(config, ["127.0.0.1"])
    nextcloud.set_app_settings(config, tenant, nextcloud.APP, wanted)
    before = len(fake.occ_calls)
    assert not nextcloud.set_app_settings(config, tenant, nextcloud.APP, wanted)
    assert len(fake.occ_calls) == before + 1  # only the reading of what is there now


def test_the_editor_reads_its_settings_again_once_they_change(config, tenant, fake):
    nextcloud.set_app_settings(config, tenant, nextcloud.APP, {"wopi_url": "https://office.example.nl"})
    assert ("ncacme", f"{nextcloud.APP}:activate-config") in fake.occ_calls


def test_its_background_jobs_run_every_few_minutes_as_the_user_the_pages_run_as(config, tenant):
    text = nextcloud.cron(config, tenant)
    assert f"*/{config.cron_minutes} * * * * {config.web_user} {config.php()} -f " \
           f"{config.docroot_of('ncacme')}/cron.php" in text
    assert f"PHP_INI_SCAN_DIR={php.scan_dir(config, 'ncacme')}" in text


def test_writing_the_cron_file_twice_changes_nothing_the_second_time(config, tenant):
    assert nextcloud.write_cron(config, tenant)
    assert not nextcloud.write_cron(config, tenant)
    assert nextcloud.cron_path(config, tenant).name == "nextcloud-ncacme"
    assert nextcloud.remove_cron(config, tenant)


def test_when_its_background_jobs_last_ran(config, tenant, fake):
    assert nextcloud.last_cron(config, tenant) == 0
    fake.last_cron["ncacme"] = 1762000000
    assert nextcloud.last_cron(config, tenant) == 1762000000


def test_the_files_are_put_in_the_web_root(config, tenant, fake, monkeypatch, tmp_path):
    layout.create(config, "ncacme")
    downloads(monkeypatch, release(tmp_path))
    assert nextcloud.put_code_in_place(config, tenant) == VERSION
    assert nextcloud.code_here(config, "ncacme")
    assert nextcloud.version(config, "ncacme") == VERSION
    # The challenge folder was there first and has to stay: the certificate is renewed through it.
    assert (config.docroot_of("ncacme") / ".well-known" / "acme-challenge").is_dir()


def test_a_newer_release_keeps_the_settings_and_the_apps_of_its_own(config, tenant, fake, monkeypatch,
                                                                    tmp_path):
    layout.create(config, "ncacme")
    downloads(monkeypatch, release(tmp_path))
    nextcloud.put_code_in_place(config, tenant)
    docroot = config.docroot_of("ncacme")
    (docroot / "config" / "config.php").write_text("<?php // this Nextcloud's own\n")
    (docroot / "apps" / "richdocuments").mkdir()
    downloads(monkeypatch, release(tmp_path, "32.0.2"))

    version, kept = nextcloud.replace_code(config, tenant)

    assert version == "32.0.2" and nextcloud.version(config, "ncacme") == "32.0.2"
    assert "this Nextcloud's own" in (docroot / "config" / "config.php").read_text()
    assert (docroot / "apps" / "richdocuments").is_dir()  # installed here, so it is carried over
    assert (docroot / ".well-known" / "acme-challenge").is_dir()
    assert kept is not None and (kept / "version.php").is_file()  # the old files, to go back to


def test_the_release_that_is_already_in_place_is_not_put_in_place_again(config, tenant, fake, monkeypatch,
                                                                        tmp_path):
    layout.create(config, "ncacme")
    downloads(monkeypatch, release(tmp_path))
    nextcloud.put_code_in_place(config, tenant)
    version, kept = nextcloud.replace_code(config, tenant)
    assert (version, kept) == (VERSION, None)


def test_bringing_the_database_up_to_date_switches_the_maintenance_page_off(config, tenant, fake):
    fake.installed.add("ncacme")
    nextcloud.occ(config, tenant, "maintenance:mode", "--on")
    nextcloud.upgrade(config, tenant)
    assert fake.upgraded == ["ncacme"] and "ncacme" not in fake.maintenance
    assert ("ncacme", "db:add-missing-indices") in fake.occ_calls


def test_a_nextcloud_in_maintenance_mode_needs_the_upgrade_run(config, tenant, fake):
    fake.installed.add("ncacme")
    assert not nextcloud.needs_upgrade(config, tenant)
    fake.maintenance.add("ncacme")
    assert nextcloud.needs_upgrade(config, tenant)


def test_the_version_of_files_that_are_not_there_is_nothing(config):
    assert nextcloud.version(config, "ncacme") == ""
    assert not nextcloud.code_here(config, "ncacme")
