"""Makes the shared serverctl package importable from the repository, without installing it."""

import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))


import pytest  # noqa: E402

from migrate import migrate  # noqa: E402


@pytest.fixture(autouse=True)
def no_web_servers_of_this_machine(monkeypatch, tmp_path):
    """No test reads the nginx or OpenLiteSpeed configuration of the machine it runs on: a developer's own
    sites would turn up in what a test found, and the tests would say something else on the next machine.
    A test that is about them points these at a tree of its own."""
    monkeypatch.setattr(migrate, "NGINX_SITES", str(tmp_path / "no-nginx"))
    monkeypatch.setattr(migrate, "LSWS_ROOT", str(tmp_path / "no-lsws"))


@pytest.fixture(autouse=True)
def no_mailboxes_of_this_machine(monkeypatch, tmp_path):
    """No test measures the mailboxes of the machine it runs on, and no test sees what another one measured:
    the screen remembers a measurement for a minute, which inside one run of the tests is forever."""
    monkeypatch.setattr(migrate, "VMAIL", str(tmp_path / "no-vmail"))
    # And no test writes its measurements where the next run of the tool would read them, or reads what the
    # machine's own last run left there.
    monkeypatch.setattr(migrate, "SIZES_FILE", str(tmp_path / "sizes.json"))
    migrate._SIZES_SEEN.clear()
    migrate._DNS_SEEN.clear()
    # A note is printed once in a run; inside one run of the tests that would be once in the whole suite.
    migrate._SAID.clear()
