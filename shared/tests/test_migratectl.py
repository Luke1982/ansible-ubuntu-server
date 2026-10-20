import io
import json

import pytest

from migrate import migratectl


@pytest.fixture
def fake_command(monkeypatch):
    """Pretends mailctl is installed and domainctl is not."""
    installed = {"mailctl": "/usr/local/bin/mailctl"}
    monkeypatch.setattr(migratectl.shutil, "which", installed.get)


def test_hello_says_which_tools_the_server_has(fake_command):
    answer = migratectl.answer({"request": "hello"})
    assert answer["ok"] is True and answer["version"] == 1
    assert answer["tools"] == {"mailctl": True, "domainctl": False}
    assert isinstance(answer["server"], str) and answer["server"]


def test_a_request_it_doesnt_know_is_an_error_not_a_crash():
    answer = migratectl.answer({"request": "dance"})
    assert answer["ok"] is False
    assert answer["error"].startswith("I don't know")


def test_a_request_that_is_not_an_object_is_an_error_not_a_crash():
    assert migratectl.answer(["hello"])["ok"] is False


def test_a_handler_that_blows_up_becomes_an_error(monkeypatch):
    def broken(request):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(migratectl.HANDLERS, "hello", broken)
    answer = migratectl.answer({"request": "hello"})
    assert answer["ok"] is False and "disk on fire" in answer["error"]


def test_every_line_in_is_a_line_out(capsys, monkeypatch, fake_command):
    lines = ['{"request": "hello"}', "not json", "", '{"request": "dance"}']
    monkeypatch.setattr(migratectl.sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    migratectl.main()
    captured = capsys.readouterr()
    answers = [json.loads(line) for line in captured.out.splitlines()]
    assert [a["ok"] for a in answers] == [True, False, False]
    assert captured.err == ""
