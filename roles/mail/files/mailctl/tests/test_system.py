import os
import pwd
import time
from ipaddress import ip_address
from types import SimpleNamespace

import pytest

from mailctl.core import system
from mailctl.core.errors import MailctlError


def test_run_returns_standard_output_and_passes_standard_input():
    assert system.run("cat", stdin="hello") == "hello"


def test_run_replaces_output_that_isnt_utf8():
    assert system.run("printf", r"caf\351") == "caf�"


def test_run_reports_a_failing_command_with_its_error_output():
    with pytest.raises(MailctlError, match="^ls /no/such/path failed: .*No such file"):
        system.run("ls", "/no/such/path")


def test_run_reports_a_missing_program():
    with pytest.raises(MailctlError, match="no-such-program isn't installed"):
        system.run("no-such-program")


def test_require_root_refuses_other_users(monkeypatch):
    monkeypatch.setattr(system.os, "geteuid", lambda: 1000)

    with pytest.raises(MailctlError, match="must run as root"):
        system.require_root()


def test_find_user_explains_a_missing_user():
    with pytest.raises(MailctlError, match="There's no user nobody-at-all on this server"):
        system.find_user("nobody-at-all")


def test_as_user_takes_on_the_users_identity_and_always_gives_it_back(monkeypatch):
    calls = []
    monkeypatch.setattr(system.pwd, "getpwnam", lambda name: SimpleNamespace(pw_name=name, pw_uid=5000, pw_gid=5000))
    monkeypatch.setattr(system.os, "getgrouplist", lambda name, gid: [gid, 8])
    monkeypatch.setattr(system.os, "geteuid", lambda: 0)
    monkeypatch.setattr(system.os, "getegid", lambda: 0)
    monkeypatch.setattr(system.os, "getgroups", lambda: [0, 4])
    for name in ("setgroups", "setegid", "seteuid"):
        monkeypatch.setattr(system.os, name, lambda value, name=name: calls.append((name, value)))

    with pytest.raises(RuntimeError):
        with system.as_user("vmail"):
            calls.append("inside")
            raise RuntimeError

    assert calls == [
        ("setgroups", [5000, 8]), ("setegid", 5000), ("seteuid", 5000),
        "inside",
        ("seteuid", 0), ("setegid", 0), ("setgroups", [0, 4]),
    ]


def test_as_user_changes_nothing_for_the_user_the_process_already_is(monkeypatch):
    monkeypatch.setattr(system.os, "setgroups", lambda groups: pytest.fail("changed the groups"))

    with system.as_user(pwd.getpwuid(os.geteuid()).pw_name):
        pass


def test_remove_tree_deletes_a_directory_and_accepts_a_missing_one(tmp_path):
    directory = tmp_path / "mail"
    (directory / "cur").mkdir(parents=True)
    (directory / "cur" / "1").write_text("message")

    system.remove_tree(directory)
    system.remove_tree(directory)

    assert not directory.exists()


def test_remove_tree_explains_why_it_wont_delete_a_symbolic_link(tmp_path):
    (tmp_path / "target").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")

    with pytest.raises(MailctlError, match="Can't delete .*link: .*symbolic link"):
        system.remove_tree(link)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can delete anything")
def test_remove_tree_reports_a_directory_it_cant_delete(tmp_path):
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o500)
    try:
        with pytest.raises(MailctlError, match="Can't delete .*inner"):
            system.remove_tree(locked / "inner")
    finally:
        locked.chmod(0o700)


def test_parse_route_sources_reads_the_source_address_of_a_route():
    output = '[{"dst":"1.1.1.1","gateway":"203.0.113.1","dev":"eth0","prefsrc":"203.0.113.5","flags":[],"cache":[]}]'

    assert system.parse_route_sources(output) == {ip_address("203.0.113.5")}


def test_server_ips_are_the_sources_of_the_default_routes(fake_command):
    fake_command("ip", """
case "$4" in
  1.1.1.1) echo '[{"dst":"1.1.1.1","dev":"eth0","prefsrc":"203.0.113.5"}]' ;;
  *) echo '[{"dst":"2606:4700:4700::1111","dev":"eth0","prefsrc":"2001:db8::5"}]' ;;
esac
""")

    assert system.server_ips() == {ip_address("203.0.113.5"), ip_address("2001:db8::5")}


def test_server_ips_leave_out_a_kind_of_address_without_a_route(fake_command):
    fake_command("ip", """
case "$4" in
  1.1.1.1) echo '[{"dst":"1.1.1.1","dev":"eth0","prefsrc":"203.0.113.5"}]' ;;
  *) echo 'RTNETLINK answers: Network is unreachable' >&2; exit 2 ;;
esac
""")

    assert system.server_ips() == {ip_address("203.0.113.5")}


def test_server_ips_without_any_route_explain_why(fake_command):
    fake_command("ip", "echo 'RTNETLINK answers: Network is unreachable' >&2; exit 2")

    with pytest.raises(MailctlError, match="Can't tell which address this server sends mail from: .*unreachable"):
        system.server_ips()


def test_run_starter_returns_while_a_daemon_it_started_keeps_running(fake_command):
    # lswsctrl starts OpenLiteSpeed and its watchdog in the background when it isn't running.
    fake_command("starter", "sleep 5 & echo '[OK] started'")
    started = time.monotonic()

    assert system.run_starter("starter") == "[OK] started"
    assert time.monotonic() - started < 3


def test_run_starter_reports_a_failure_with_its_output(fake_command):
    fake_command("starter", "echo '[ERROR] Failed to start' ; exit 1")

    with pytest.raises(MailctlError, match=r"^starter failed: \[ERROR\] Failed to start$"):
        system.run_starter("starter")
