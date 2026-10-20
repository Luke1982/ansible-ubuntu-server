import io
import json
import os
import shutil
import stat

import pytest

from migrate import migrate


class FakeProcess(object):
    def __init__(self, code, out, err, sent):
        self.returncode, self._sent = code, sent
        self._out = out if isinstance(out, bytes) else out.encode()
        self._err = err if isinstance(err, bytes) else err.encode()

    def communicate(self, text=None, timeout=None):
        self._sent.append(text)
        return self._out, self._err


@pytest.fixture
def fake_ssh(monkeypatch):
    """Stands in for ssh. .replies is a list of (code, stdout, stderr) or a function of the request; .commands and
    .sent record what was run and what went in on stdin."""
    class Fake(object):
        pass

    fake = Fake()
    fake.commands, fake.sent, fake.replies = [], [], []

    def popen(command, **kwargs):
        pipe = migrate.subprocess.PIPE
        assert (kwargs.get("stdin"), kwargs.get("stdout"), kwargs.get("stderr")) == (pipe, pipe, pipe)
        assert kwargs["env"]["LC_ALL"] == "C"
        fake.commands.append(command)
        reply = fake.replies.pop(0) if isinstance(fake.replies, list) else fake.replies
        if callable(reply):
            process = FakeProcess(0, "", "", [])

            def communicate(text=None, timeout=None):
                fake.sent.append(text)
                code, out, err = reply(json.loads(text.decode()))
                return out.encode(), err.encode()
            process.communicate = communicate
            process.returncode = 0
            return process
        return FakeProcess(*(reply + (fake.sent,)))
    monkeypatch.setattr(migrate.subprocess, "Popen", popen)
    return fake


def _connection(tmp_path):
    return migrate.Connection("new.example.nl", key=str(tmp_path / "migrate_key"))


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_key_is_made_once_and_kept(tmp_path):
    first = migrate.own_key(str(tmp_path / "ssh"))
    private = tmp_path / "ssh" / "migrate_key"
    assert first.startswith("ssh-ed25519 ")
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    before = private.read_bytes()
    assert migrate.own_key(str(tmp_path / "ssh")) == first
    assert private.read_bytes() == before


def test_a_refused_connection_says_to_paste_the_key(fake_ssh, tmp_path):
    (tmp_path / "migrate_key.pub").write_text("ssh-ed25519 AAAAkey migrate\n")
    (tmp_path / "migrate_key").write_text("private")
    fake_ssh.replies = [(255, "", "ansible@new.example.nl: Permission denied (publickey).\n")]
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.check(_connection(tmp_path))
    text = stopped.value.message
    assert "/home/ansible/.ssh/authorized_keys" in text and "ssh-ed25519 AAAAkey migrate" in text
    assert "BatchMode=yes" in fake_ssh.commands[0]
    assert fake_ssh.commands[0][-3:] == ["sudo", "-n", "migratectl"]


def test_the_handshake_refuses_a_new_server_that_speaks_another_version(fake_ssh, tmp_path):
    fake_ssh.replies = [(0, json.dumps({"ok": True, "version": 2, "server": "x", "tools": {}}) + "\n", "")]
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.check(_connection(tmp_path))
    assert "reports 2, this file is 1" in stopped.value.message
    assert "same release" in stopped.value.message
    fake_ssh.replies = [(0, json.dumps({"ok": True, "version": 1, "server": "x", "tools": {}}) + "\n", "")]
    assert migrate.check(_connection(tmp_path))["server"] == "x"


@pytest.mark.parametrize("reply, words", [
    ((255, "", "ssh: Could not resolve hostname new.example.nl"), "can't reach"),
    ((1, "", "sudo: migratectl: command not found"), "isn't installed"),
    ((1, "", "sudo: a password is required"), "without a password"),
    ((0, "", ""), "nothing"),
    ((0, "not json\n", ""), "can't read"),
    ((0, "[1]\n", ""), "can't read"),
    ((255, "", "Host key verification failed."), "identity"),
])
def test_every_way_to_fail_is_a_plain_message(fake_ssh, tmp_path, reply, words):
    fake_ssh.replies = [reply]
    with pytest.raises(migrate.Stopped) as stopped:
        _connection(tmp_path).ask({"request": "hello"})
    assert words in stopped.value.message


