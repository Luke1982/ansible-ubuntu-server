import os
import io
import json
import types

import pytest

from migrate import migratectl


def _stdin(data):
    return types.SimpleNamespace(buffer=io.BytesIO(data))


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
    monkeypatch.setattr(migratectl.sys, "stdin", _stdin(("\n".join(lines) + "\n").encode()))
    migratectl.main()
    captured = capsys.readouterr()
    answers = [json.loads(line) for line in captured.out.splitlines()]
    assert [a["ok"] for a in answers] == [True, False, False]
    assert captured.err == ""


def test_a_line_that_is_not_utf8_gets_an_error_and_the_next_line_is_still_answered(capsys, monkeypatch, fake_command):
    monkeypatch.setattr(migratectl.sys, "stdin", _stdin(b'\xff\xfe\n{"request": "hello"}\n'))
    migratectl.main()
    answers = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [a["ok"] for a in answers] == [False, True]


def test_a_line_nested_too_deeply_gets_an_error_and_the_next_line_is_still_answered(
    capsys, monkeypatch, fake_command
):
    deep = b"[" * 100000 + b"]" * 100000
    monkeypatch.setattr(migratectl.sys, "stdin", _stdin(deep + b'\n{"request": "hello"}\n'))
    migratectl.main()
    answers = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [a["ok"] for a in answers] == [False, True]


BAR = "\u2501" * 69
MAIL_TABLE = (
    " " * 69 + " \n"
    " Domain                 Addresses   Forwards   Disk usage   DKIM key \n"
    f" {BAR} \n"
    " boersma-advocaten.nl           1          0          0 B   \u2713        \n"
    " " * 1 + " " * 68 + " \n"
)
SITES_TABLE = (
    " Site      Domain        Also   Certificate \n"
    f" {BAR} \n"
    " example   example.nl    \u2013      valid \n"
)
NO_SITES = "There are no sites on this server yet.\n"


@pytest.fixture
def lists(monkeypatch):
    """Both tools installed; what they print is set per test in the returned dict, keyed by command name."""
    installed = {"mailctl": "/usr/local/bin/mailctl", "domainctl": "/usr/local/bin/domainctl"}
    monkeypatch.setattr(migratectl.shutil, "which", installed.get)
    output = {"mailctl": MAIL_TABLE, "domainctl": SITES_TABLE}
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs)
        out = output[command[0]]
        if isinstance(out, int):
            return types.SimpleNamespace(returncode=out, stdout="", stderr="boom")
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(migratectl.subprocess, "run", run)
    output["seen"] = seen
    return output


def status(item_id, kind, **fields):
    item = {"id": item_id, "kind": kind, **fields}
    answer = migratectl.answer({"request": "status", "items": [item]})
    assert answer["ok"] is True, answer
    return answer["items"][item_id]


def mail(name):
    return status(f"mail:{name}", "mail_domain", name=name)


def site(name, user):
    return status(f"site:{name}", "site", name=name, user=user)


def test_a_mail_domain_the_server_has_is_exists_with_what_is_in_it(lists):
    answer = mail("boersma-advocaten.nl")
    assert answer["state"] == "exists"
    assert "1 accounts" in answer["detail"] and "0 forwards" in answer["detail"]


def test_a_mail_domain_the_server_does_not_have_is_new(lists):
    assert mail("other.nl")["state"] == "new"


def test_the_tables_are_read_at_a_width_that_does_not_cut_them_off(lists):
    mail("other.nl")
    assert lists["seen"][0]["env"]["COLUMNS"] == "200"


def test_a_site_whose_user_is_taken_by_another_site_is_refused(lists):
    answer = site("other.nl", "example")
    assert answer["state"] == "refused" and "example" in answer["detail"]


def test_a_site_that_is_already_set_up_is_exists(lists):
    assert site("example.nl", "example")["state"] == "exists"


def test_a_domain_served_by_another_user_is_refused(lists):
    assert site("example.nl", "other")["state"] == "refused"


