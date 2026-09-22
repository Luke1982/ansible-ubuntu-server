"""The tests must lock in their own directory, not in /run.

The fixture patching LOCK_FILE has to patch the module the code actually calls. Patch another one and every test
still passes: they would simply be taking the real lock, which works as root and fails for a normal user. That
failure protects nothing and looks fine, so it is checked here.
"""

from pathlib import Path

from mailctl.core import openlitespeed

# domainctl (roles/web, from shared/serverctl) waits for this same file. The two only shut each other out while
# both use it, so it can't be changed in one of them alone.
SHARED_LOCK = Path("/run/serverctl-openlitespeed.lock")


def test_the_lock_is_the_file_the_other_tools_take():
    source = Path(openlitespeed.__file__).read_text()

    assert f'LOCK_FILE = Path("{SHARED_LOCK}")' in source


def test_the_tests_take_their_own_lock_and_not_the_real_one(tmp_path):
    existed = SHARED_LOCK.exists()

    assert openlitespeed.LOCK_FILE.parent == tmp_path, "the lock_file fixture didn't patch the module under test"
    with openlitespeed.locked():
        assert openlitespeed.LOCK_FILE.exists(), \
            "locked() didn't use the patched LOCK_FILE, so the tests take the real lock"
    assert SHARED_LOCK.exists() == existed
