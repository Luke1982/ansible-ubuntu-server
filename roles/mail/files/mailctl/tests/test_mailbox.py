import os
import shutil
from contextlib import contextmanager

import pytest

from mailctl.core import mailbox
from mailctl.core.errors import MailctlError
from mailctl.core.mailbox import Folder, SieveScript

ADDRESS = "info@example.nl"


@pytest.fixture
def maildir(config):
    config.vmail_root.mkdir()
    mailbox.create_maildir(config, ADDRESS)
    return config.vmail_root / "example.nl" / "info" / "Maildir"


def test_home_dir_is_the_local_part_under_the_domain(config):
    assert mailbox.home_dir(config, ADDRESS) == config.vmail_root / "example.nl" / "info"
    assert mailbox.domain_dir(config, "example.nl") == config.vmail_root / "example.nl"


def test_create_maildir_makes_the_folders_and_their_aliases(maildir):
    assert sorted(path.name for path in maildir.iterdir() if not path.is_symlink()) == [".Junk", ".Sent", ".Trash"]
    assert os.readlink(maildir / ".Verzonden items") == ".Sent"
    assert os.readlink(maildir / ".Sent Items") == ".Sent"
    assert os.readlink(maildir / ".Deleted Messages") == ".Trash"
    assert os.readlink(maildir / ".Ongewenste e-mail") == ".Junk"
    assert maildir.stat().st_mode & 0o777 == 0o700
    assert (maildir / ".Sent").stat().st_uid == os.getuid()


def test_create_maildir_keeps_existing_mail_and_folders(config, maildir):
    (maildir / ".Sent" / "kept").write_text("mail")
    (maildir / ".Sent Items").unlink()
    (maildir / ".Sent Items").mkdir()

    mailbox.create_maildir(config, ADDRESS)

    assert (maildir / ".Sent" / "kept").read_text() == "mail"
    assert not (maildir / ".Sent Items").is_symlink()


@pytest.fixture
def linked_domain(config, tmp_path):
    """The vmail user replaced the domain's folder with a link to another folder."""
    config.vmail_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "info").mkdir(parents=True)
    (config.vmail_root / "example.nl").symlink_to(elsewhere)
    return elsewhere


def test_create_maildir_refuses_to_work_through_a_symbolic_link(config, linked_domain):
    with pytest.raises(MailctlError, match="example.nl is a symbolic link"):
        mailbox.create_maildir(config, ADDRESS)

    assert list((linked_domain / "info").iterdir()) == []


def test_delete_mail_removes_the_folder(config, maildir):
    mailbox.delete_mail(config, maildir.parent)

    assert not maildir.parent.exists()


def test_delete_mail_refuses_to_work_through_a_symbolic_link(config, linked_domain):
    with pytest.raises(MailctlError, match="example.nl is a symbolic link"):
        mailbox.delete_mail(config, mailbox.home_dir(config, ADDRESS))

    assert (linked_domain / "info").is_dir()


def test_create_maildir_refuses_a_maildir_that_is_a_symbolic_link(config, maildir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    maildir.rename(tmp_path / "old")
    maildir.symlink_to(elsewhere)

    with pytest.raises(MailctlError, match="Maildir is a symbolic link"):
        mailbox.create_maildir(config, ADDRESS)

    assert list(elsewhere.iterdir()) == []


def test_delete_mail_refuses_a_mail_folder_that_is_a_symbolic_link(config, tmp_path):
    config.vmail_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config.vmail_root / "example.nl").mkdir()
    (config.vmail_root / "example.nl" / "info").symlink_to(elsewhere)

    with pytest.raises(MailctlError, match="info is a symbolic link"):
        mailbox.delete_mail(config, mailbox.home_dir(config, ADDRESS))

    assert elsewhere.is_dir()


@pytest.fixture
def acting_users(config, monkeypatch):
    """For every folder created, link made and folder deleted in the vmail root: the user mailctl was acting as."""
    config.vmail_root.mkdir()
    acting_as = None
    users = []

    @contextmanager
    def as_user(name):
        nonlocal acting_as
        acting_as = name
        try:
            yield
        finally:
            acting_as = None

    def recording(change):
        def recorded(*args, **kwargs):
            users.append(acting_as)
            return change(*args, **kwargs)
        return recorded

    monkeypatch.setattr(mailbox.system, "as_user", as_user)
    for module, name in ((os, "mkdir"), (os, "symlink"), (shutil, "rmtree")):
        monkeypatch.setattr(module, name, recording(getattr(module, name)))
    return users


def test_mail_folders_are_created_and_deleted_as_the_vmail_user(config, acting_users):
    mailbox.create_maildir(config, ADDRESS)
    mailbox.delete_mail(config, mailbox.home_dir(config, ADDRESS))

    assert len(acting_users) > 2
    assert set(acting_users) == {config.vmail_user}