def test_a_site_with_a_free_user_and_domain_is_new(lists):
    assert site("other.nl", "other")["state"] == "new"


def test_a_server_with_no_sites_says_new_for_a_site(lists):
    lists["domainctl"] = NO_SITES
    assert site("example.nl", "example")["state"] == "new"


def test_a_cron_job_is_new_because_the_crontab_cannot_be_compared(lists):
    assert status("cron:example:0", "cron", user="example")["state"] == "new"


def test_a_server_without_mailctl_refuses_mail_items(fake_command):
    """A web-only server says so instead of failing on a command that isn't there."""
    answer = mail("example.nl")
    assert answer["state"] == "refused" and "mailctl" in answer["detail"]


def test_a_server_without_domainctl_refuses_sites(fake_command):
    assert site("example.nl", "example")["state"] == "refused"


def test_a_list_that_cannot_be_read_is_refused_never_new(lists):
    lists["mailctl"] = "Something unexpected happened\n"
    answer = mail("example.nl")
    assert answer["state"] == "refused" and "ouldn't read the list of mail domains" in answer["detail"]


def test_a_table_with_a_header_but_no_rows_is_refused_never_new(lists):
    lists["mailctl"] = " Domain  Addresses \n " + BAR + " \n"
    assert mail("example.nl")["state"] == "refused"


def test_a_list_command_that_fails_is_refused_never_new(lists):
    lists["domainctl"] = 1
    assert site("example.nl", "example")["state"] == "refused"


