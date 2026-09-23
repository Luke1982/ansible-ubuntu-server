"""Tests for pmasite. Run them from the project root:

    python3 -m pytest roles/db/files/pmasite
"""

import os
import stat

import pytest

import pmasite
from pmasite import ConfigError, Site

# Laid out like WebAdmin writes it, with the listeners of a server set up by hand.
HTTPD_CONFIG = """\
serverName                web01
user                      nobody
group                     nogroup

rewrite  {
  enable                  1
  # Braces in rules don't open or close blocks.
  rules                   <<<END_rules
RewriteRule ^/old/(.{1,3})$ /new/$1 [R=301,L]
RewriteRule ^/x}$ /y [L]
  END_rules
}

virtualhost shop {
  vhRoot                  /var/www/shop/
  configFile              $SERVER_ROOT/conf/vhosts/shop/vhconf.conf
}

listener HTTP {
  address                 *:80
  secure                  0
  map                     shop shop.example.nl
}

listener HTTPS {
  address                 *:443
  secure                  1
  map                     shop shop.example.nl
}

listener HTTPS6{
  address                 [::]:443
  secure                  1
}

listener Admin {
  address                 *:7080
  secure                  1
}
"""

SITE = Site("pma.web01.example.nl", "/var/www/phpmyadmin/",
            "$SERVER_ROOT/conf/vhosts/pma.web01.example.nl/vhconf.conf", "Managed by Ansible: phpMyAdmin")


def lines(text=HTTPD_CONFIG):
    return text.splitlines()


def block_text(config, kind, name):
    """The block's lines, as one string."""
    found = next(block for block in pmasite.blocks(config)
                 if block.kind == kind and block.name == name and not block.depth)
    return "\n".join(config[found.start:found.end + 1])


def maps(config, listener):
    """The names the listener gives each virtual host."""
    found = next(block for block in pmasite.blocks(config) if block.kind == "listener" and block.name == listener)
    mapped = {}
    for line in config[found.start + 1:found.end]:
        key, _, rest = line.strip().partition(" ")
        if key.lower() == "map":
            host, _, names = rest.strip().partition(" ")
            mapped[host] = names.strip()
    return mapped


def test_listeners_are_those_for_port_80_and_443():
    assert pmasite.listeners(lines()) == (["HTTP"], ["HTTPS", "HTTPS6"])


def test_braces_in_rewrite_rules_dont_open_a_block():
    assert [(block.kind, block.name) for block in pmasite.blocks(lines())] == [
        ("rewrite", ""), ("virtualhost", "shop"), ("listener", "HTTP"), ("listener", "HTTPS"),
        ("listener", "HTTPS6"), ("listener", "Admin"),
    ]


def test_configure_adds_the_virtual_host():
    updated = pmasite.configure(lines(), SITE)

    assert block_text(updated, "virtualhost", SITE.name) == """\
virtualhost pma.web01.example.nl {
  vhRoot                  /var/www/phpmyadmin/
  configFile              $SERVER_ROOT/conf/vhosts/pma.web01.example.nl/vhconf.conf
  allowSymbolLink         0
  enableScript            1
  restrained              1
  setUIDMode              0
  note                    Managed by Ansible: phpMyAdmin
}"""


def test_configure_puts_the_name_on_every_listener_for_port_80_and_443():
    updated = pmasite.configure(lines(), SITE)

    for listener in ("HTTP", "HTTPS", "HTTPS6"):
        assert maps(updated, listener)[SITE.name] == SITE.name


def test_configure_leaves_the_other_sites_and_listeners_alone():
    updated = pmasite.configure(lines(), SITE)

    assert block_text(updated, "virtualhost", "shop") == block_text(lines(), "virtualhost", "shop")
    assert maps(updated, "HTTP")["shop"] == "shop.example.nl"
    assert block_text(updated, "listener", "Admin") == block_text(lines(), "listener", "Admin")


def test_configure_changes_nothing_when_the_site_is_already_there():
    once = pmasite.configure(lines(), SITE)

    assert pmasite.configure(once, SITE) == once


def test_configure_brings_a_changed_setting_up_to_date():
    once = pmasite.configure(lines(), SITE)

    updated = pmasite.configure(once, Site(SITE.name, "/srv/phpmyadmin/", SITE.config_file, SITE.note))

    assert len([block for block in pmasite.blocks(updated) if block.name == SITE.name]) == 1
    assert "/srv/phpmyadmin/" in block_text(updated, "virtualhost", SITE.name)
    assert "/var/www/phpmyadmin/" not in block_text(updated, "virtualhost", SITE.name)


