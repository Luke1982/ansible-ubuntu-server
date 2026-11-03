"""Makes officectl and the shared serverctl package importable from the repository, without installing them."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT.parents[3] / "shared"

for directory in (ROOT, SHARED):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


import getpass  # noqa: E402

import pytest  # noqa: E402

from officectl.config import Config  # noqa: E402
from serverctl import openlitespeed  # noqa: E402

HTTPD_CONFIG = """serverName                web03

listener http {
  address                 *:80
  secure                  0
}

listener https {
  address                 *:443
  secure                  1
}
"""

# Collabora's own settings, shortened to the parts officectl touches, with its comments and its tabs.
COOLWSD = """<!-- -*- nxml-child-indent: 4; tab-width: 4; indent-tabs-mode: nil -*- -->
<config>
\t<server_name default="" desc="External hostname:port of the server running coolwsd." type="string"></server_name>
\t<ssl desc="SSL settings">
\t\t<enable type="bool" desc="Controls whether SSL encryption is enabled." default="true">true</enable>
\t\t<termination desc="Connection via proxy." type="bool" default="false">false</termination>
\t</ssl>
\t<net desc="Network settings">
\t\t<proto type="string" default="all" desc="Protocol to use IPv4, IPv6 or all for both">all</proto>
\t\t<listen type="string" default="any" desc="Listen address that coolwsd binds to.">any</listen>
\t</net>
\t<storage desc="Backend storage">
\t\t<filesystem allow="false"/>
\t\t<wopi allow="true" desc="Allow/deny wopi storage.">
\t\t\t<alias_groups desc="The WOPI hosts that may connect." mode="first">
\t\t\t\t<!--
\t\t\t<group>
\t\t\t\t\t<host allow="true">scheme://hostname:port</host>
\t\t\t</group>
\t\t\t-->
\t\t\t\t<!-- More "group"s possible here -->
\t\t\t</alias_groups>
\t\t</wopi>
\t</storage>
</config>
"""


@pytest.fixture
def ols_root(tmp_path):
    """An OpenLiteSpeed directory with a config and its listeners in it."""
    root = tmp_path / "lsws"
    (root / "conf" / "vhosts").mkdir(parents=True)
    (root / "conf" / "httpd_config.conf").write_text(HTTPD_CONFIG)
    (root / "lsphp85" / "bin").mkdir(parents=True)
    for program in ("lsphp", "php"):
        (root / "lsphp85" / "bin" / program).write_text("#!/bin/sh\n")
    (root / "lsphp85" / "etc" / "php" / "8.5" / "mods-available").mkdir(parents=True)
    return root


@pytest.fixture
def coolwsd_config(tmp_path):
    path = tmp_path / "coolwsd" / "coolwsd.xml"
    path.parent.mkdir(parents=True)
    path.write_text(COOLWSD)
    return path


@pytest.fixture
def config(tmp_path, ols_root, coolwsd_config):
    home_root = tmp_path / "home"
    home_root.mkdir()
    cron = tmp_path / "cron.d"
    cron.mkdir()
    # The tests don't run as root, so the config directory's user is whoever runs them: system.as_user() then
    # does nothing, instead of failing on a switch it isn't allowed to make.
    return Config(hostname="web03.example.nl", office_host="office.example.nl", office_root=tmp_path / "office",
                  home_root=home_root, ols_root=ols_root, ols_user=getpass.getuser(),
                  web_user=getpass.getuser(), web_group=getpass.getuser(), coolwsd_config=coolwsd_config,
                  cron_dir=cron, letsencrypt_dir=tmp_path / "letsencrypt", letsencrypt_email="info@example.nl")


@pytest.fixture(autouse=True)
def lock_file(tmp_path, monkeypatch):
    """Keeps every test off /run, which a normal user may not write.

    This has to patch the module the code actually calls: patching any other module object leaves the tests
    passing while they take the real lock.
    """
    monkeypatch.setattr(openlitespeed, "LOCK_FILE", tmp_path / "openlitespeed.lock")
    return tmp_path / "openlitespeed.lock"