def test_a_list_command_that_cannot_run_is_refused_never_new(lists, monkeypatch):
    def broken(command, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(migratectl.subprocess, "run", broken)
    assert mail("example.nl")["state"] == "refused"


def test_a_kind_it_does_not_know_is_refused(lists):
    assert status("x:1", "teapot")["state"] == "refused"


def test_an_item_without_a_name_is_refused_not_a_crash(lists):
    assert status("mail:?", "mail_domain")["state"] == "refused"


def test_items_without_a_list_or_an_id_are_an_error(lists):
    assert migratectl.answer({"request": "status"})["ok"] is False
    assert migratectl.answer({"request": "status", "items": [{"kind": "site"}]})["ok"] is False


def test_each_item_gets_its_own_answer(lists):
    items = [{"id": "a", "kind": "mail_domain", "name": "other.nl"},
             {"id": "b", "kind": "mail_domain", "name": "boersma-advocaten.nl"}]
    answer = migratectl.answer({"request": "status", "items": items})
    assert {k: v["state"] for k, v in answer["items"].items()} == {"a": "new", "b": "exists"}


def test_an_answer_that_cannot_be_serialized_becomes_an_error_and_the_loop_goes_on(
    capsys, monkeypatch, fake_command
):
    monkeypatch.setitem(migratectl.HANDLERS, "hello", lambda request: {"ok": True, "oops": object()})
    monkeypatch.setattr(migratectl.sys, "stdin", _stdin(b'{"request": "hello"}\n{"request": "dance"}\n'))
    migratectl.main()
    answers = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [a["ok"] for a in answers] == [False, False]
    assert "JSON" in answers[0]["error"]


WRAPPED_SITES = (
    " Site      Domain        Also                       Certificate \n"
    f" {BAR} \n"
    " example   example.nl    www.example.nl, shop.nl,   valid \n"
    "                         blog.example.nl                      \n"
    " second    second.nl     \u2013                          valid \n"
)


def test_a_domain_served_as_an_alias_of_another_site_is_refused(lists):
    lists["domainctl"] = WRAPPED_SITES
    answer = site("shop.nl", "shopuser")
    assert answer["state"] == "refused" and "example" in answer["detail"]


def test_an_alias_on_a_wrapped_line_is_still_found(lists):
    lists["domainctl"] = WRAPPED_SITES
    assert site("blog.example.nl", "blog")["state"] == "refused"


def test_a_site_next_to_a_wrapped_row_is_still_read_and_a_free_domain_is_new(lists):
    lists["domainctl"] = WRAPPED_SITES
    assert site("second.nl", "second")["state"] == "exists"
    assert site("free.nl", "free")["state"] == "new"


def test_a_cron_job_says_nothing_on_the_server_was_checked(lists):
    assert "Nothing on this server was checked" in status("cron:example:0", "cron", user="example")["detail"]


class Machine:
    """A new server that records every command it is asked to run and answers MariaDB from a set of names."""

    def __init__(self, homes):
        self.homes = homes
        self.calls = []
        self.kwargs = []
        self.databases = set()
        self.fail = {}

    def run(self, command, **kwargs):
        self.calls.append(command)
        self.kwargs.append(kwargs)
        failing = self.fail.get(" ".join(command[:2]))
        if failing:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=failing)
        if command[0] == "mariadb":
            taken = any(f"'{name}'" in kwargs["input"] for name in self.databases)
            return types.SimpleNamespace(returncode=0, stdout="1\n0\n" if taken else "0\n0\n", stderr="")
        if command[:2] == ["domainctl", "add"]:
            root = self.homes / command[command.index("--user") + 1] / "public_html" / "www"
            root.mkdir(parents=True, exist_ok=True)
            (root / "index.html").write_text("<p>This site is set up and waiting for its files.</p>")
        if command[:3] == ["domainctl", "wp", "adopt"]:
            config = self.homes / command[3] / "public_html" / "www" / "wp-config.php"
            assert config.exists(), "wp adopt needs a wp-config.php"
            self.databases.add(command[command.index("--database") + 1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def ran(self, program):
        return [call for call in self.calls if call[0] == program]


@pytest.fixture
def machine(monkeypatch, tmp_path):
    new = Machine(tmp_path)
    monkeypatch.setattr(migratectl, "HOMES", str(tmp_path))
    monkeypatch.setattr(migratectl.subprocess, "run", new.run)
    monkeypatch.setattr(migratectl.shutil, "chown", lambda *args, **kwargs: None)
    return new


APPLY_SITE = {"request": "apply", "item": {"id": "site:example.nl", "kind": "site", "name": "example.nl",
                                           "user": "example", "database": "example"}}


def web_root(machine):
    return machine.homes / "example" / "public_html" / "www"


def test_a_new_site_is_made_and_says_where_the_files_go(machine):
    answer = migratectl.answer(APPLY_SITE)
    assert answer["ok"] is True, answer
    assert answer["docroot"] == str(web_root(machine))
    assert answer["database"] == "example" and answer["db_user"] == "example" and "password" not in answer
    assert "wp-config.php" in answer["note"] and "stand-in wp-config.php" in " ".join(answer["steps"])
    assert ["domainctl", "add", "example.nl", "--user", "example", "--no-dns", "--yes"] in machine.calls
    assert ["domainctl", "wp", "adopt", "example", "--database", "example", "--replace", "--yes"] in machine.calls
    assert answer["steps"]


def test_a_web_root_with_files_asks_before_anything_is_touched(machine):
    web_root(machine).mkdir(parents=True)
    (web_root(machine) / "index.php").write_text("<?php")
    answer = migratectl.answer(APPLY_SITE)
    assert answer["ok"] is False and answer["ask"]["about"] == "files"
    assert machine.ran("domainctl") == [] and machine.ran("rsync") == []


def test_a_web_root_with_only_what_domainctl_left_there_is_not_asked_about(machine):
    web_root(machine).mkdir(parents=True)
    (web_root(machine) / "index.html").write_text("<p>This site is set up and waiting for its files.</p>")
    (web_root(machine) / ".well-known").mkdir()
    assert migratectl.answer(APPLY_SITE)["ok"] is True


def test_a_database_that_is_already_there_asks_before_anything_is_made(machine):
    machine.databases.add("example")
    answer = migratectl.answer(APPLY_SITE)
    assert answer["ok"] is False and answer["ask"]["about"] == "database"
    assert machine.ran("domainctl") == []


def test_answering_yes_goes_on_and_answering_no_leaves_both_sides_as_they_were(machine):
    machine.databases.add("example")
    declined = migratectl.answer({**APPLY_SITE, "answers": {"database": "no"}})
    assert declined["ok"] is False and "ask" not in declined and "Nothing was changed" in declined["error"]
    assert machine.ran("domainctl") == []
    assert migratectl.answer({**APPLY_SITE, "answers": {"database": "yes"}})["ok"] is True


def test_a_failing_command_is_a_refusal_that_says_what_was_already_made(machine):
    machine.fail["domainctl wp"] = "There is already a database example."
    answer = migratectl.answer(APPLY_SITE)
    assert answer["ok"] is False and "already a database" in answer["error"]
    assert "user example" in answer["error"] and "stand-in wp-config.php" in answer["error"]
    assert "the database example" not in answer["error"].split("Now there is")[1]


def test_an_item_that_is_not_a_site_is_an_error_not_a_crash(machine):
    answer = migratectl.answer({"request": "apply", "item": {"kind": "cron", "user": "example"}})
    assert answer["ok"] is False and machine.calls == []


def test_finish_gives_the_files_to_the_site_user(machine):
    migratectl.answer(APPLY_SITE)
    (web_root(machine) / "index.php").write_text("<?php")
    (web_root(machine) / "wp-config.php").write_text("<?php // the site's own")
    answer = migratectl.answer({"request": "finish_site", "item": APPLY_SITE["item"]})
    assert answer["ok"] is True, answer
    assert ["chown", "-R", "-h", "example:example", str(web_root(machine))] in machine.calls
    assert machine.calls[-1] == ["domainctl", "sync"]
    assert not (web_root(machine) / "index.html").exists()


def test_no_child_command_reads_the_request_pipe(machine):
    migratectl.answer(APPLY_SITE)
    migratectl.answer({"request": "finish_site", "item": APPLY_SITE["item"]})
    assert machine.kwargs
    assert all("input" in kw or kw.get("stdin") == migratectl.subprocess.DEVNULL for kw in machine.kwargs)


def test_the_commands_behind_status_do_not_read_the_request_pipe_either(lists):
    status("site:example.nl", "site", name="example.nl", user="example")
    mail("other.nl")
    assert len(lists["seen"]) == 2
    assert all(kw.get("stdin") == migratectl.subprocess.DEVNULL for kw in lists["seen"])


def test_a_name_in_capitals_is_lower_cased_and_that_is_what_domainctl_gets(machine):
    item = {**APPLY_SITE["item"], "name": " Example.NL ", "user": "Example", "database": "Example"}
    assert migratectl.answer({"request": "apply", "item": item})["ok"] is True
    assert ["domainctl", "add", "example.nl", "--user", "example", "--no-dns", "--yes"] in machine.calls


def test_a_long_site_domain_is_accepted_up_to_the_dns_limit(machine):
    long = ".".join(["a" * 60] * 3) + ".nl"
    item = {**APPLY_SITE["item"], "name": long}
    assert len(long) > 50 and migratectl.answer({"request": "apply", "item": item})["ok"] is True


def test_a_site_without_a_database_gets_no_stand_in_and_no_adopt(machine):
    item = {**APPLY_SITE["item"], "database": None}
    answer = migratectl.answer({"request": "apply", "item": item})
    assert answer["ok"] is True and answer["database"] is None
    assert not (web_root(machine) / "wp-config.php").exists()
    assert not [call for call in machine.calls if call[:3] == ["domainctl", "wp", "adopt"]]


def test_a_real_wp_config_is_never_overwritten_by_the_stand_in(machine):
    web_root(machine).mkdir(parents=True)
    (web_root(machine) / "wp-config.php").write_text("<?php // the real one")
    answer = migratectl.answer({**APPLY_SITE, "answers": {"files": "yes"}})
    assert answer["ok"] is True and (web_root(machine) / "wp-config.php").read_text() == "<?php // the real one"


def test_finish_refuses_a_site_whose_files_never_brought_a_real_wp_config(machine):
    migratectl.answer(APPLY_SITE)
    (web_root(machine) / "index.php").write_text("<?php")
    answer = migratectl.answer({"request": "finish_site", "item": APPLY_SITE["item"]})
    assert answer["ok"] is False and "never brought a real wp-config.php" in answer["error"]
    assert (web_root(machine) / "index.html").exists()
    assert ["domainctl", "sync"] not in machine.calls and machine.ran("chown") == []


@pytest.mark.parametrize("field, value", [("name", "-rf.nl"), ("name", "nodot"), ("user", "_x"), ("user", "ex ample"),
                                          ("database", "a$b"), ("database", "x" * 33)])
def test_a_name_domainctl_would_refuse_is_refused_before_anything_is_made(machine, field, value):
    item = {**APPLY_SITE["item"], field: value}
    answer = migratectl.answer({"request": "apply", "item": item})
    assert answer["ok"] is False and machine.calls == []


class Server:
    """A new server for mail and cron: mailctl import reads its file, doveadm lists schemes, crontab keeps text."""

    def __init__(self):
        self.calls, self.kwargs = [], []
        self.import_file = None
        self.import_mode = None
        self.import_exit = 0
        self.accounts = {"old@a.nl"}
        self.crontabs = {}
        self.imported = None  # what the import file held when mailctl was run
        self.domains = []  # mail domains already here
        self.here = {}  # domain -> accounts already here; a missing domain list or account list can be broken
        self.break_lists = False
        self.doveadm = "SHA512-CRYPT SHA256-CRYPT MD5-CRYPT PLAIN\n"
        self.refuse = False
        self.crontab_error = None

    def run(self, command, **kwargs):
        self.calls.append(command)
        self.kwargs.append(kwargs)
        ok = lambda out="": types.SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        if command[:2] == ["doveadm", "pw"]:
            return ok(self.doveadm) if self.doveadm is not None else \
                types.SimpleNamespace(returncode=1, stdout="", stderr="doveadm: broken")
        if command[:3] == ["mailctl", "domain", "list"]:
            if self.break_lists:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="broken")
            if not self.domains:
                return ok("There are no mail domains yet.\n")
            return ok(f" Domain  Addresses  Forwards\n {BAR}\n" + "".join(f" {d}  1  0\n" for d in self.domains))
        if command[:3] == ["mailctl", "address", "list"]:
            found = self.here.get(command[3], [])
            if not found:
                return ok(f"{command[3]} has no accounts yet.\n")
            return ok(f" Address  Disk usage\n {BAR}\n" + "".join(f" {a}  0 B\n" for a in found))
        if command[:2] == ["mailctl", "import"]:
            self.import_file = command[2]
            self.import_mode = os.stat(command[2]).st_mode & 0o777
            with open(command[2]) as file:
                self.imported = json.load(file)
            if self.refuse:
                return types.SimpleNamespace(
                    returncode=1, stdout="", stderr="Error: New passwords are needed for 1 account, and mailctl "
                                                    "can't ask for them without a terminal.\nHint: give them a password.")
            if self.import_exit:
                return types.SimpleNamespace(returncode=self.import_exit, stdout="", stderr="Error: no good")
            return ok("Imported 2 accounts.\n")
        if command[:3] == ["mailctl", "address", "password"]:
            if command[3] not in self.accounts:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=f"Error: {command[3]} doesn't exist")
            return ok()
        if command[0] == "crontab" and command[3:] == ["-l"]:
            if self.crontab_error:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=self.crontab_error)
            if command[2] not in self.crontabs:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=f"no crontab for {command[2]}")
            return ok(self.crontabs[command[2]])
        if command[0] == "crontab" and command[3:] == ["-"]:
            self.crontabs[command[2]] = kwargs["input"]
            return ok()
        raise AssertionError(command)