def test_configure_removes_a_site_it_made_under_another_name():
    """The server was renamed: the previous phpMyAdmin site, marked with the same note, goes with its name."""
    previous = Site("pma.old.example.nl", SITE.root, "$SERVER_ROOT/conf/vhosts/pma.old.example.nl/vhconf.conf",
                    SITE.note)
    config = pmasite.configure(lines(), previous)

    updated = pmasite.configure(config, SITE)

    assert "pma.old.example.nl" not in "\n".join(updated)
    assert maps(updated, "HTTP") == {"shop": "shop.example.nl", SITE.name: SITE.name}


def test_configure_leaves_a_site_it_didnt_make_alone():
    """Without the note it is somebody else's, even under a name that looks like one of ours."""
    mine = Site("pma.web01.example.nl", SITE.root, SITE.config_file, "Made in WebAdmin")
    config = pmasite.configure(lines(), mine)

    updated = pmasite.configure(config, Site("pma.web02.example.nl", SITE.root, SITE.config_file, SITE.note))

    assert "pma.web01.example.nl" in "\n".join(updated)


def test_configure_replaces_a_map_that_gives_the_site_another_name():
    config = lines(HTTPD_CONFIG.replace("listener HTTP {\n", f"listener HTTP {{\n  map {SITE.name} elsewhere\n"))

    updated = pmasite.configure(config, SITE)

    assert maps(updated, "HTTP")[SITE.name] == SITE.name


@pytest.mark.parametrize("port, elsewhere, missing", [(":80", ":8080", "no listener for port 80"),
                                                     (":443", ":8443", "no listener for port 443")])
def test_a_missing_listener_is_an_error(port, elsewhere, missing):
    config = lines(HTTPD_CONFIG.replace(port, elsewhere))

    with pytest.raises(ConfigError, match=missing):
        pmasite.configure(config, SITE)


@pytest.mark.parametrize("text", ["listener HTTP {\n  address *:80\n", "}\n", "rewrite {\n  rules <<<END\n}\n"])
def test_a_config_pmasite_cant_follow_is_an_error(text):
    with pytest.raises(ConfigError, match="Can.t read"):
        pmasite.blocks(lines(text))


def config_file(tmp_path):
    path = tmp_path / "httpd_config.conf"
    path.write_text(HTTPD_CONFIG)
    path.chmod(0o640)
    return path


def test_apply_writes_the_config_with_its_mode_and_keeps_the_previous_version(tmp_path):
    path = config_file(tmp_path)

    assert pmasite.apply(path, SITE) is True
    assert SITE.name in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.with_suffix(".conf.pmasite.bak").read_text() == HTTPD_CONFIG


def test_apply_leaves_the_config_alone_when_the_site_is_already_there(tmp_path):
    path = config_file(tmp_path)
    pmasite.apply(path, SITE)
    before = path.read_text()

    assert pmasite.apply(path, SITE) is False
    assert path.read_text() == before


def test_apply_refuses_a_symbolic_link(tmp_path):
    path = config_file(tmp_path)
    link = tmp_path / "linked.conf"
    link.symlink_to(path)

    with pytest.raises(ConfigError, match="symbolic link"):
        pmasite.apply(link, SITE)


def test_apply_reports_a_config_that_isnt_there(tmp_path):
    with pytest.raises(ConfigError, match="no OpenLiteSpeed config"):
        pmasite.apply(tmp_path / "gone.conf", SITE)


def test_main_reports_what_it_changed(tmp_path, capsys):
    path = config_file(tmp_path)
    arguments = ["--config", str(path), "--name", SITE.name, "--root", SITE.root, "--vhost-config", SITE.config_file]

    assert pmasite.main(arguments) == 0
    assert capsys.readouterr().out.startswith("Changed:")
    assert pmasite.main(arguments) == 0
    assert capsys.readouterr().out.startswith("No change:")


def test_main_reports_a_problem_without_a_traceback(tmp_path, capsys):
    arguments = ["--config", str(tmp_path / "gone.conf"), "--name", SITE.name, "--root", SITE.root,
                 "--vhost-config", SITE.config_file]

    assert pmasite.main(arguments) == 1
    assert "no OpenLiteSpeed config" in capsys.readouterr().err
