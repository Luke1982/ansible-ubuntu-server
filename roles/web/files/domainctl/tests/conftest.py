"""Makes domainctl and the shared serverctl package importable from the repository, without installing them."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT.parents[3] / "shared"

for directory in (ROOT, SHARED):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


import getpass  # noqa: E402

import pytest  # noqa: E402

from domainctl.config import Config  # noqa: E402
from serverctl import openlitespeed  # noqa: E402

TEMPLATE = """# Written by Ansible.
vhRoot                    /home/$VH_NAME/

virtualHostConfig  {
  docRoot                 $VH_ROOT/public_html/www

  rewrite  {
    enable                1
    autoLoadHtaccess      1
# BEGIN https-redirect
RewriteCond %{HTTPS} off
RewriteRule .* https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]
# END https-redirect
  }
}
"""

HTTPD_CONFIG = """serverName                web02

listener http {
  address                 *:80
  secure                  0
}

listener https {
  address                 *:443
  secure                  1
}
"""


@pytest.fixture
def ols_root(tmp_path):
    """An OpenLiteSpeed directory with a config and the webhosting template in it."""
    root = tmp_path / "lsws"
    (root / "conf" / "templates").mkdir(parents=True)
    (root / "conf" / "httpd_config.conf").write_text(HTTPD_CONFIG)
    (root / "conf" / "templates" / "webhosting.conf").write_text(TEMPLATE)
    return root


@pytest.fixture
def config(tmp_path, ols_root):
    home_root = tmp_path / "home"
    home_root.mkdir()
    # The tests don't run as root, so the config directory's user is whoever runs them: system.as_user() then
    # does nothing, instead of failing on a switch it isn't allowed to make.
    return Config(hostname="web02.example.nl", home_root=home_root, ols_root=ols_root,
                  ols_user=getpass.getuser(), letsencrypt_dir=tmp_path / "letsencrypt")


@pytest.fixture(autouse=True)
def lock_file(tmp_path, monkeypatch):
    """Keeps every test off /run, which a normal user may not write.

    This has to patch the module the code actually calls. Patching any other module object leaves the tests
    passing while they take the real lock; tests/test_locking.py is the guard against that.
    """
    monkeypatch.setattr(openlitespeed, "LOCK_FILE", tmp_path / "openlitespeed.lock")
    return tmp_path / "openlitespeed.lock"
