import json
from pathlib import Path

import pytest

from mailctl.core import config
from mailctl.core.config import Config, SendLimit
from mailctl.core.errors import MailctlError


def write_config(tmp_path: Path, data) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_load_reads_values_and_keeps_defaults(tmp_path):
    path = write_config(
        tmp_path,
        {
            "hostname": "mail.example.nl",
            "send_limits": [{"recipients": 300, "seconds": 3600}],
            "send_limits_by_account": {"News@Example.nl": [{"recipients": 2000, "seconds": 3600}]},
            "vmail_root": str(tmp_path / "vmail"),
            "mail_logs": [str(tmp_path / "mail.log")],
        },
    )

    loaded = config.load(path)

    assert loaded.hostname == "mail.example.nl"
    assert loaded.send_limits == (SendLimit(300, 3600),)
    assert loaded.send_limits_by_account == {"news@example.nl": (SendLimit(2000, 3600),)}
    assert loaded.vmail_root == tmp_path / "vmail"
    assert loaded.mail_logs == (tmp_path / "mail.log",)
    assert loaded.db_socket == "/run/mysqld/mysqld.sock"


def test_load_finds_the_file_through_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILCTL_CONFIG", str(write_config(tmp_path, {"hostname": "mail.example.nl"})))

    assert config.load().hostname == "mail.example.nl"


def test_load_rejects_unknown_settings(tmp_path):
    path = write_config(tmp_path, {"hostname": "mail.example.nl", "colour": "blue"})

    with pytest.raises(MailctlError, match="Unknown setting.*colour"):
        config.load(path)


def test_load_requires_the_hostname(tmp_path):
    with pytest.raises(MailctlError, match="missing.*hostname"):
        config.load(write_config(tmp_path, {}))


def test_load_reports_an_unreadable_file(tmp_path):
    with pytest.raises(MailctlError, match="Can't read"):
        config.load(tmp_path / "missing.json")


def test_load_reports_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{hostname")

    with pytest.raises(MailctlError, match="isn't valid JSON"):
        config.load(path)


def test_load_reports_a_file_that_isnt_utf8(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"hostname": "\xff"}')

    with pytest.raises(MailctlError, match="isn't valid JSON"):
        config.load(path)


def test_load_needs_a_json_object(tmp_path):
    with pytest.raises(MailctlError, match="should hold a JSON object"):
        config.load(write_config(tmp_path, ["mail.example.nl"]))


@pytest.mark.parametrize(
    "setting, value",
    [("send_limits", [{"recipients": 300}]), ("hostname", None), ("vmail_root", 5), ("mail_logs", "/var/log/mail.log")],
)
def test_load_reports_malformed_values(tmp_path, setting, value):
    path = write_config(tmp_path, {"hostname": "mail.example.nl", setting: value})

    with pytest.raises(MailctlError, match=f"Invalid value for {setting}"):
        config.load(path)


def test_limits_for_prefers_account_limits():
    loaded = Config(
        hostname="mail.example.nl",
        send_limits=(SendLimit(300, 3600),),
        send_limits_by_account={"news@example.nl": ()},
    )

    assert loaded.limits_for("info@example.nl") == (SendLimit(300, 3600),)
    assert loaded.limits_for("news@example.nl") == ()
