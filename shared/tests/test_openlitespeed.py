"""The parts of the OpenLiteSpeed module the web tools rely on. mailctl's own suite covers the rest."""

import fcntl
import os
import re
import stat
from pathlib import Path

import pytest

from serverctl import openlitespeed
from serverctl.errors import CtlError
from serverctl.openlitespeed import Member, Template

CONFIG = """serverName                web02

listener http {
  address                 *:80
  secure                  0
  map                     webmail.example.nl webmail.example.nl
}

listener https {
  address                 *:443
  secure                  1
}

virtualHost Example {
  vhRoot                  /var/www/example/
  note                    Set up by hand
}
"""


@pytest.fixture
def root(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "httpd_config.conf").write_text(CONFIG)
    return tmp_path


def test_listeners_are_found_by_port_and_secure_flag():
    assert openlitespeed.listeners(CONFIG.splitlines()) == (["http"], ["https"])


def test_a_member_is_added_to_a_template_that_does_not_exist_yet():
    template = Template("webhosting", "conf/templates/webhosting.conf", ("http", "https"), "Managed by domainctl")
    lines = openlitespeed.with_member(CONFIG.splitlines(), Member("example", "example.nl", ("www.example.nl",)),
                                      template)
    assert openlitespeed.members(lines, "webhosting") == ["example"]
    [found] = openlitespeed.member_details(lines, "webhosting")
    assert (found.domain, found.aliases) == ("example.nl", ("www.example.nl",))
    # The hand-made virtual host and the listeners are untouched.
    assert "virtualHost Example {" in lines
    assert openlitespeed.listeners(lines) == (["http"], ["https"])


def test_with_template_brings_the_listeners_up_to_date_and_keeps_the_members():
    template = Template("webhosting", "conf/templates/webhosting.conf", ("http",), "Managed by domainctl")
    lines = openlitespeed.with_member(CONFIG.splitlines(), Member("example", "example.nl"), template)
    both = openlitespeed.with_template(lines, Template("webhosting", "conf/templates/webhosting.conf",
                                                       ("http", "https"), "Managed by domainctl"))
    assert openlitespeed.members(both, "webhosting") == ["example"]
    block = next(one for one in openlitespeed.blocks(both) if one.kind == "vhtemplate")
    assert openlitespeed.value(both, block, "listeners") == "http, https"


def test_a_member_is_removed_without_touching_the_others():
    template = Template("webhosting", "conf/templates/webhosting.conf", ("http", "https"), "Managed by domainctl")
    lines = openlitespeed.with_member(CONFIG.splitlines(), Member("one", "one.nl"), template)
    lines = openlitespeed.with_member(lines, Member("two", "two.nl"), template)
    left = openlitespeed.without_member(lines, "one", "webhosting")
    assert openlitespeed.members(left, "webhosting") == ["two"]


def test_writing_keeps_the_owner_and_mode_and_a_backup_named_after_the_tool(root):
    path = openlitespeed.config_file(root)
    path.chmod(0o640)
    lines = openlitespeed.read(root)
    openlitespeed.write(root, [*lines, "# added"], "domainctl")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text().endswith("# added\n")
    assert path.with_name("httpd_config.conf.domainctl.bak").read_text() == CONFIG


def test_a_symbolic_link_in_place_of_the_config_is_refused(root, tmp_path):
    path = openlitespeed.config_file(root)
    elsewhere = tmp_path / "elsewhere.conf"
    elsewhere.write_text(CONFIG)
    path.unlink()
    os.symlink(elsewhere, path)
    with pytest.raises(CtlError, match="symbolic link"):
        openlitespeed.read(root)


def test_an_unbalanced_config_is_reported_rather_than_half_read():
    with pytest.raises(CtlError, match="can't be read"):
        openlitespeed.blocks(["listener http {", "  address *:80"])


def test_the_lock_is_re_entrant_so_a_locking_function_may_call_another(tmp_path):
    lock = tmp_path / "lock"
    with openlitespeed.locked(lock, timeout=1):
        with openlitespeed.locked(lock, timeout=1):
            assert openlitespeed._held == 2
        assert openlitespeed._held == 1
    assert openlitespeed._held == 0


def test_a_lock_someone_else_holds_is_waited_for_and_then_reported(tmp_path):
    """Two tools change the config by reading it and renaming a new file into place, so the second must wait."""
    lock = tmp_path / "lock"
    other = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(CtlError, match="changing OpenLiteSpeed's configuration"):
            with openlitespeed.locked(lock, timeout=0.3):
                pass
    finally:
        fcntl.flock(other, fcntl.LOCK_UN)
        os.close(other)
    assert openlitespeed._held == 0


def test_the_lock_is_free_again_when_the_block_raises(tmp_path):
    lock = tmp_path / "lock"
    with pytest.raises(ValueError):
        with openlitespeed.locked(lock, timeout=1):
            raise ValueError("something went wrong")
    assert openlitespeed._held == 0
    with openlitespeed.locked(lock, timeout=0.3):  # free, so this doesn't time out
        pass


def test_the_lock_is_taken_once_and_given_back_once(tmp_path):
    """A second open file description must be able to take it straight after the block ends."""
    lock = tmp_path / "lock"
    with openlitespeed.locked(lock, timeout=1):
        pass
    other = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if it was never released
        fcntl.flock(other, fcntl.LOCK_UN)
    finally:
        os.close(other)


MAILCTL_COPY = Path(__file__).resolve().parents[2] / "roles/mail/files/mailctl/mailctl/core/openlitespeed.py"


def test_mailctls_copy_agrees_on_the_lock_file():
    """While there are two copies of this module, changing LOCK_FILE in one alone is the worst kind of mistake:
    the two tools then take different locks, shut nobody out, and every test still passes.

    This goes away with the second copy, when mailctl imports this module instead of carrying its own.
    """
    if not MAILCTL_COPY.exists():
        pytest.skip("mailctl isn't in this checkout")
    source = MAILCTL_COPY.read_text()
    found = re.search(r'^LOCK_FILE = Path\("([^"]+)"\)', source, re.MULTILINE)
    assert found, f"{MAILCTL_COPY} has no LOCK_FILE, so it can no longer be compared with this one"
    assert found.group(1) == str(openlitespeed.LOCK_FILE), (
        "mailctl and domainctl would take different locks, which protects neither"
    )
