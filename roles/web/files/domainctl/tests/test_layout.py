from dataclasses import replace
from pathlib import Path

import pytest

from domainctl.core import acl, layout


@pytest.fixture
def no_chown(monkeypatch):
    """The tests don't run as root, so nothing is handed to another user."""
    monkeypatch.setattr(layout, "_own", lambda path, user: None)


def test_the_paths_on_the_way_in_only_get_the_right_to_be_walked_through(config):
    wanted = layout.permissions(config, "example")
    home = config.home("example")
    assert wanted[home] == acl.traversable_by("nobody")
    assert wanted[home / "public_html"] == acl.traversable_by("nobody")


def test_the_web_root_is_readable_and_the_log_directory_writable(config):
    wanted = layout.permissions(config, "example")
    assert wanted[config.docroot_of("example")] == acl.readable_by("nobody")
    assert wanted[config.logs_of("example")] == acl.writable_by("nobody")


def test_the_web_root_gets_its_own_entries_not_the_walk_through_ones(config):
    """public_html/www must not be left with only --x, which would hide the site's files."""
    wanted = layout.permissions(config, "example")
    assert wanted[config.docroot_of("example")] != acl.traversable_by("nobody")


def test_a_web_root_straight_under_the_home_directory_has_nothing_in_between(config):
    flat = replace(config, docroot="www")
    wanted = layout.permissions(flat, "example")
    assert list(wanted) == [flat.home("example"), flat.docroot_of("example"), flat.logs_of("example")]


def test_create_makes_the_web_root_the_logs_and_a_placeholder_page(config, no_chown):
    layout.create(config, "example", "example.nl")
    assert config.docroot_of("example").is_dir()
    assert config.logs_of("example").is_dir()
    assert "example.nl" in (config.docroot_of("example") / "index.html").read_text()


def test_create_leaves_a_page_that_is_already_there_alone(config, no_chown):
    layout.create(config, "example", "example.nl")
    index = config.docroot_of("example") / "index.html"
    index.write_text("the real site")
    layout.create(config, "example", "example.nl")
    assert index.read_text() == "the real site"


def test_missing_permissions_names_every_path_that_is_not_right(config, no_chown, monkeypatch):
    layout.create(config, "example", "example.nl")
    monkeypatch.setattr(acl, "read", lambda path: {})
    wrong = layout.missing_permissions(config, "example")
    assert set(wrong) == set(layout.permissions(config, "example"))


def test_missing_permissions_is_empty_when_everything_is_set(config, no_chown, monkeypatch):
    layout.create(config, "example", "example.nl")
    monkeypatch.setattr(acl, "read", lambda path: dict(layout.permissions(config, "example")[Path(path)]))
    assert layout.missing_permissions(config, "example") == {}


def test_apply_permissions_touches_every_path_that_needs_it(config, no_chown, monkeypatch):
    layout.create(config, "example", "example.nl")
    applied = []
    monkeypatch.setattr(acl, "apply", lambda path, entries: applied.append(path) or True)
    assert layout.apply_permissions(config, "example") is True
    assert applied == list(layout.permissions(config, "example"))


def test_apply_permissions_reports_no_change_when_nothing_was_needed(config, no_chown, monkeypatch):
    layout.create(config, "example", "example.nl")
    monkeypatch.setattr(acl, "apply", lambda path, entries: False)
    assert layout.apply_permissions(config, "example") is False


def test_create_makes_the_challenge_folder_the_template_serves(config, no_chown):
    """The template gives it a context of its own, and OpenLiteSpeed complains about one that isn't there."""
    layout.create(config, "example", "example.nl")
    assert (config.docroot_of("example") / ".well-known" / "acme-challenge").is_dir()


def test_the_placeholder_goes_when_the_site_has_an_index_of_its_own(config, no_chown):
    """OpenLiteSpeed serves index.html before index.php, so a placeholder left there answers for the site."""
    layout.create(config, "example", "example.nl")
    placeholder = config.docroot_of("example") / "index.html"
    assert placeholder.exists()

    (config.docroot_of("example") / "index.php").write_text("<?php // wordpress\n")
    layout.create(config, "example", "example.nl")

    assert not placeholder.exists()


def test_an_index_html_that_isnt_ours_is_left_alone(config, no_chown):
    layout.create(config, "example", "example.nl")
    docroot = config.docroot_of("example")
    (docroot / "index.html").write_text("<h1>the site's own page</h1>")
    (docroot / "index.php").write_text("<?php\n")

    layout.create(config, "example", "example.nl")

    assert (docroot / "index.html").read_text() == "<h1>the site's own page</h1>"


def test_a_site_without_its_own_index_keeps_the_placeholder(config, no_chown):
    layout.create(config, "example", "example.nl")
    (config.docroot_of("example") / "style.css").write_text("body {}")

    layout.create(config, "example", "example.nl")

    assert (config.docroot_of("example") / "index.html").exists()


def test_a_placeholder_from_before_the_marker_is_ours_too(config, no_chown):
    """Servers set up earlier have one without the marker; it blocks index.php just the same."""
    docroot = config.docroot_of("example")
    docroot.mkdir(parents=True)
    (docroot / "index.html").write_text("<html><body><p>This site is set up and waiting for its files.</p></body>")
    (docroot / "index.php").write_text("<?php\n")

    assert layout.remove_placeholder(config, "example") is True
    assert not (docroot / "index.html").exists()
