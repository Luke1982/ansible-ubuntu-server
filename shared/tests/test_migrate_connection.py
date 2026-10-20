import json
import os
import shutil
import stat

import pytest

from migrate import migrate


class FakeProcess(object):
    def __init__(self, code, out, err, sent):
        self.returncode, self._out, self._err, self._sent = code, out, err, sent

    def communicate(self, text=None):
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
        assert kwargs.get("stdin") == migrate.subprocess.PIPE and kwargs.get("stdout") == migrate.subprocess.PIPE
        fake.commands.append(command)
        reply = fake.replies.pop(0) if isinstance(fake.replies, list) else fake.replies
        if callable(reply):
            process = FakeProcess(0, "", "", [])

            def communicate(text=None):
                fake.sent.append(text)
                code, out, err = reply(json.loads(text))
                return out, err
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
    assert "version 1" in stopped.value.message and "version 2" in stopped.value.message
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
    assert words in stopped.value.message and "Traceback" not in stopped.value.message


def test_an_old_ssh_gets_one_retry_and_one_note(fake_ssh, tmp_path, capsys):
    fake_ssh.replies = [(255, "", "command-line: line 0: Bad configuration option: stricthostkeychecking=accept-new"),
                        (0, '{"ok": true}\n', ""), (0, '{"ok": true}\n', "")]
    connection = _connection(tmp_path)
    assert connection.ask({"request": "hello"}) == {"ok": True}
    assert "StrictHostKeyChecking=no" in fake_ssh.commands[1]
    connection.ask({"request": "hello"})
    assert capsys.readouterr().out.count("too old") == 1


def test_an_account_whose_password_is_needed_is_asked_for_and_enter_skips_it(fake_ssh, monkeypatch, tmp_path, capsys):
    item = {"kind": "mail_domain", "name": "a.nl", "data": {
        "addresses": [{"address": "x@a.nl", "password_hash": "{OLD}1"}, {"address": "y@a.nl", "password_hash": "{OLD}2"},
                      {"address": "z@a.nl", "password_hash": "{SHA512-CRYPT}ok"}],
        "sieve": [{"address": "y@a.nl", "name": "f", "active": True, "content": ""}]}}
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
    monkeypatch.setattr(migrate.getpass, "getpass", lambda prompt: asked.append(prompt) or {"x": "s3cret"}.get(prompt[13]
                                                                                                             , ""))
    answer, skipped = migrate.apply_item(_connection(tmp_path), item)
    assert answer == {"ok": True} and skipped == ["y@a.nl"]
    assert len(asked) == 2 and "x@a.nl" in asked[0] and "y@a.nl" in asked[1]
    final = seen[-1]["item"]["data"]
    assert final["addresses"][0] == {"address": "x@a.nl", "password": "s3cret"}
    assert [a["address"] for a in final["addresses"]] == ["x@a.nl", "z@a.nl"] and final["sieve"] == []
    assert item["data"]["addresses"][0]["password_hash"] == "{OLD}1"  # the caller's item is left alone
    assert all("s3cret" not in " ".join(command) for command in fake_ssh.commands)
    assert "s3cret" not in capsys.readouterr().out
