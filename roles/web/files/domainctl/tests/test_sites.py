import pytest

from domainctl.core import sites
from domainctl.core.sites import Site
from serverctl import openlitespeed
from serverctl.errors import CtlError


def test_a_new_server_has_no_sites(config):
    assert sites.list_sites(config) == []


def test_saving_a_site_adds_the_template_and_the_member(config):
    assert sites.save(config, Site("example", "example.nl", ("www.example.nl",))) is True
    assert sites.list_sites(config) == [Site("example", "example.nl", ("www.example.nl",))]
    lines = sites.read(config)
    block = next(one for one in openlitespeed.blocks(lines) if one.kind == "vhtemplate")
    assert openlitespeed.value(lines, block, "templateFile") == "conf/templates/webhosting.conf"
    assert openlitespeed.value(lines, block, "listeners") == "http, https"
    assert openlitespeed.value(lines, block, "note") == sites.NOTE


def test_saving_the_same_site_again_changes_nothing(config):
    site = Site("example", "example.nl", ("www.example.nl",))
    sites.save(config, site)
    assert sites.save(config, site) is False


def test_adding_www_later_updates_the_member(config):
    sites.save(config, Site("example", "example.nl"))
    assert sites.save(config, Site("example", "example.nl", ("www.example.nl",))) is True
    assert sites.find(config, "example").aliases == ("www.example.nl",)


def test_removing_a_site_leaves_the_others(config):
    sites.save(config, Site("one", "one.nl"))
    sites.save(config, Site("two", "two.nl"))
    assert sites.remove(config, "one") is True
    assert [site.user for site in sites.list_sites(config)] == ["two"]
    assert sites.remove(config, "one") is False


def test_a_name_another_site_already_serves_is_reported(config):
    sites.save(config, Site("one", "one.nl", ("www.one.nl",)))
    lines = sites.read(config)
    assert "already served by the site one" in sites.taken_by_another(config, ("one.nl",), "two", lines)
    assert "already served by the site one" in sites.taken_by_another(config, ("www.one.nl",), "two", lines)
    assert sites.taken_by_another(config, ("two.nl",), "two", lines) is None


def test_a_site_may_keep_its_own_names(config):
    sites.save(config, Site("one", "one.nl"))
    lines = sites.read(config)
    assert sites.taken_by_another(config, ("one.nl",), "one", lines) is None


def test_a_hand_made_virtual_host_with_the_same_name_is_left_alone(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text() + "\nvirtualHost example.nl {\n  vhRoot /var/www/example/\n}\n")
    lines = sites.read(config)
    assert "leaves alone" in sites.taken_by_another(config, ("example.nl",), "example", lines)


def test_ensure_template_adds_the_block_without_any_member(config):
    assert sites.ensure_template(config) is True
    assert sites.list_sites(config) == []
    assert sites.ensure_template(config) is False


def test_ensure_template_keeps_the_members_when_a_listener_is_added(config):
    sites.save(config, Site("example", "example.nl"))
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("listener https {", "listener https2 {\n  address *:443\n  secure 1\n}\n\nlistener https {"))
    assert sites.ensure_template(config) is True
    assert [site.user for site in sites.list_sites(config)] == ["example"]


def test_a_server_without_an_http_listener_cannot_serve_a_challenge(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("  address                 *:80", "  address                 *:8080"))
    with pytest.raises(CtlError, match="no HTTP listener"):
        sites.ensure_template(config)


def test_a_server_without_an_https_listener_cannot_serve_a_site(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("  address                 *:443", "  address                 *:8443"))
    with pytest.raises(CtlError, match="no HTTPS listener"):
        sites.ensure_template(config)


def test_the_certificate_covers_the_domain_and_its_aliases():
    assert Site("example", "example.nl", ("www.example.nl",)).names == ("example.nl", "www.example.nl")
    assert Site("example", "example.nl").names == ("example.nl",)


def test_listing_sites_works_without_any_listener(config):
    """So 'domainctl sync' can still put the permissions right on a server that isn't finished yet."""
    sites.save(config, Site("example", "example.nl"))
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("*:80", "*:8080").replace("*:443", "*:8443"))
    assert [site.user for site in sites.list_sites(config)] == ["example"]