@pytest.fixture
def server(monkeypatch):
    new = Server()
    monkeypatch.setattr(migratectl.subprocess, "run", new.run)
    return new


def mail_item(*accounts):
    return {"id": "mail:a.nl", "kind": "mail_domain", "name": "a.nl",
            "data": {"domains": ["a.nl"], "addresses": list(accounts)}}


OWN = {"address": "own@a.nl", "password_hash": "{SHA512-CRYPT}$6$xx"}
WEAK = {"address": "weak@a.nl", "password_hash": "{MD5-CRYPT}$1$xx"}
UNREADABLE = {"address": "odd@a.nl", "password_hash": "{SSHA256}secrethashvalue"}


def test_a_mail_domain_is_imported_and_the_file_is_deleted(server):
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN, WEAK)})
    assert answer["ok"] is True, answer
    call = next(c for c in server.calls if c[:2] == ["mailctl", "import"])
    assert call[3:] == ["--yes", "--verbose", "--keep-hashes"]
    assert not os.path.exists(server.import_file)
    assert server.imported == mail_item(OWN, WEAK)["data"]
    assert answer["mailctl"]["exit"] == 0 and "Imported" in answer["mailctl"]["stdout"]
    assert answer["weak"] == ["weak@a.nl"]


def test_the_file_is_never_readable_by_anybody_else(server):
    migratectl.answer({"request": "apply", "item": mail_item(OWN)})
    assert server.import_mode == 0o600


