"""That the tests themselves never take the real lock.

The autouse fixture patches LOCK_FILE on the module the code calls. Patching it on some other module object
would leave every other test passing and quietly using /run/serverctl-openlitespeed.lock instead, which works
when the suite runs as root and fails for anyone else. This is the guard against that.
"""

from pathlib import Path

from serverctl import openlitespeed

REAL = Path("/run/serverctl-openlitespeed.lock")


def test_locking_uses_the_patched_path_and_not_the_real_one(lock_file, tmp_path):
    was_there = REAL.exists()
    with openlitespeed.locked():
        assert lock_file.exists(), "locked() didn't use the patched LOCK_FILE, so the tests take the real lock"
        assert lock_file.parent == tmp_path
    assert REAL.exists() == was_there, "the tests created the real lock file"


def test_the_fixture_points_somewhere_a_normal_user_may_write(lock_file):
    assert openlitespeed.LOCK_FILE == lock_file
    assert lock_file != REAL
