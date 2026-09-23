"""Against OpenLiteSpeed's own default httpd_config.conf, not a tidied-up one.

The fixture used by the other tests is written the way this code writes it. A real config is not: WebAdmin and
the installer leave no space before an opening brace, put a "map" on the default listener, and write multi-line
values with heredocs. Those are the things a parser gets wrong.
"""

import pytest

from domainctl.core import sites
from domainctl.core.sites import Site
from serverctl import openlitespeed
from serverctl.errors import CtlError

# Trimmed from a fresh OpenLiteSpeed install: note "listener Default{" with no space, and the heredoc in
# "module cache".
FRESH = """#
# PLAIN TEXT CONFIGURATION FILE
#
serverName
user                      nobody
group                     nogroup
priority                  0
enableChroot              0
mime                      $SERVER_ROOT/conf/mime.properties
adminEmails               root@localhost

errorlog $SERVER_ROOT/logs/error.log{
  logLevel                DEBUG
  debugLevel              0
  rollingSize             10M
  enableStderrLog         1
}

accesslog $SERVER_ROOT/logs/access.log{
  rollingSize             10M
  keepDays                30
  compressArchive         0
}

indexFiles                index.html, index.php

expires  {
  enableExpires           1
  expiresByType           image/*=A604800,text/css=A604800
}

fileAccessControl  {
  followSymbolLink        1
  checkSymbolLink         0
  requiredPermissionMask  000
}

virtualHost Example {
  vhRoot                  Example/
  configFile              $SERVER_ROOT/conf/vhosts/$VH_NAME/vhconf.conf
  allowSymbolLink         1
  enableScript            1
  restrained              1
  setUIDMode              0
}

listener Default{
  address                 *:8088
  secure                  0
  map                     Example *
}

module cache {
  internal                1
  checkPrivateCache       1
  checkPublicCache        1
  param                 <<<END_param
  enableCache 0
  qsCache 1
  END_param
}
"""

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
  certFile                /usr/local/lsws/conf/example.crt
  map                     Example *
}
"""


@pytest.fixture
def fresh(config):
    (config.ols_root / "conf" / "httpd_config.conf").write_text(FRESH)
    return config


def test_a_fresh_install_is_read_without_complaint(fresh):
    lines = sites.read(fresh)
    kinds = {(block.kind, block.name) for block in openlitespeed.blocks(lines)}
    assert ("listener", "Default") in kinds, "an opening brace with no space before it wasn't recognised"
    assert ("virtualhost", "Example") in kinds
    assert ("module", "cache") in kinds
    assert sites.list_sites(fresh) == []


def test_a_fresh_install_has_no_listener_a_site_can_use(fresh):
    """Port 8088 only, so domainctl says so instead of adding a template nothing serves."""
    with pytest.raises(CtlError, match="no HTTP listener"):
        sites.ensure_template(fresh)


def test_a_site_is_added_once_the_listeners_are_there(fresh):
    path = fresh.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    assert sites.save(fresh, Site("example", "example.nl", ("www.example.nl",))) is True
    assert sites.list_sites(fresh) == [Site("example", "example.nl", ("www.example.nl",))]


def test_everything_that_was_there_is_still_there_afterwards(fresh):
    """The config is set up by hand, so nothing but the template block may change."""
    path = fresh.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    before = path.read_text()
    sites.save(fresh, Site("example", "example.nl"))
    after = path.read_text()
    for line in before.splitlines():
        assert line in after.splitlines(), f"the playbook lost this line: {line!r}"
    assert after.startswith("#\n# PLAIN TEXT CONFIGURATION FILE")


def test_the_heredoc_is_not_broken_by_a_change(fresh):
    path = fresh.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    sites.save(fresh, Site("example", "example.nl"))
    after = path.read_text()
    assert "param                 <<<END_param" in after
    assert after.count("END_param") == 2


def test_the_listeners_a_site_uses_are_both_named_on_the_template(fresh):
    path = fresh.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    sites.save(fresh, Site("example", "example.nl"))
    lines = sites.read(fresh)
    block = next(one for one in openlitespeed.blocks(lines) if one.kind == "vhtemplate")
    assert openlitespeed.value(lines, block, "listeners") == "HTTP, HTTPS"


def test_the_default_8088_listener_is_left_out(fresh):
    """It serves OpenLiteSpeed's own example site; a real domain is never mapped to it."""
    path = fresh.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    sites.save(fresh, Site("example", "example.nl"))
    lines = sites.read(fresh)
    block = next(one for one in openlitespeed.blocks(lines) if one.kind == "vhtemplate")
    assert "Default" not in (openlitespeed.value(lines, block, "listeners") or "")


@pytest.fixture
def runnable(fresh, tmp_path, monkeypatch):
    """Enough to run a command: no root, no restarts. The playbook runs 'domainctl sync' as the last thing it
    does on a web server, so it must not fail on one that isn't finished being set up."""
    import json
    from dataclasses import asdict

    from serverctl import system

    settings = tmp_path / "config.json"
    settings.write_text(json.dumps({key: str(value) for key, value in asdict(fresh).items()}))
    monkeypatch.setenv("DOMAINCTL_CONFIG", str(settings))
    monkeypatch.setattr(system, "require_root", lambda tool: None)
    monkeypatch.setattr(openlitespeed, "restart", lambda root: None)
    return fresh


def test_sync_on_a_fresh_install_says_what_is_missing_and_does_not_fail(runnable):
    """Exit 1 here would stop the playbook on every new web server."""
    from typer.testing import CliRunner

    from domainctl import cli

    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "no HTTP listener" in result.output
    assert "Add one in WebAdmin" in result.output


def test_sync_adds_the_template_once_the_listeners_are_there(runnable):
    from typer.testing import CliRunner

    from domainctl import cli

    path = runnable.ols_root / "conf" / "httpd_config.conf"
    path.write_text(FRESH + LIVE_LISTENERS)
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "Changed the web sites" in result.output
    assert CliRunner().invoke(cli.app, ["sync"]).output.strip().endswith("Nothing changed.")