def test_an_account_that_needs_a_password_is_named_back(server):
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN, UNREADABLE)})
    assert answer["ok"] is False and answer["needs_password"] == ["odd@a.nl"]
    assert "Nothing was created" in answer["error"] and "password" in answer["error"]
    assert not [c for c in server.calls if c[:2] == ["mailctl", "import"]]
    assert "secrethashvalue" not in json.dumps(answer)


def test_an_account_given_its_password_imports_as_one_ordinary_import(server):
    given = {**UNREADABLE, "password": "plain-secret"}
    answer = migratectl.answer({"request": "apply", "item": mail_item(given)})
    assert answer["ok"] is True and "needs_password" not in answer
    assert "plain-secret" not in json.dumps(answer)


def test_the_import_file_is_gone_after_a_failure_and_the_answer_carries_mailctls_words(server):
    server.import_exit = 1
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN)})
    assert answer["ok"] is False and answer["mailctl"]["exit"] == 1
    assert "no good" in answer["error"]
    assert not os.path.exists(server.import_file)


def test_a_password_typed_on_the_old_server_is_hashed_here(server):
    item = {"id": "p", "kind": "mail_passwords", "passwords": {"old@a.nl": "hunter22hunter"}}
    answer = migratectl.answer({"request": "apply", "item": item})
    assert answer["ok"] is True and answer["done"] == ["old@a.nl"]
    assert server.calls == [["mailctl", "address", "password", "old@a.nl", "--password-stdin"]]
    assert "hunter22hunter" in server.kwargs[0]["input"]
    assert not any("hunter22hunter" in part for call in server.calls for part in call)


