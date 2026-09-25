import os
import time

import pytest

from mailctl.core import spamlearn, system
from mailctl.core.errors import MailctlError

ADDRESS = "info@example.nl"


@pytest.fixture
def maildir(config):
    """The account's Maildir, with an inbox and a Junk folder."""
    root = config.vmail_root / "example.nl" / "info" / "Maildir"
    for part in ("cur", "new", ".Junk/cur", ".Junk/new"):
        (root / part).mkdir(parents=True)
    return root


def message(directory, name, content="Subject: hello\n\nbody\n"):
    path = directory / name
    path.write_text(content)
    return path


def test_the_folders_are_every_accounts_junk_and_inbox(config, maildir):
    found = spamlearn.folders(config, [ADDRESS])

    assert [(one.path, one.spam) for one in found] == [(maildir / ".Junk", True), (maildir, False)]


def test_an_account_without_mail_on_this_server_has_no_folders(config):
    assert spamlearn.folders(config, ["gone@example.nl"]) == []


def test_junk_teaches_from_read_and_unread_mail_and_the_inbox_only_from_read(config, maildir):
    junk, inbox = spamlearn.folders(config, [ADDRESS])
    message(maildir / ".Junk" / "cur", "1:2,S")
    message(maildir / ".Junk" / "new", "2")
    message(maildir / "cur", "3:2,S")
    message(maildir / "new", "4")  # nobody has looked at it yet: it may be spam that isn't filed

    assert len(spamlearn.messages(junk)) == 2
    assert [path.name for path in spamlearn.messages(inbox)] == ["3:2,S"]


def test_the_messages_from_before_the_last_run_are_left_out(config, maildir):
    junk, _ = spamlearn.folders(config, [ADDRESS])
    message(maildir / ".Junk" / "cur", "old:2,S")

    assert spamlearn.messages(junk, since=time.time() + 60) == []
    assert len(spamlearn.messages(junk, since=0.0)) == 1


def test_a_message_written_long_ago_but_filed_just_now_counts(config, maildir):
    """Dragging a message into Junk keeps the time it was written, so the time it was last changed decides."""
    junk, _ = spamlearn.folders(config, [ADDRESS])
    moved = message(maildir / ".Junk" / "cur", "moved:2,S")
    long_ago = time.time() - 30 * 86400
    os.utime(moved, (long_ago, long_ago))

    assert [path.name for path in spamlearn.messages(junk, time.time() - 3600)] == ["moved:2,S"]


def test_learn_hands_the_messages_to_sa_learn_and_counts_what_it_learned(fake_command, tmp_path):
    sa_learn = fake_command("sa-learn", 'echo "Learned tokens from 2 message(s) (3 message(s) examined)"')
    paths = [tmp_path / f"{number}" for number in range(3)]

    learned, problems = spamlearn.learn(paths, spam=True)

    assert (learned, problems) == (2, [])
    assert sa_learn.calls[0][:3] == ["--no-sync", "--spam", str(paths[0])]


def test_learn_asks_for_ham_when_the_mail_was_kept(fake_command, tmp_path):
    sa_learn = fake_command("sa-learn", 'echo "Learned tokens from 0 message(s) (1 message(s) examined)"')

    learned, problems = spamlearn.learn([tmp_path / "one"], spam=False)

    assert (learned, problems) == (0, [])
    assert "--ham" in sa_learn.calls[0]


def test_learn_goes_on_when_one_batch_fails(fake_command, tmp_path, monkeypatch):
    monkeypatch.setattr(spamlearn, "BATCH", 1)
    fake_command("sa-learn", 'case "$3" in *bad) echo "sa-learn: unreadable" >&2; exit 1;; esac;'
                             ' echo "Learned tokens from 1 message(s) (1 message(s) examined)"')

    learned, problems = spamlearn.learn([tmp_path / "good", tmp_path / "bad"], spam=True)

    assert learned == 1
    assert len(problems) == 1 and "unreadable" in problems[0]


def test_learn_without_messages_runs_nothing(fake_command):
    sa_learn = fake_command("sa-learn")

    assert spamlearn.learn([], spam=True) == (0, [])
    assert sa_learn.calls == []


def test_sync_writes_what_was_learned(fake_command):
    sa_learn = fake_command("sa-learn")

    spamlearn.sync()

    assert sa_learn.calls == [["--sync"]]


def test_sa_learn_that_isnt_installed_is_reported_not_raised(tmp_path, monkeypatch):
    """A nightly run says what went wrong and ends; it doesn't stop on the first batch."""
    monkeypatch.setenv("PATH", str(tmp_path))

    learned, problems = spamlearn.learn([tmp_path / "one"], spam=True)

    assert learned == 0
    assert problems == ["sa-learn isn't installed."]


def test_the_time_of_the_last_run_is_kept_between_runs(tmp_path):
    state = tmp_path / "state" / "spam-learn.json"

    assert spamlearn.last_run(state) == 0.0

    spamlearn.remember(1700000000.5, state)

    assert spamlearn.last_run(state) == 1700000000.5


def test_a_state_file_that_cant_be_read_means_everything_is_learned_again(tmp_path):
    state = tmp_path / "spam-learn.json"
    state.write_text("{not json")

    assert spamlearn.last_run(state) == 0.0


def test_a_folder_that_cant_be_read_says_which_one(config, maildir, monkeypatch):
    junk, _ = spamlearn.folders(config, [ADDRESS])

    def refuse(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(spamlearn.os, "scandir", refuse)

    with pytest.raises(MailctlError, match="Can't read"):
        spamlearn.messages(junk)


def test_the_test_suite_does_not_reach_the_real_sa_learn():
    """system.run is what every step goes through, so a fake on PATH is all a test needs."""
    assert spamlearn.system is system
