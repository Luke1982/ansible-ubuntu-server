"""A server whose sites were set up by hand before domainctl, which is how a real one arrives.

Such a server has its members in OpenLiteSpeed's own template, Webhosting, and Linux users with home
directories and certificates to match. domainctl has to find those sites, not quietly start a second set.
"""

import json
from dataclasses import asdict
from ipaddress import ip_address

import pytest
from test_cli import HERE, _Resolver, run

from domainctl import session
from domainctl.core import acl, layout, serving, sites, users
from domainctl.core.sites import Site
from serverctl import certbot, openlitespeed, system

# The member as WebAdmin writes it, in the template OpenLiteSpeed ships: note the capital W.
BY_HAND = """
vhTemplate Webhosting {
  templateFile            $SERVER_ROOT/conf/templates/webhosting.conf
  listeners               http, https

  member casabarata {
    vhDomain              casabarata.nl
    vhAliases             www.casabarata.nl
  }
}
"""


@pytest.fixture
def by_hand(config):
    """A site set up in WebAdmin, in OpenLiteSpeed's own Webhosting template."""
    path = openlitespeed.config_file(config.ols_root)
    path.write_text(path.read_text() + BY_HAND)
    return config


def test_a_site_in_openlitespeeds_own_template_is_found(by_hand):
    assert sites.list_sites(by_hand) == [Site("casabarata", "casabarata.nl", ("www.casabarata.nl",))]
    assert sites.managed(by_hand, sites.read(by_hand)) == "Webhosting"


def test_no_second_template_is_added_beside_it(by_hand):
    sites.ensure_template(by_hand)

    lines = sites.read(by_hand)
    assert [block.name for block in openlitespeed.blocks(lines) if block.kind == "vhtemplate"] == ["Webhosting"]
    assert sites.list_sites(by_hand) == [Site("casabarata", "casabarata.nl", ("www.casabarata.nl",))]


def test_the_template_it_was_using_is_brought_up_to_date_and_keeps_its_member(by_hand):
    sites.ensure_template(by_hand)

    lines = sites.read(by_hand)
    block = next(one for one in openlitespeed.blocks(lines) if one.kind == "vhtemplate")
    assert openlitespeed.value(lines, block, "note") == sites.NOTE
    assert openlitespeed.value(lines, block, "listeners") == "http, https"
    assert openlitespeed.members(lines, "Webhosting") == ["casabarata"]


def test_a_site_is_added_to_the_template_that_is_there(by_hand):
    assert sites.save(by_hand, Site("example", "example.nl")) is True

    assert [site.user for site in sites.list_sites(by_hand)] == ["casabarata", "example"]
    assert [block.name for block in openlitespeed.blocks(sites.read(by_hand)) if block.kind == "vhtemplate"] \
        == ["Webhosting"]


def test_removing_a_site_only_touches_that_member(by_hand):
    sites.save(by_hand, Site("example", "example.nl"))

    assert sites.remove(by_hand, "example") is True

    assert sites.list_sites(by_hand) == [Site("casabarata", "casabarata.nl", ("www.casabarata.nl",))]


def test_a_server_without_one_gets_the_template_it_is_configured_for(config):
    sites.ensure_template(config)

    assert [block.name for block in openlitespeed.blocks(sites.read(config)) if block.kind == "vhtemplate"] \
        == [config.template]
    assert sites.twins(config, sites.read(config)) == []


# What that server looks like today, after a version that added the second template: both blocks are there.

@pytest.fixture
def split(by_hand):
    lines = openlitespeed.with_template(sites.read(by_hand),
                                        openlitespeed.Template("webhosting", "conf/templates/webhosting.conf",
                                                               ("http", "https"), sites.NOTE))
    openlitespeed.write(by_hand.ols_root, lines, "test")
    return by_hand


def test_the_sites_in_the_template_that_was_there_are_the_ones_managed(split):
    assert sites.managed(split, sites.read(split)) == "Webhosting"
    assert sites.twins(split, sites.read(split)) == ["webhosting"]
    assert sites.list_sites(split) == [Site("casabarata", "casabarata.nl", ("www.casabarata.nl",))]


def test_the_block_holding_the_sites_wins_whichever_case_it_has(by_hand):
    """The other way round: a server where domainctl's own block has the sites and an empty one is beside it."""
    lines = sites.read(by_hand)
    for name in ("Webhosting", "webhosting"):  # move the member into the lower-case block
        lines = openlitespeed.with_template(lines, openlitespeed.Template(
            name, "conf/templates/webhosting.conf", ("http", "https"), sites.NOTE))
    lines = openlitespeed.without_member(lines, "casabarata", "Webhosting")
    lines = openlitespeed.with_member(lines, openlitespeed.Member("casabarata", "casabarata.nl"),
                                      openlitespeed.Template("webhosting", "conf/templates/webhosting.conf",
                                                             ("http", "https"), sites.NOTE))
    openlitespeed.write(by_hand.ols_root, lines, "test")

    assert sites.managed(by_hand, sites.read(by_hand)) == "webhosting"
    assert sites.twins(by_hand, sites.read(by_hand)) == ["Webhosting"]
    assert [site.user for site in sites.list_sites(by_hand)] == ["casabarata"]


@pytest.fixture
def server(split, tmp_path, monkeypatch):
    """The commands, against that server: no root, no restarts, no certbot, no DNS, no useradd."""
    settings = tmp_path / "config.json"
    settings.write_text(json.dumps({key: str(value) for key, value in asdict(split).items()}))
    monkeypatch.setenv("DOMAINCTL_CONFIG", str(settings))
    monkeypatch.setattr(system, "require_root", lambda tool: None)
    monkeypatch.setattr(openlitespeed, "restart", lambda root: None)
    monkeypatch.setattr(system, "server_ips", lambda: HERE)
    monkeypatch.setattr(session.Session, "resolver", property(lambda self: _Resolver()))
    monkeypatch.setattr(acl, "read", lambda path: {})
    monkeypatch.setattr(acl, "apply", lambda path, entries: True)
    monkeypatch.setattr(layout, "_own", lambda path, user: None)
    monkeypatch.setattr(users, "create", lambda name, home, shell: home.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(serving, "wait_until_served", lambda docroot, name, addresses, timeout=0: None)
    monkeypatch.setattr(certbot, "obtain", lambda *args, **kwargs: None)
    return split


def test_list_shows_the_site_and_says_there_are_two_templates(server, monkeypatch):
    monkeypatch.setattr(users, "exists", lambda name: False)

    result = run("list")

    assert result.exit_code == 0, result.output
    assert "casabarata.nl" in result.output
    assert "another template named webhosting" in result.output


def test_add_offers_the_user_that_is_already_there(server, monkeypatch):
    """The site's Linux user, home and files exist; --user would make a second user beside them."""
    monkeypatch.setattr(users, "exists", lambda name: name == "example")

    result = run("add", "example.nl", "--no-dns")

    assert result.exit_code == 1
    assert "already a Linux user example" in result.output
    assert "domainctl add example.nl --existing-user" in result.output


def test_add_with_existing_user_makes_the_site_for_it(server, monkeypatch):
    monkeypatch.setattr(users, "exists", lambda name: name == "example")
    (server.home_root / "example").mkdir(parents=True, exist_ok=True)

    result = run("add", "example.nl", "--no-dns", "--existing-user")

    assert result.exit_code == 0, result.output
    assert [site.user for site in sites.list_sites(server)] == ["casabarata", "example"]