def test_an_address_that_does_not_exist_is_reported_and_the_rest_go_on(server):
    item = {"id": "p", "kind": "mail_passwords", "passwords": {"gone@a.nl": "pw-one-1234", "old@a.nl": "pw-two-1234"}}
    answer = migratectl.answer({"request": "apply", "item": item})
    assert answer["ok"] is False and answer["done"] == ["old@a.nl"] and list(answer["failed"]) == ["gone@a.nl"]
    assert "pw-one-1234" not in json.dumps(answer)


CRON = {"id": "cron:example:0", "kind": "cron", "user": "example", "line": "*/5 * * * * php /home/example/x.php",
        "settings": ["MAILTO=me@example.nl", "PATH=/usr/bin"]}


def test_a_cron_job_is_installed_for_the_sites_user(server):
    answer = migratectl.answer({"request": "apply", "item": CRON})
    assert answer["ok"] is True, answer
    assert server.crontabs["example"] == ("# migratectl: cron:example:0\nMAILTO=me@example.nl\nPATH=/usr/bin\n"
                                          "*/5 * * * * php /home/example/x.php\n")


def test_the_same_cron_item_applied_twice_adds_one_job_and_keeps_existing_lines(server):
    server.crontabs["example"] = "# mine\n0 1 * * * /bin/true"
    migratectl.answer({"request": "apply", "item": CRON})
    again = migratectl.answer({"request": "apply", "item": CRON})
    assert again["ok"] is True and again["already"] is True
    text = server.crontabs["example"]
    assert text.startswith("# mine\n0 1 * * * /bin/true\n") and text.count("php /home/example/x.php") == 1


