"""Against OpenLiteSpeed's own httpd_config.conf, not a tidied-up one.

The config the other tests use is written the way this code writes it. A real one is not: the installer and
WebAdmin leave no space before an opening brace, put a "map" on the default listener, and write multi-line values
as heredocs. Those are what a parser gets wrong, and the file it gets wrong is a server's own, set up by hand.
"""

import pytest

from mailctl.core import autodiscover, mailcert, openlitespeed
from mailctl.core.openlitespeed import Member, Template, VirtualHost

# Trimmed from a fresh OpenLiteSpeed install: "listener Default{" with no space, and the heredoc in "module cache".
FRESH = """#
# PLAIN TEXT CONFIGURATION FILE
#
serverName
user                      nobody
group                     nogroup
mime                      $SERVER_ROOT/conf/mime.properties

errorlog $SERVER_ROOT/logs/error.log{
  logLevel                DEBUG
  rollingSize             10M
}

indexFiles                index.html, index.php

virtualHost Example {
  vhRoot                  Example/
  configFile              $SERVER_ROOT/conf/vhosts/$VH_NAME/vhconf.conf
  allowSymbolLink         1
  restrained              1
}

listener Default{
  address                 *:8088
  secure                  0
  map                     Example *
}

module cache {
  internal                1
  param                 <<<END_param
  enableCache 0
  qsCache 1
  END_param
}
"""

# What a server in use has, added in WebAdmin.
LIVE_LISTENERS = """
listener HTTP{
  address                 *:80
  secure                  0
  map                     Example *
}

listener HTTPS{
  address                 *:443
  secure                  1
  keyFile                 /usr/local/lsws/conf/example.key
  map                     Example *
}
"""
NAMES = ["server.hosting.example", "mail.example.nl"]


@pytest.fixture
def fresh(config):
    (config.ols_root / "conf" / "templates").mkdir(parents=True)
    for template in (mailcert.TEMPLATE, autodiscover.TEMPLATE, autodiscover.WAITING_TEMPLATE):
        (config.ols_root / "conf" / "templates" / f"{template}.conf").write_text("")
    openlitespeed.config_file(config.ols_root).write_text(FRESH)
    return config


@pytest.fixture
def live(fresh):
    openlitespeed.config_file(fresh.ols_root).write_text(FRESH + LIVE_LISTENERS)
    return fresh


def test_a_fresh_install_is_read_without_complaint(fresh):
    kinds = {(block.kind, block.name) for block in openlitespeed.blocks(openlitespeed.read(fresh.ols_root))}

    assert ("listener", "Default") in kinds, "an opening brace with no space before it wasn't recognised"
    assert ("virtualhost", "Example") in kinds
    assert ("module", "cache") in kinds


def test_the_default_listener_on_8088_is_not_one_lets_encrypt_can_use(fresh):
    """Nothing serves port 80 yet, so there is nothing to do rather than anything wrong."""
    assert mailcert.can_be_proved(fresh) is False
    with pytest.raises(Exception, match="no HTTP listener on port 80"):
        mailcert.planned_config(fresh, NAMES)


def test_the_mail_names_go_on_the_listeners_a_server_in_use_has(live):
    assert mailcert.can_be_proved(live) is True

    _, after = mailcert.planned_config(live, NAMES)

    assert openlitespeed.members(after, mailcert.TEMPLATE) == NAMES
    template = next(block for block in openlitespeed.blocks(after) if block.name == mailcert.TEMPLATE)
    assert openlitespeed.value(after, template, "listeners") == "HTTP"  # not Default, and not HTTPS


def test_a_change_keeps_every_line_the_server_was_set_up_with(live):
    """The config is set up by hand: mailctl adds its own sites and members and touches nothing else."""
    path = openlitespeed.config_file(live.ols_root)
    before = path.read_text().splitlines()
    _, with_names = mailcert.planned_config(live, NAMES)
    openlitespeed.write(live.ols_root, with_names)
    _, with_site = autodiscover.planned_config(live, "example.nl", https=True)
    openlitespeed.write(live.ols_root, with_site)
    site = VirtualHost("webmail.example.nl", "/var/www/webmail/", "$SERVER_ROOT/conf/vhosts/x/vhconf.conf", "note")
    openlitespeed.write(live.ols_root, openlitespeed.with_virtual_host(openlitespeed.read(live.ols_root), site))

    after = path.read_text().splitlines()

    for line in before:
        assert line in after, f"mailctl lost this line of the server's own config: {line!r}"
    assert after[:3] == before[:3]  # including the header it starts with


def test_a_heredoc_survives_a_change(live):
    _, after = mailcert.planned_config(live, NAMES)
    openlitespeed.write(live.ols_root, after)

    written = openlitespeed.config_file(live.ols_root).read_text()

    assert "  param                 <<<END_param\n  enableCache 0\n  qsCache 1\n  END_param\n" in written
    # And it is still one value, not blocks: a heredoc holding a brace would tear the file in half otherwise.
    assert [block.name for block in openlitespeed.blocks(openlitespeed.read(live.ols_root)) if block.kind == "module"] \
        == ["cache"]


def test_a_member_leaves_the_maps_of_a_server_in_use_alone(live):
    """A template is served through its own "listeners" line, so the map that sends every name to Example, on
    the default listener and on the ones in use, stays as the server has it."""
    template = Template("mailnames", "conf/templates/mailnames.conf", ("HTTP",), "note")

    after = openlitespeed.with_member(openlitespeed.read(live.ols_root), Member("mail.example.nl", "mail.example.nl"),
                                      template)

    assert openlitespeed.maps(after, "Default") == {"Example": ["*"]}
    assert openlitespeed.maps(after, "HTTP") == {"Example": ["*"]}
    assert openlitespeed.maps(after, "HTTPS") == {"Example": ["*"]}
    block = next(found for found in openlitespeed.blocks(after) if found.name == "mailnames")
    assert openlitespeed.value(after, block, "listeners") == "HTTP"
    assert openlitespeed.members(after, "mailnames") == ["mail.example.nl"]
