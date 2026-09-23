from pathlib import Path

import pytest

from domainctl.core import acl
from serverctl import system
from serverctl.errors import CtlError

# What getfacl prints for a web root that is set up the way domainctl wants it.
WEB_ROOT = """user::rwx
user:nobody:r-x
group::rwx
mask::rwx
other::r-x
default:user::rwx
default:user:nobody:r-x
default:group::rwx
default:mask::rwx
default:other::r-x
"""


@pytest.fixture
def getfacl(monkeypatch):
    """Answers getfacl with given text and records every setfacl that is run."""
    calls = []

    def fake_run(*args, stdin=None):
        calls.append(args)
        if args[0] == "getfacl":
            return fake_run.output
        return ""

    fake_run.output = ""
    fake_run.calls = calls
    monkeypatch.setattr(system, "run", fake_run)
    return fake_run


def test_getfacl_is_read_into_entries(getfacl):
    getfacl.output = WEB_ROOT
    assert acl.read(Path("/")) == {
        "user:": "rwx", "user:nobody": "r-x", "group:": "rwx", "mask:": "rwx", "other:": "r-x",
        "default:user:": "rwx", "default:user:nobody": "r-x", "default:group:": "rwx",
        "default:mask:": "rwx", "default:other:": "r-x",
    }


def test_the_effective_permissions_comment_is_not_part_of_an_entry(getfacl):
    getfacl.output = "user::rwx\nuser:nobody:rwx\t#effective:r-x\nmask::r-x\n"
    assert acl.read(Path("/"))["user:nobody"] == "rwx"


def test_a_getfacl_header_is_ignored(getfacl):
    getfacl.output = "# file: home/example\n# owner: example\n# group: example\nuser::rwx\n"
    assert acl.read(Path("/")) == {"user:": "rwx"}


def test_nothing_is_missing_when_the_entries_are_already_right(getfacl):
    getfacl.output = WEB_ROOT
    assert acl.missing(Path("/"), acl.readable_by("nobody")) == {}


def test_a_web_root_without_the_web_user_is_missing_two_entries(getfacl):
    getfacl.output = "user::rwx\ngroup::rwx\nmask::rwx\nother::r-x\n"
    missing = acl.missing(Path("/"), acl.readable_by("nobody"))
    assert missing["user:nobody"] == "r-x"
    assert missing["default:user:nobody"] == "r-x"


def test_apply_runs_setfacl_only_for_what_differs(getfacl, tmp_path):
    getfacl.output = "user::rwx\n"
    assert acl.apply(tmp_path, {"user:nobody": "--x"}) is True
    [setfacl] = [call for call in getfacl.calls if call[0] == "setfacl"]
    assert setfacl == ("setfacl", "--modify", "user:nobody:--x", str(tmp_path))


def test_apply_does_nothing_when_the_entries_are_already_there(getfacl, tmp_path):
    getfacl.output = "user::rwx\nuser:nobody:--x\n"
    assert acl.apply(tmp_path, {"user:nobody": "--x"}) is False
    assert not [call for call in getfacl.calls if call[0] == "setfacl"]


def test_a_path_that_is_not_there_is_reported_rather_than_created(getfacl, tmp_path):
    getfacl.output = ""
    with pytest.raises(CtlError, match="There is no"):
        acl.apply(tmp_path / "gone", {"user:nobody": "--x"})


def test_the_web_user_may_walk_through_but_not_list():
    assert acl.traversable_by("nobody") == {"user:nobody": "--x"}


def test_the_log_directory_is_writable_and_its_files_are_not_executable():
    entries = acl.writable_by("nobody")
    assert entries["user:nobody"] == "rwx"
    assert entries["default:user:nobody"] == "rw-"
    assert entries["other:"] == "---"


def test_the_web_root_sets_the_mask_so_the_group_keeps_its_permissions():
    """Adding a named entry makes the kernel recalculate the mask, which can quietly drop group permissions."""
    assert acl.readable_by("nobody")["mask:"] == "rwx"
    assert acl.readable_by("nobody")["group:"] == "rwx"
