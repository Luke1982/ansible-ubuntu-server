import io
import tarfile

import pytest

from mailctl.core import sieve
from mailctl.core.errors import MailctlError

ROUNDCUBE = 'require ["fileinto"];\nif header :contains "subject" "invoice" { fileinto "Invoices"; }\n'
VACATION = 'require ["vacation"];\nvacation :days 1 "Away";\n'


def archive(tmp_path, files, links=(), name="sieve.tar.gz"):
    """A tar archive like the sieve directory of an account on another server."""
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as tar:
        for member_name, content in files.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(content.encode())
            tar.addfile(info, io.BytesIO(content.encode()))
        for member_name, target in links:
            info = tarfile.TarInfo(member_name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return path


def test_read_archive_takes_the_scripts_and_the_active_one_from_the_link(tmp_path):
    path = archive(tmp_path, {"info/sieve/roundcube.sieve": ROUNDCUBE, "info/sieve/vacation.sieve": VACATION},
                   links=[("info/.dovecot.sieve", "sieve/roundcube.sieve")])

    scripts, skipped = sieve.read_archive(path)

    assert skipped == []
    assert scripts == [sieve.Script("roundcube", ROUNDCUBE, True), sieve.Script("vacation", VACATION, False)]


def test_read_archive_finds_the_active_script_by_its_content(tmp_path):
    """An archive of copies, not links, holds .dovecot.sieve as a file with the active script's content."""
    path = archive(tmp_path, {"sieve/roundcube.sieve": ROUNDCUBE, "sieve/vacation.sieve": VACATION,
                              ".dovecot.sieve": VACATION})

    scripts, _ = sieve.read_archive(path)

    assert [(script.name, script.active) for script in scripts] == [("roundcube", False), ("vacation", True)]


def test_read_archive_makes_a_single_script_the_active_one(tmp_path):
    scripts, _ = sieve.read_archive(archive(tmp_path, {"sieve/roundcube.sieve": ROUNDCUBE}))

    assert scripts == [sieve.Script("roundcube", ROUNDCUBE, True)]


def test_read_archive_leaves_none_active_when_it_cant_tell(tmp_path):
    path = archive(tmp_path, {"sieve/one.sieve": ROUNDCUBE, "sieve/two.sieve": VACATION})

    assert [script.active for script in sieve.read_archive(path)[0]] == [False, False]


def test_read_archive_skips_what_isnt_a_filter(tmp_path):
    path = archive(tmp_path, {"sieve/roundcube.sieve": ROUNDCUBE, "sieve/roundcube.svbin": "compiled",
                              "sieve/tmp/.svtmp.sieve": "temporary", "sieve/big.sieve": "x" * (sieve.MAX_SCRIPT + 1)})

    scripts, skipped = sieve.read_archive(path)

    assert [script.name for script in scripts] == ["roundcube"]
    assert skipped == [
        "sieve/tmp/.svtmp.sieve: not a filter name",
        f"sieve/big.sieve: bigger than {sieve.MAX_SCRIPT // 1024} KB",
    ]


def test_read_archive_explains_a_file_that_isnt_a_tar(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("no tar here\n")

    with pytest.raises(MailctlError, match="Can't read"):
        sieve.read_archive(path)

    with pytest.raises(MailctlError, match="There is no file"):
        sieve.read_archive(tmp_path / "missing.tar")


def test_read_archive_refuses_an_archive_full_of_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(sieve, "MAX_SCRIPTS", 2)
    path = archive(tmp_path, {f"sieve/filter{number}.sieve": ROUNDCUBE for number in range(3)})

    with pytest.raises(MailctlError, match="more than 2 filters"):
        sieve.read_archive(path)