def test_an_old_ssh_gets_one_retry_and_one_note(fake_ssh, tmp_path, capsys):
    fake_ssh.replies = [(255, "", 'command-line: line 0: unsupported option "accept-new".'),
                        (0, '{"ok": true}\n', ""), (0, '{"ok": true}\n', "")]
    connection = _connection(tmp_path)
    assert connection.ask({"request": "hello"}) == {"ok": True}
    assert "StrictHostKeyChecking=no" in fake_ssh.commands[1]
    connection.ask({"request": "hello"})
    assert "StrictHostKeyChecking=no" in fake_ssh.commands[2]  # it remembers: one process, not two
    assert capsys.readouterr().out.count("too old") == 1


def _with_terminal(monkeypatch):
    """A terminal to type on: open("/dev/tty") succeeds, as it does for a person on the old server."""
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/dev/tty":
            return io.StringIO()
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", fake_open)

def test_an_account_whose_password_is_needed_is_asked_for_and_enter_skips_it(fake_ssh, monkeypatch, tmp_path, capsys):
    item = {"kind": "mail_domain", "name": "a.nl", "data": {
        "addresses": [{"address": "x@a.nl", "password_hash": "{OLD}1"}, {"address": "y@a.nl", "password_hash": "{OLD}2"},
                      {"address": "z@a.nl", "password_hash": "{SHA512-CRYPT}ok"}],
        "sieve": [{"address": "y@a.nl", "name": "f", "active": True, "content": ""}],
        "spam": [{"target": "y@a.nl", "setting": "required_score", "value": "5"},
                 {"target": "x@a.nl", "setting": "required_score", "value": "6"}]}}
    seen = []

    def server(request):
        seen.append(copy_of(request))
        accounts = request["item"]["data"]["addresses"]
        needs = [a["address"] for a in accounts if "password_hash" in a and a["password_hash"].startswith("{OLD}")]
        if needs:
            return 0, json.dumps({"ok": False, "needs_password": needs, "error": "needs"}) + "\n", ""
        return 0, '{"ok": true}\n', ""

    def copy_of(value):
        return json.loads(json.dumps(value))

    fake_ssh.replies = server
    asked = []
    _with_terminal(monkeypatch)  # a person sitting at the old server, so the password really is asked for
    monkeypatch.setattr(migrate.getpass, "getpass", lambda prompt: asked.append(prompt) or (
        "s3cret" if "x@a.nl" in prompt else ""))
    answer, skipped = migrate.apply_item(_connection(tmp_path), item)
    assert answer == {"ok": True} and skipped == ["y@a.nl"]
    assert len(asked) == 2 and "x@a.nl" in asked[0] and "y@a.nl" in asked[1]
    final = seen[-1]["item"]["data"]
    assert final["addresses"][0] == {"address": "x@a.nl", "password": "s3cret"}
    assert [a["address"] for a in final["addresses"]] == ["x@a.nl", "z@a.nl"] and final["sieve"] == []
    assert final["spam"] == [{"target": "x@a.nl", "setting": "required_score", "value": "6"}]
    assert item["data"]["addresses"][0]["password_hash"] == "{OLD}1"  # the caller's item is left alone
    assert all("s3cret" not in " ".join(command) for command in fake_ssh.commands)
    assert "s3cret" not in capsys.readouterr().out


def test_an_old_ssh_is_not_mistaken_for_any_other_bad_option(fake_ssh, tmp_path):
    fake_ssh.replies = [(255, "", 'command-line: line 0: Bad configuration option: IdentitiesOnly')]
    with pytest.raises(migrate.Stopped):
        _connection(tmp_path).ask({"request": "hello"})
    assert len(fake_ssh.commands) == 1