def test_create_maildir_names_the_folder_vmail_may_not_change(config):
    config.vmail_root.mkdir()
    (config.vmail_root / "example.nl").mkdir(mode=0o500)

    with pytest.raises(MailctlError) as raised:
        mailbox.create_maildir(config, ADDRESS)

    assert raised.value.message == f"Can't create {mailbox.home_dir(config, ADDRESS)}: Permission denied."
    assert raised.value.hint == f"mailctl changes mail folders as {config.vmail_user}, so they must belong to {config.vmail_user}."


def test_delete_mail_names_the_file_vmail_may_not_delete(config, maildir):
    message = maildir / ".Sent" / "message"
    message.write_text("mail")
    message.parent.chmod(0o500)

    with pytest.raises(MailctlError) as raised:
        mailbox.delete_mail(config, maildir.parent)

    assert raised.value.message == f"Can't delete {message}: Permission denied."
    assert "must belong to" in raised.value.hint


def test_delete_mail_skips_a_message_that_disappears_meanwhile(config, maildir, monkeypatch):
    for number in range(3):
        (maildir / ".Sent" / f"message{number}").write_text("mail")
    unlink = os.unlink

    def expunged_by_dovecot(path, *args, **kwargs):
        if str(path).startswith("message1"):
            unlink(path, *args, **kwargs)
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", expunged_by_dovecot)

    mailbox.delete_mail(config, maildir.parent)

    assert not maildir.parent.exists()


def test_delete_mail_accepts_mail_that_isnt_there(config):
    config.vmail_root.mkdir()

    mailbox.delete_mail(config, mailbox.home_dir(config, ADDRESS))


def test_delete_mail_refuses_folders_outside_the_vmail_root(config, tmp_path):
    with pytest.raises(MailctlError, match="isn't in"):
        mailbox.delete_mail(config, tmp_path)


def test_parse_folders_reads_doveadm_output_and_marks_aliased_folders(maildir):
    output = "mailbox\tmessages\tvsize\nINBOX\t12\t34567\nSent\t3\t4000\nSent Items\t3\t4000\nProjects.2026\t1\t10\n"

    assert mailbox.parse_folders(output, maildir) == [
        Folder("INBOX", messages=12, size=34567),
        Folder("Sent", messages=3, size=4000),
        Folder("Sent Items", messages=3, size=4000, alias_of="Sent"),
        Folder("Projects.2026", messages=1, size=10),
    ]


def test_parse_folders_without_a_header_line(maildir):
    assert mailbox.parse_folders("INBOX\t2\t300\n", maildir) == [Folder("INBOX", messages=2, size=300)]


def test_folders_asks_doveadm_about_the_account(config, maildir, fake_command):
    doveadm = fake_command("doveadm", r'printf "mailbox\tmessages\tvsize\nINBOX\t1\t2\n"')

    assert mailbox.folders(config, ADDRESS) == [Folder("INBOX", messages=1, size=2)]
    assert doveadm.calls == [["-f", "tab", "mailbox", "status", "-u", "info@example.nl", "messages vsize", "*"]]


def test_parse_sieve_list_marks_the_active_script():
    assert mailbox.parse_sieve_list("roundcube ACTIVE\nold filters \n\n") == [("roundcube", True), ("old filters", False)]


def test_sieve_scripts_reads_every_script_of_the_account(fake_command):
    fake_command("doveadm", """if [ "$2" = list ]; then echo "roundcube ACTIVE"; else echo 'require "fileinto";'; fi""")

    assert mailbox.sieve_scripts(ADDRESS) == [SieveScript("roundcube", active=True, content='require "fileinto";\n')]


def test_server_scripts_reads_the_scripts_that_run_after_the_accounts_own(config):
    config.sieve_after.mkdir()
    (config.sieve_after / "spam-to-folder.sieve").write_text("stop;")
    (config.sieve_after / "spam-to-folder.svbin").write_bytes(b"\0")

    assert mailbox.server_scripts(config) == [SieveScript("spam-to-folder", active=True, content="stop;")]


def test_server_scripts_without_the_directory(config):
    assert mailbox.server_scripts(config) == []


def test_disk_usage_counts_files_but_not_symlinked_folders_twice(maildir):
    (maildir / "cur").mkdir()
    (maildir / "cur" / "1").write_bytes(b"x" * 100)
    (maildir / ".Sent" / "2").write_bytes(b"x" * 50)

    assert mailbox.disk_usage(maildir) == 150


def test_disk_usage_of_a_missing_directory_is_zero(tmp_path):
    assert mailbox.disk_usage(tmp_path / "missing") == 0


def test_kick_logs_out_sessions_and_ignores_failures(fake_command):
    doveadm = fake_command("doveadm", "exit 1")

    mailbox.kick(ADDRESS)

    assert doveadm.calls == [["kick", "info@example.nl"]]
