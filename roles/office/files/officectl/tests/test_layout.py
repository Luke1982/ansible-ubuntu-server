"""The directories one Nextcloud has: who owns them, and who may read them."""

import pytest

from officectl.core import layout
from serverctl import acl


@pytest.fixture
def owned(monkeypatch):
    """Records who each directory would be given to: the tests don't run as root."""
    given = {}
    monkeypatch.setattr(layout, "own", lambda path, user, group: given.__setitem__(path, (user, group)))
    return given


def test_nextcloud_s_code_is_root_s(config):
    docroot = config.docroot_of("ncacme")
    assert layout.directories(config, "ncacme")[docroot][0] == "root"


def test_what_nextcloud_writes_belongs_to_the_user_php_runs_as(config):
    wanted = layout.directories(config, "ncacme")
    docroot = config.docroot_of("ncacme")
    for path in (docroot / "config", docroot / "apps", config.data_of("ncacme"), config.tmp_of("ncacme")):
        assert wanted[path][0] == config.web_user, path


def test_the_files_people_store_are_not_under_the_web_root(config):
    assert config.docroot_of("ncacme") not in config.data_of("ncacme").parents


def test_nobody_else_may_read_the_files_people_store(config):
    assert layout.directories(config, "ncacme")[config.data_of("ncacme")][1] == 0o770


def test_its_home_directory_is_its_own(config):
    assert layout.directories(config, "ncacme")[config.home("ncacme")] == ("ncacme", 0o750)


def test_creating_them_makes_every_directory(config, owned):
    layout.create(config, "ncacme")
    for path in layout.directories(config, "ncacme"):
        assert path.is_dir(), path


def test_creating_them_again_keeps_what_is_in_them(config, owned):
    layout.create(config, "ncacme")
    (config.docroot_of("ncacme") / "index.php").write_text("<?php")
    layout.create(config, "ncacme")
    assert (config.docroot_of("ncacme") / "index.php").is_file()


def test_the_web_server_is_let_into_the_home_directory_and_no_further(config):
    wanted = layout.permissions(config, "ncacme")
    assert wanted[config.home("ncacme")] == acl.traversable_by(config.web_user)
    assert wanted[config.home("ncacme") / "public_html"] == acl.traversable_by(config.web_user)
    assert wanted[config.docroot_of("ncacme")] == acl.readable_by(config.web_user)
    assert wanted[config.logs_of("ncacme")] == acl.writable_by(config.web_user)


def test_the_web_server_is_not_let_into_what_people_store(config):
    assert config.data_of("ncacme") not in layout.permissions(config, "ncacme")


def test_the_permissions_that_are_right_are_left_alone(config, owned, monkeypatch):
    layout.create(config, "ncacme")
    monkeypatch.setattr(acl, "read", lambda path: dict(layout.permissions(config, "ncacme")[path]))
    assert not layout.apply_permissions(config, "ncacme")
    assert layout.missing_permissions(config, "ncacme") == {}


def test_the_permissions_that_are_wrong_are_reported(config, owned, monkeypatch):
    layout.create(config, "ncacme")
    monkeypatch.setattr(acl, "read", lambda path: {})
    assert set(layout.missing_permissions(config, "ncacme")) == set(layout.permissions(config, "ncacme"))


def test_a_home_directory_officectl_made_is_known_by_what_is_in_it(config, owned):
    assert not layout.made_here(config, "ncacme")
    layout.create(config, "ncacme")
    assert layout.made_here(config, "ncacme")