def test_ssh_not_installed_is_a_plain_message(monkeypatch, tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError("ssh")
    monkeypatch.setattr(migrate.subprocess, "Popen", missing)
    with pytest.raises(migrate.Stopped) as stopped:
        _connection(tmp_path).ask({"request": "hello"})
    assert "openssh-client" in stopped.value.message


def test_a_first_check_the_new_server_does_not_accept_is_told_plainly(fake_ssh, tmp_path):
    fake_ssh.replies = [(0, '{"ok": false, "error": "no"}\n', "")]
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.check(_connection(tmp_path))
    assert "did not accept the first check: no" in stopped.value.message and "hello" not in stopped.value.message


@pytest.mark.parametrize("version", ['null', 'true', '1.0', '"1"'])
def test_a_missing_or_odd_version_is_refused(fake_ssh, tmp_path, version):
    fake_ssh.replies = [(0, '{"ok": true, "version": %s}\n' % version, "")]
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.check(_connection(tmp_path))
    assert "did not say which release" in stopped.value.message and "None" not in stopped.value.message


def test_a_login_banner_before_the_answer_is_skipped(fake_ssh, tmp_path):
    fake_ssh.replies = [(0, 'Welcome to the new server\n{"ok": true}\n', "")]
    assert _connection(tmp_path).ask({"request": "hello"}) == {"ok": True}


def test_non_ascii_words_from_ssh_are_not_a_traceback(fake_ssh, tmp_path):
    fake_ssh.replies = [(255, b"", "ssh: Could not resolve hostname n\xe9w".encode("latin-1"))]
    with pytest.raises(migrate.Stopped) as stopped:
        _connection(tmp_path).ask({"request": "hello"})
    assert "can't reach" in stopped.value.message


def test_ssh_runs_in_the_c_locale_and_a_stall_is_stopped(monkeypatch, tmp_path):
    seen = {}

    class Stalled(object):
        returncode = None
        killed = False

        def communicate(self, text=None, timeout=None):
            if timeout is not None:
                seen.setdefault("timeouts", []).append(timeout)
                raise migrate.subprocess.TimeoutExpired("ssh", timeout)
            return b"", b""

        def kill(self):
            self.killed = True

    process = Stalled()

    def popen(command, **kwargs):
        seen["env"] = kwargs["env"]
        return process
    monkeypatch.setattr(migrate.subprocess, "Popen", popen)
    with pytest.raises(migrate.Stopped) as stopped:
        _connection(tmp_path).ask({"request": "apply", "item": {}})
    # 1800 to make something, then 10 to reap the killed ssh rather than wait on it for good.
    assert seen["env"]["LC_ALL"] == "C" and seen["timeouts"] == [1800, 10] and process.killed
    assert "part of the work" in stopped.value.message and "status" in stopped.value.message
    with pytest.raises(migrate.Stopped):
        _connection(tmp_path).ask({"request": "status"})
    assert seen["timeouts"] == [1800, 10, 120, 10]


def test_the_public_key_is_rebuilt_when_only_it_is_missing(tmp_path):
    (tmp_path / "migrate_key").write_text("private")
    calls = []

    def fake_run(*args, stdin=None):
        calls.append(args)
        return "ssh-ed25519 AAAArebuilt\n"
    assert migrate.own_key(str(tmp_path), run=fake_run) == "ssh-ed25519 AAAArebuilt migrate"
    assert calls[0][:2] == ("ssh-keygen", "-y") and "-P" in calls[0] and len(calls) == 1


def test_a_missing_ssh_keygen_says_to_install_it(tmp_path):
    def fake_run(*args, stdin=None):
        raise migrate.CommandFailed("ssh-keygen isn't installed", missing=True)
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.own_key(str(tmp_path / "ssh"), run=fake_run)
    assert "openssh-client" in stopped.value.message and "write" not in stopped.value.message


def test_an_unwritable_folder_never_shows_an_error_number(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(migrate.Stopped) as stopped:
        migrate.own_key(str(blocker / "ssh"))
    assert "Errno" not in stopped.value.message


def _no_terminal(monkeypatch):
    """Everything opens as usual except /dev/tty, which a run with no terminal cannot open."""
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/dev/tty":
            raise IOError("no terminal")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", fake_open)


def test_with_no_terminal_a_password_is_never_printed_and_the_account_is_left_out(monkeypatch, capsys):
    """Run as "ssh old python3 migrate.py", getpass would print what was typed. Then we don't ask."""
    _no_terminal(monkeypatch)
    monkeypatch.setattr(migrate.getpass, "getpass", lambda prompt="": "would be echoed")

    assert migrate._typed_password("Password for info@example.nl: ") == ""
    said = capsys.readouterr().out
    assert "no terminal" in said and "left out" in said and "would be echoed" not in said


def test_end_of_input_while_typing_leaves_the_account_out(monkeypatch):
    def raise_eof(prompt=""):
        raise EOFError()
    monkeypatch.setattr(migrate.getpass, "getpass", raise_eof)
    assert migrate._typed_password("Password for info@example.nl: ") == ""


def test_a_killed_ssh_is_never_waited_for_twice_without_a_limit(monkeypatch):
    """The reaping call after the kill has its own timeout: a helper that outlives ssh holds the pipes open."""
    waits = []

    class StuckProcess(object):
        returncode = None

        def communicate(self, text=None, timeout=None):
            waits.append(timeout)
            raise migrate.subprocess.TimeoutExpired("ssh", timeout)

        def kill(self):
            waits.append("killed")

    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *args, **kwargs: StuckProcess())
    connection = migrate.Connection("new.example.nl", key="/tmp/key")
    with pytest.raises(migrate.Stopped) as stopped:
        connection.ask({"request": "hello"})
    assert waits == [120, "killed", 10]
    assert "status" in stopped.value.message