def test_no_mail_or_cron_child_reads_the_request_pipe(server):
    migratectl.answer({"request": "apply", "item": mail_item(WEAK)})
    migratectl.answer({"request": "apply", "item": {"id": "p", "kind": "mail_passwords", "passwords": {"old@a.nl": "x"}}})
    migratectl.answer({"request": "apply", "item": CRON})
    assert server.kwargs
    assert all("input" in kw or kw.get("stdin") == migratectl.subprocess.DEVNULL for kw in server.kwargs)


def test_an_unknown_kind_is_still_refused(server):
    answer = migratectl.answer({"request": "apply", "item": {"id": "x", "kind": "dance"}})
    assert answer["ok"] is False and server.calls == []


def test_an_existing_account_with_an_unreadable_hash_is_not_refused_over(server):
    server.domains, server.here = ["a.nl"], {"a.nl": ["odd@a.nl"]}
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN, UNREADABLE)})
    assert answer["ok"] is True and "needs_password" not in answer and "note" not in answer
    assert server.imported["addresses"] == [OWN, UNREADABLE]


def test_when_the_accounts_here_cannot_be_read_every_account_is_judged_and_it_is_said(server):
    server.domains, server.break_lists = ["a.nl"], True
    answer = migratectl.answer({"request": "apply", "item": mail_item(UNREADABLE)})
    assert answer["ok"] is False and answer["needs_password"] == ["odd@a.nl"]
    assert "couldn't read which accounts" in answer["note"]


def test_mailctl_refusing_on_its_own_is_answered_like_our_own_check(server):
    server.refuse = True
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN)})
    assert answer["ok"] is False and answer["needs_password"] == []
    assert "Nothing was created" in answer["error"] and "New passwords are needed" in answer["error"]
    assert answer["mailctl"]["exit"] == 1 and not os.path.exists(server.import_file)


@pytest.mark.parametrize("output", [None, "", "\n"])
def test_dovecot_failing_or_listing_nothing_is_a_failure_not_a_list_of_unreadable_accounts(server, output):
    server.doveadm = output
    answer = migratectl.answer({"request": "apply", "item": mail_item(WEAK)})
    assert answer["ok"] is False and "needs_password" not in answer
    assert not [c for c in server.calls if c[:2] == ["mailctl", "import"]]


def test_a_crontab_that_cannot_be_read_is_never_replaced(server):
    server.crontabs["example"] = "0 1 * * * /bin/true\n"
    server.crontab_error = "crontab: cannot open the spool"
    answer = migratectl.answer({"request": "apply", "item": CRON})
    assert answer["ok"] is False and "Nothing was changed" in answer["error"]
    assert server.crontabs["example"] == "0 1 * * * /bin/true\n"
    assert ["crontab", "-u", "example", "-"] not in server.calls


def test_a_marker_that_is_a_prefix_of_another_items_id_does_not_count(server):
    other = {**CRON, "id": "cron:example:01", "line": "0 3 * * * other"}
    migratectl.answer({"request": "apply", "item": other})
    answer = migratectl.answer({"request": "apply", "item": CRON})
    assert answer["already"] is False
    assert "php /home/example/x.php" in server.crontabs["example"] and "0 3 * * * other" in server.crontabs["example"]
