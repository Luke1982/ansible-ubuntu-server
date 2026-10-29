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
    assert answer["ok"] is True and answer["version"] == migratectl.PROTOCOL_VERSION
    assert answer["tools"] == {"mailctl": True, "domainctl": False}
    assert isinstance(answer["server"], str) and answer["server"]


def test_hello_says_which_php_a_cron_job_must_run_here(monkeypatch):
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: "/usr/bin/php" if name == "php" else None)
    assert migratectl.answer({"request": "hello"})["php"] == "/usr/bin/php"


def test_hello_falls_back_to_openlitespeeds_own_php(monkeypatch, tmp_path):
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: None)
    lsphp = tmp_path / "lsphp83" / "bin" / "lsphp"
    lsphp.parent.mkdir(parents=True)
    lsphp.write_text("")
    monkeypatch.setattr(migratectl, "OLS_PHP", str(tmp_path / "lsphp*" / "bin" / "lsphp"))
    assert migratectl.answer({"request": "hello"})["php"] == str(lsphp)


def test_hello_says_php_is_none_when_this_server_has_none(monkeypatch):
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: None)
    monkeypatch.setattr(migratectl, "OLS_PHP", "/nowhere/lsphp*/bin/lsphp")
    assert migratectl.answer({"request": "hello"})["php"] is None


def test_the_two_halves_speak_the_same_version():
    """A new migrate.py meeting an older migratectl is caught at the handshake, not halfway through a site."""
    from migrate import migrate
    assert migrate.PROTOCOL_VERSION == migratectl.PROTOCOL_VERSION
    # It went up when finish_site, dns, php, wp and the addresses were added, and again when the mail
    # messages went out of scope and the password request came in; never back.
    assert migratectl.PROTOCOL_VERSION >= 4


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
    """Both tools installed; what they print is set per test in the returned dict, keyed by command name.

    "domainctl state" has a key of its own: it is the one command that can tell a site waiting for its DNS from
    one that was never made, and its answer is one word, not a table.
    """
    installed = {"mailctl": "/usr/local/bin/mailctl", "domainctl": "/usr/local/bin/domainctl",
                 "crontab": "/usr/bin/crontab"}
    monkeypatch.setattr(migratectl.shutil, "which", installed.get)
    output = {"mailctl": MAIL_TABLE, "domainctl": SITES_TABLE, "domainctl state": "absent\n", "crontab": ""}
    seen, commands = [], []

    def run(command, **kwargs):
        seen.append(kwargs)
        commands.append(list(command))
        out = output[" ".join(command[:2])] if " ".join(command[:2]) in output else output[command[0]]
        if isinstance(out, int):
            return types.SimpleNamespace(returncode=out, stdout="", stderr="boom")
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(migratectl.subprocess, "run", run)
    output["seen"] = seen
    output["commands"] = commands
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


def test_a_site_that_was_made_here_and_waits_for_its_dns_is_exists(lists):
    """The only window a migration runs in: domainctl saves no virtual host until the domain points here, so the
    list is empty for a site that has completely moved."""
    lists["domainctl"], lists["domainctl state"] = NO_SITES, "made\n"
    answer = site("example.nl", "example")
    assert answer["state"] == "exists"
    assert "waiting for the DNS" in answer["detail"] and "example.nl" in answer["detail"]


def test_a_site_the_list_shows_as_waiting_for_its_dns_is_exists_not_another_site(lists):
    """domainctl list now has a row for such a site, with a dash where its domain goes; that dash is not some
    other domain the user serves."""
    lists["domainctl"] = (" Site                 Domain   Also   Certificate         \n"
                          f" {BAR} \n"
                          " cateringwestbetuwe   –        –      waiting for the DNS \n")
    lists["domainctl state"] = "made\n"
    answer = site("cateringwestbetuwe.nl", "cateringwestbetuwe")
    assert answer["state"] == "exists" and "waiting for the DNS" in answer["detail"]


def test_a_site_nothing_is_known_about_is_new_and_domainctl_is_asked_about_its_user(lists):
    lists["domainctl"] = NO_SITES
    assert site("example.nl", "example")["state"] == "new"
    assert lists["commands"][-1] == ["domainctl", "state", "example"]


def test_a_site_state_that_cannot_be_read_is_refused_never_new(lists):
    lists["domainctl"], lists["domainctl state"] = NO_SITES, "I don't know that command\n"
    answer = site("example.nl", "example")
    assert answer["state"] == "refused" and "example" in answer["detail"]


def test_a_site_state_command_that_fails_is_refused_never_new(lists):
    lists["domainctl"], lists["domainctl state"] = NO_SITES, 1
    assert site("example.nl", "example")["state"] == "refused"


def test_a_user_that_already_has_a_site_here_is_refused(lists):
    """The list didn't name it, so this says nothing about which domain it serves: it is another site either way."""
    lists["domainctl"], lists["domainctl state"] = NO_SITES, "served\n"
    answer = site("example.nl", "example")
    assert answer["state"] == "refused" and "another user name" in answer["detail"].lower()


def test_a_cron_job_the_crontab_does_not_have_yet_is_new(lists):
    assert status("cron:example:0", "cron", user="example")["state"] == "new"
    assert lists["commands"][-1] == ["crontab", "-u", "example", "-l"]


def test_a_cron_job_the_crontab_already_has_is_exists(lists):
    """Its marker carries the item's own id, so this is the same job and not another one of that user's."""
    lists["crontab"] = "# mine\n0 1 * * * /bin/true\n# migratectl: cron:example:0\n*/5 * * * * php x.php\n"
    answer = status("cron:example:0", "cron", user="example")
    assert answer["state"] == "exists" and "already in the crontab of example" in answer["detail"]
    assert status("cron:example:1", "cron", user="example")["state"] == "new"


def test_a_crontab_that_cannot_be_read_is_refused_never_new(lists):
    lists["crontab"] = 1
    assert status("cron:example:0", "cron", user="example")["state"] == "refused"


def test_a_user_without_a_crontab_is_new_and_not_an_error(lists, monkeypatch):
    def run(command, **kwargs):
        if command[0] == "crontab":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for example")
        raise AssertionError(command)

    monkeypatch.setattr(migratectl.subprocess, "run", run)
    assert status("cron:example:0", "cron", user="example")["state"] == "new"


def test_a_server_without_the_crontab_command_refuses_a_cron_job(fake_command):
    answer = status("cron:example:0", "cron", user="example")
    assert answer["state"] == "refused" and "crontab" in answer["detail"]


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


def test_a_cron_job_says_what_was_looked_at(lists):
    assert "crontab of example doesn't have this job" in status("cron:example:0", "cron", user="example")["detail"]


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


def test_the_question_about_a_taken_database_offers_a_name_that_is_free(machine):
    machine.databases = {"example", "example_2"}

    answer = migratectl.answer(APPLY_SITE)

    assert answer["ask"]["about"] == "database"
    assert "example_3 is free" in answer["ask"]["question"]


def test_another_name_for_the_database_is_the_one_it_makes(machine):
    """The site keeps its own name in its own wp-config here; the database it gets there is the one given."""
    machine.databases = {"example"}
    item = dict(APPLY_SITE["item"], new_database="example_2")

    answer = migratectl.answer({"request": "apply", "item": item})

    assert answer["ok"] is True and answer["database"] == "example_2" and answer["db_user"] == "example_2"
    assert ["domainctl", "wp", "adopt", "example", "--database", "example_2", "--replace",
            "--yes"] in machine.calls


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


def test_the_database_name_in_the_query_is_quoted_like_domainctl_does(machine):
    """It is checked before it gets here, but the quoting belongs where the value meets the SQL."""
    assert migratectl._quoted("o'b") == "'o''b'" and migratectl._quoted("a\\b") == "'a\\\\b'"
    migratectl._database_taken("o'b")
    query = next(kw["input"] for call, kw in zip(machine.calls, machine.kwargs) if call[0] == "mariadb")
    assert "SCHEMA_NAME = 'o''b'" in query and "User = 'o''b'" in query


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
        self.signage = []  # the piSignage servers here, each (user, domain, port)

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
        if command[:3] == ["domainctl", "database", "add"]:
            if self.refuse:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Error: no good")
            return ok("Made the database.\n")
        if command[:3] == ["domainctl", "signage", "list"]:
            if self.break_lists:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="broken")
            if not self.signage:
                return ok("There are no piSignage servers on this server yet.\n")
            width = [max([len(head)] + [len(one[i]) for one in self.signage])
                     for i, head in enumerate(("Name", "Domain", "Port"))]
            head = " %s  %s  %s  Running  Certificate \n" % tuple(
                word.ljust(width[i]) for i, word in enumerate(("Name", "Domain", "Port")))
            return ok(head + f" {BAR} \n" + "".join(
                " %s  %s  %s  yes      valid \n" % tuple(one[i].ljust(width[i]) for i in range(3))
                for one in self.signage))
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


def test_a_password_is_only_ever_set_by_sending_the_domain_again(server):
    """There is no request that sets a password by itself: a password comes back in the item it belongs to, and
    mailctl hashes it in this server's own scheme. A request that could set one and nothing sends is a way in."""
    item = {"id": "p", "kind": "mail_passwords", "passwords": {"old@a.nl": "hunter22hunter"}}
    answer = migratectl.answer({"request": "apply", "item": item})
    assert answer["ok"] is False and "mail_passwords" in answer["error"]
    assert server.calls == []


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


def test_an_import_that_was_stopped_halfway_says_the_domain_may_be_half_there(server, monkeypatch):
    """A big domain's import can legitimately outlast the limit, and mailctl is then killed mid-import."""
    def slow(command, **kwargs):
        if command[:2] == ["mailctl", "import"]:
            raise migratectl.subprocess.TimeoutExpired(command, migratectl.IMPORT_TIMEOUT)
        return server.run(command, **kwargs)

    monkeypatch.setattr(migratectl.subprocess, "run", slow)
    answer = migratectl.answer({"request": "apply", "item": mail_item(OWN)})
    assert answer["ok"] is False
    assert "part of the domain may have been created" in answer["error"]
    assert "mailctl address list a.nl" in answer["error"]
    assert "Nothing is known" not in answer["error"]


def test_the_import_gets_longer_than_a_list_or_an_add(server):
    assert migratectl.IMPORT_TIMEOUT > migratectl.COMMAND_TIMEOUT
    assert migratectl.IMPORT_TIMEOUT < 1800  # the old server stops waiting for an apply at half an hour


def test_hello_says_where_wp_cli_is(monkeypatch, tmp_path):
    """A WordPress site's own cron jobs run wp-cli, and domainctl runs it from a place of its own."""
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: None)
    wp = tmp_path / "wp"
    wp.write_text("")
    monkeypatch.setattr(migratectl, "WP_CLI", str(wp))
    assert migratectl.answer({"request": "hello"})["wp"] == str(wp)


def test_hello_falls_back_to_the_wp_on_the_path_and_says_none_without_one(monkeypatch):
    monkeypatch.setattr(migratectl, "WP_CLI", "/nowhere/wp")
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: "/opt/bin/wp" if name == "wp" else None)
    assert migratectl.answer({"request": "hello"})["wp"] == "/opt/bin/wp"
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: None)
    assert migratectl.answer({"request": "hello"})["wp"] is None


# ---- a new password for one account ------------------------------------------

def test_a_password_goes_into_mailctls_standard_input_and_never_into_a_command(monkeypatch):
    ran = []

    def run(command, **kwargs):
        ran.append((list(command), kwargs.get("input")))
        return types.SimpleNamespace(returncode=0, stdout="Changed it.", stderr="")

    monkeypatch.setattr(migratectl.subprocess, "run", run)
    answer = migratectl.answer({"request": "password", "address": "Info@Example.NL",
                                "password": "Kw7mRt2xPqL9sVbn"})
    assert answer["ok"] is True and "info@example.nl" in answer["steps"][0]
    assert ran == [(["mailctl", "address", "password", "info@example.nl", "--password-stdin"],
                    "Kw7mRt2xPqL9sVbn\n")]


def test_a_password_request_that_makes_no_sense_is_refused(monkeypatch):
    monkeypatch.setattr(migratectl.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    for request in ({"address": "not an address", "password": "x" * 16},
                    {"address": "info@example.nl", "password": ""},
                    {"address": "info@example.nl", "password": "two\nlines"},
                    {"address": "info@example.nl"}):
        answer = migratectl.answer(dict(request, request="password"))
        assert answer["ok"] is False and answer["error"]


def test_a_mailctl_that_refuses_the_password_is_an_answer_and_not_a_crash(monkeypatch):
    monkeypatch.setattr(migratectl.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=1, stdout="", stderr="The password is too short."))
    answer = migratectl.answer({"request": "password", "address": "info@example.nl",
                                "password": "Kw7mRt2xPqL9sVbn"})
    assert answer["ok"] is False and "too short" in answer["error"]


def test_the_requests_it_takes_are_the_ones_the_old_server_sends():
    assert sorted(migratectl.HANDLERS) == ["adopt_database", "apply", "bayes", "bayes_prepare", "bayes_swap",
                                           "compare_mail", "copy_mail", "copy_mail_status", "dns",
                                           "finish_site", "hello", "mail_sizes", "password", "point_dns",
                                           "repair", "restore_webmail", "status", "webmail_place"]
    assert sorted(migratectl.KINDS) == ["cron", "mail_domain", "site"]


REPAIR_FOUND_AND_FIXED = """\
  ✗ MX           example.nl has no MX record.
  ✗ Certificate  mail.example.nl isn't in this server's certificate.
  ✓ Published the mail records of example.nl at TransIP.
  ✓ Postfix and Dovecot serve a certificate for 17 names.
See how it stands now with: mailctl doctor example.nl
"""

DOCTOR_SAYS_RIGHT = """\
Server web03.majorlabel.nl
  ✗ Reverse DNS  178.21.114.195 has no name.
✓ example.nl  everything is set up.

✗ Found 1 problem.
"""

DOCTOR_SAYS_WRONG = """\
example.nl
  ✗ Certificate  mail.example.nl isn't in this server's certificate.

✗ Found 1 problem.
"""


def _repairs(monkeypatch, doctor=DOCTOR_SAYS_RIGHT, code=1, blow_up=False):
    """mailctl repair, and the doctor that is asked after it how the domain stands now."""
    ran = []

    def fake(command, **named):
        ran.append(command)
        if command[1] == "doctor":
            if blow_up:
                raise OSError("no mailctl here")
            return types.SimpleNamespace(returncode=code, stdout=doctor, stderr="")
        return types.SimpleNamespace(returncode=0, stdout=REPAIR_FOUND_AND_FIXED, stderr="")

    monkeypatch.setattr(migratectl.subprocess, "run", fake)
    return ran


def test_repair_runs_mailctl_repair_with_yes_and_then_asks_how_the_domain_stands(monkeypatch):
    """The repair prints what it found before it puts it right, so its own output says a fixed check is wrong.
    What comes back is the check made afterwards."""
    ran = _repairs(monkeypatch)

    answer = migratectl.answer({"request": "repair", "domain": " Example.NL "})

    assert ran == [["mailctl", "repair", "example.nl", "--yes"], ["mailctl", "doctor", "example.nl"]]
    assert answer["ok"] is True and answer["right"] is True
    # Not the catalogue of what it found and fixed: that is what made the old server call a repaired domain broken.
    assert "✗ MX" not in answer["text"] and "everything is set up" in answer["text"]
    assert migratectl.answer({"request": "repair", "domain": "not a domain"})["ok"] is False


def test_a_domain_that_is_still_not_right_comes_back_with_what_is_wrong_with_it(monkeypatch):
    _repairs(monkeypatch, doctor=DOCTOR_SAYS_WRONG)

    answer = migratectl.answer({"request": "repair", "domain": "example.nl"})

    assert answer["ok"] is True and answer["right"] is False
    assert "mail.example.nl isn't in this server's certificate" in answer["text"]


def test_a_problem_of_this_server_is_not_laid_at_the_domains_door(monkeypatch):
    """Doctor's exit status covers the server too; the domain's own line is what is asked about here."""
    _repairs(monkeypatch, doctor=DOCTOR_SAYS_RIGHT, code=1)
    assert migratectl.answer({"request": "repair", "domain": "example.nl"})["right"] is True


def test_a_doctor_that_cannot_be_asked_claims_nothing_either_way(monkeypatch):
    _repairs(monkeypatch, blow_up=True)
    answer = migratectl.answer({"request": "repair", "domain": "example.nl"})
    assert answer["ok"] is True and answer["right"] is None and answer["text"] == ""


# ---- copying the messages of one mailbox over --------------------------------

LOG_OF_A_FINISHED_COPY = """\
Host1: imap.old.nl says it has 2 messages
msg INBOX/1 copied to INBOX/1 1.0 msgs/s 2 KiB/s 2 KiB copied 0:00
msg INBOX/2 copied to INBOX/2 1.0 msgs/s 2 KiB/s 4 KiB copied 0:00
The sync looks good, all 2 identified messages in host1 are on host2.
Detected 0 errors
Messages transferred                    : 2
Exiting with return value 0 (EX_OK: successful termination) 0/50 nb_errors/max_errors PID 20332
"""


class Imapsync(object):
    """A stand-in for imapsync: it says how it was started and pretends to go on running."""

    def __init__(self):
        self.commands, self.named, self.pid = [], {}, 4321

    def __call__(self, command, **named):
        self.commands.append(list(command))
        self.named = named
        named["stdout"].write("Host1: connecting\n")
        return types.SimpleNamespace(pid=self.pid)


@pytest.fixture
def imapsync(monkeypatch, tmp_path):
    """imapsync installed, with a place of its own to write, and nothing really started."""
    binary = tmp_path / "imapsync"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(migratectl, "IMAPSYNC", str(binary))
    monkeypatch.setattr(migratectl, "COPY_ROOT", str(tmp_path / "mail"))
    started = Imapsync()
    monkeypatch.setattr(migratectl.subprocess, "Popen", started)
    return started


def _copy(**more):
    return migratectl.answer(dict({"request": "copy_mail", "address": "Old@A.nl",
                                   "password": "Kw7mRt2xPqL9sVbn", "host": "178.21.114.195"}, **more))


def test_copying_a_mailbox_starts_imapsync_with_the_password_in_a_file_of_its_own(server, imapsync, tmp_path):
    """A password in a command would stand in the process list for everyone on the server to read."""
    server.here = {"a.nl": ["old@a.nl"]}
    answer = _copy()
    assert answer["ok"] is True and answer["started"] is True and answer["pid"] == 4321
    command = imapsync.commands[0]
    assert "Kw7mRt2xPqL9sVbn" not in " ".join(command)
    assert command[0] == str(tmp_path / "imapsync")
    assert command[command.index("--host1") + 1] == "178.21.114.195"
    assert command[command.index("--host2") + 1] == "localhost"
    assert [command[command.index(flag) + 1] for flag in ("--user1", "--user2")] == ["old@a.nl"] * 2
    passfile = command[command.index("--passfile1") + 1]
    assert command[command.index("--passfile2") + 1] == passfile
    assert open(passfile).read() == "Kw7mRt2xPqL9sVbn\n"
    assert os.stat(passfile).st_mode & 0o777 == 0o600
    # Its own session, or it would be killed with the ssh connection the old server asked through.
    assert imapsync.named["start_new_session"] is True
    assert open(str(tmp_path / "mail" / "old@a.nl.pid")).read().strip() == "4321"
    assert "copying old@a.nl from 178.21.114.195" in open(str(tmp_path / "mail" / "old@a.nl.log")).read()


def test_a_mailbox_that_is_not_on_this_server_yet_is_not_copied_into(server, imapsync):
    """Copying into a mailbox that isn't here is a pile of errors and nothing else."""
    server.here = {}
    answer = _copy()
    assert answer["ok"] is False and "Move the mail of its domain first" in answer["error"]
    assert imapsync.commands == []


def test_a_copy_without_imapsync_on_the_server_says_to_run_the_playbook(server, monkeypatch, tmp_path):
    server.here = {"a.nl": ["old@a.nl"]}
    monkeypatch.setattr(migratectl, "IMAPSYNC", str(tmp_path / "no-imapsync-here"))
    answer = _copy()
    assert answer["ok"] is False and "playbook" in answer["error"]


def test_a_copy_that_is_already_running_is_not_started_a_second_time(server, imapsync, monkeypatch):
    server.here = {"a.nl": ["old@a.nl"]}
    assert _copy()["started"] is True
    monkeypatch.setattr(migratectl, "_running_copy", lambda pidfile: 4321)
    answer = _copy()
    assert answer["ok"] is True and answer["started"] is False and answer["running"] is True
    assert len(imapsync.commands) == 1


def test_a_copy_request_that_makes_no_sense_is_refused(server, imapsync):
    server.here = {"a.nl": ["old@a.nl"]}
    for request in ({"address": "not an address"}, {"password": ""}, {"password": "two\nlines"},
                    {"host": ""}, {"host": "not a host name"}):
        assert _copy(**request)["ok"] is False
    assert imapsync.commands == []


def test_the_status_of_a_copy_says_how_far_imapsync_got(imapsync, tmp_path):
    (tmp_path / "mail").mkdir()
    (tmp_path / "mail" / "old@a.nl.log").write_text(LOG_OF_A_FINISHED_COPY)
    passfile = tmp_path / "mail" / "old@a.nl.password"
    passfile.write_text("Kw7mRt2xPqL9sVbn\n")
    answer = migratectl.answer({"request": "copy_mail_status", "address": "old@a.nl"})
    assert answer["ok"] is True and answer["started"] is True and answer["running"] is False
    assert answer["copied"] == 2 and answer["transferred"] == 2 and answer["errors"] == 0
    assert answer["exit"] == 0 and answer["good"] is True
    assert answer["last"].startswith("Exiting with return value 0")
    assert "msg INBOX/2 copied" in answer["text"]
    # Only imapsync reads it, and only while it runs.
    assert not passfile.exists()


def test_the_status_of_a_mailbox_nothing_was_copied_for_says_so(imapsync):
    assert migratectl.answer({"request": "copy_mail_status", "address": "old@a.nl"}) == \
        {"ok": True, "started": False, "running": False}
    assert migratectl.answer({"request": "copy_mail_status", "address": "nonsense"})["ok"] is False


# ---- Counting a mailbox on both servers with imapsync ----


def _sizes_line(side, counter, folder, size=None, messages=None):
    """A line as imapsync prints it under --justfoldersizes."""
    held = (" does not exist" if size is None
            else "Size: %9s Messages: %5s Biggest: %9s" % (size, messages, size))
    return "Host%d folder %7s %-35s %s" % (side, counter, folder, held)


COUNTED = "\n".join([
    "Folders sizes before the synchronization. It can take some time. Be patient.",
    _sizes_line(1, "1/3", "INBOX", 3221225472, 2674),
    _sizes_line(2, "1/3", "INBOX", 3200000000, 2630),
    "Host2-Host1  1/3  INBOX                                  -21225472             -44          0",
    _sizes_line(1, "2/3", "INBOX.Verzonden items", 2048, 2),
    _sizes_line(2, "2/3", "INBOX.Sent", 2048, 2),
    _sizes_line(1, "3/3", "INBOX.Oud archief", 512, 1),
    _sizes_line(2, "3/3", "INBOX.Oud archief"),
    "",
    "Host1 Nb messages:                 2677 messages",
    "Host2 Nb messages:                 2632 messages",
    "Host1 Total size:            3221228032 bytes (3.000 GiB)",
    "Host2 Total size:            3200002048 bytes (2.980 GiB)",
    "Exiting because of --justfoldersizes",
])


class Counter(object):
    """A stand-in for imapsync counting: it says how it was run and gives back a listing. Everything else the
    server runs goes on to the stand-in that was there before it."""

    def __init__(self, inner, text=COUNTED, code=0):
        self.inner, self.commands, self.text, self.code = inner, [], text, code
        self.passwords = []

    def __call__(self, command, **named):
        if "--justfoldersizes" not in list(command):
            return self.inner(command, **named)
        self.commands.append(list(command))
        passfile = command[command.index("--passfile1") + 1]
        self.passwords.append(open(passfile).read())
        return types.SimpleNamespace(stdout=self.text.encode(), returncode=self.code)


@pytest.fixture
def counting(monkeypatch, tmp_path, server):
    binary = tmp_path / "imapsync"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(migratectl, "IMAPSYNC", str(binary))
    monkeypatch.setattr(migratectl, "COPY_ROOT", str(tmp_path / "mail"))
    counter = Counter(migratectl.subprocess.run)
    monkeypatch.setattr(migratectl.subprocess, "run", counter)
    return counter


# ---- Reading a customer's contacts and calendars back in --------------------

@pytest.fixture
def webmail(monkeypatch, tmp_path):
    """sogo-tool on this server, and the directory the old server's backups land in."""
    place = tmp_path / "webmail"
    place.mkdir()
    monkeypatch.setattr(migratectl, "WEBMAIL_ROOT", str(place))
    monkeypatch.setattr(migratectl.os, "access", lambda path, mode: True)
    runs = []
    others = migratectl.subprocess.run  # mailctl is asked whether the account is here; only sogo-tool is ours

    class Done(object):
        returncode = 0
        stdout = b""

    def run(command, **named):
        if not command or command[0] != migratectl.SOGO_TOOL:
            return others(command, **named)
        runs.append(list(command))
        return Done()

    monkeypatch.setattr(migratectl.subprocess, "run", run)
    return place, runs


def test_reading_webmail_back_in_does_the_settings_first_and_then_the_folders(server, webmail):
    """SOGo's own order: the preferences hold the signature and the filters, the folders hold the contacts."""
    place, runs = webmail
    server.here = {"a.nl": ["old@a.nl"]}
    (place / "old@a.nl").write_text("a backup")

    answer = migratectl.answer({"request": "restore_webmail", "addresses": ["Old@A.nl"]})

    assert answer["ok"] is True and answer["restored"] == ["old@a.nl"]
    assert [command[1:4] for command in runs] == [["restore", "-p", str(place)],
                                                  ["restore", "-f", "ALL"]]
    assert all(command[-1] == "old@a.nl" for command in runs)


def test_an_account_with_no_backup_is_said_so_and_not_run(server, webmail):
    _place, runs = webmail
    server.here = {"a.nl": ["old@a.nl"]}
    answer = migratectl.answer({"request": "restore_webmail", "addresses": ["old@a.nl"]})
    assert answer["ok"] is False and "no backup" in answer["error"]
    assert runs == []


def test_webmail_is_not_read_into_an_account_this_server_has_not_got(server, webmail):
    """It would be a pile of errors, and the old server is told to move the accounts first."""
    place, runs = webmail
    server.here = {}
    (place / "old@a.nl").write_text("a backup")
    answer = migratectl.answer({"request": "restore_webmail", "addresses": ["old@a.nl"]})
    assert answer["ok"] is False and "Move the mail of its domain first" in answer["error"]
    assert runs == []


def test_one_account_that_will_not_read_in_does_not_stop_the_others(server, webmail, monkeypatch):
    place, runs = webmail
    server.here = {"a.nl": ["one@a.nl", "two@a.nl"]}
    for address in ("one@a.nl", "two@a.nl"):
        (place / address).write_text("a backup")

    class Failed(object):
        returncode = 70
        stdout = b"<0x0[SOGoCache]> noise\nunknown user two@a.nl\n"

    class Done(object):
        returncode = 0
        stdout = b""

    others = migratectl.subprocess.run
    monkeypatch.setattr(migratectl.subprocess, "run",
                        lambda command, **named: others(command, **named)
                        if command[0] != migratectl.SOGO_TOOL
                        else (Failed() if "two@a.nl" in command else Done()))

    answer = migratectl.answer({"request": "restore_webmail", "addresses": ["one@a.nl", "two@a.nl"]})
    assert answer["restored"] == ["one@a.nl"]
    assert answer["ok"] is False and "unknown user two@a.nl" in answer["error"]


def test_where_the_backups_go_is_made_for_root_alone(server, webmail, tmp_path, monkeypatch):
    """A person's whole address book is in there."""
    place = tmp_path / "made"
    monkeypatch.setattr(migratectl, "WEBMAIL_ROOT", str(place))
    answer = migratectl.answer({"request": "webmail_place"})
    assert answer["ok"] is True and answer["place"] == str(place)
    assert oct(place.stat().st_mode)[-3:] == "700"


def test_a_server_without_webmail_says_so_instead_of_trying(server, monkeypatch):
    monkeypatch.setattr(migratectl.os, "access", lambda path, mode: False)
    assert migratectl.answer({"request": "webmail_place"})["ok"] is False
    assert "no webmail" in migratectl.answer({"request": "webmail_place"})["error"]


def test_a_request_to_read_webmail_in_without_addresses_is_refused(server, webmail):
    _place, runs = webmail
    assert migratectl.answer({"request": "restore_webmail"})["ok"] is False
    assert migratectl.answer({"request": "restore_webmail", "addresses": []})["ok"] is False
    assert runs == []


def _compare(**more):
    return migratectl.answer(dict({"request": "compare_mail", "address": "Old@A.nl",
                                   "password": "Kw7mRt2xPqL9sVbn", "host": "178.21.114.195"}, **more))


def test_counting_a_mailbox_asks_imapsync_itself_what_each_folder_holds(server, counting, tmp_path):
    """The same program that copies, reading the same two mailboxes: what it counts is what it would copy."""
    server.here = {"a.nl": ["old@a.nl"]}
    answer = _compare()

    command = counting.commands[0]
    assert "--justfoldersizes" in command and "--automap" in command
    assert "Kw7mRt2xPqL9sVbn" not in " ".join(command)
    assert counting.passwords == ["Kw7mRt2xPqL9sVbn\n"]
    assert answer["ok"] is True
    assert answer["here"] == {"bytes": 3221228032, "messages": 2677}
    assert answer["there"] == {"bytes": 3200002048, "messages": 2632}
    assert [folder["name"] for folder in answer["folders"]] == \
        ["INBOX", "INBOX.Verzonden items", "INBOX.Oud archief"]
    inbox = answer["folders"][0]
    assert inbox["here"] == {"bytes": 3221225472, "messages": 2674}
    assert inbox["there"] == {"bytes": 3200000000, "messages": 2630}
    # --automap gives a folder another name on this server; the two are paired by the number in front of them.
    assert answer["folders"][1]["name_there"] == "INBOX.Sent"
    # A folder that isn't on this server at all says so, instead of counting as empty.
    assert answer["folders"][2]["there"] is None


def test_counting_and_copying_both_map_the_folders_under_inbox(server, counting):
    """A mailbox moved here from an older server has its sent mail in INBOX.Sent as well as in Sent, and both
    names open the one folder there. --automap doesn't know that name, so without this imapsync makes an
    INBOX.Sent here too and the mailbox keeps its sent mail twice. The counting carries the same mapping, so
    it pairs up the folders the copy would really write into."""
    server.here = {"a.nl": ["old@a.nl"]}
    _compare()
    command = counting.commands[0]
    assert "--regextrans2" in command
    mapping = command[command.index("--regextrans2") + 1]
    assert mapping == migratectl.UNDER_INBOX
    assert mapping.startswith("s,^INBOX\\.(Sent|")
    # Only the folders both servers have a name of their own for lose the prefix: anything else under INBOX is
    # the only copy of that mail there is, and belongs where the customer keeps it.
    for name in ("Sent", "Verzonden items", "Trash", "Drafts", "Junk", "Spam"):
        assert "|%s|" % name in mapping or "(%s|" % name in mapping or "|%s)" % name in mapping
    assert migratectl.UNDER_INBOX in migratectl._imapsync_command("old@a.nl", "old.example", "/x/pass")


def test_the_password_of_a_counted_mailbox_is_not_left_behind(server, counting, tmp_path):
    server.here = {"a.nl": ["old@a.nl"]}
    _compare()
    assert not (tmp_path / "mail" / "old@a.nl.password").exists()


def test_counting_a_mailbox_copies_nothing(server, counting):
    """It is the one thing on that menu that changes nothing, so it must not start a copy."""
    server.here = {"a.nl": ["old@a.nl"]}
    _compare()
    assert all("--justfoldersizes" in command for command in counting.commands)
    assert not any("--dry" in command for command in counting.commands)


def test_a_mailbox_that_imapsync_could_not_open_says_what_it_said(server, counting):
    server.here = {"a.nl": ["old@a.nl"]}
    counting.text = ("Host1 failure: Error login on [178.21.114.195] with user [old@a.nl]: "
                     "AUTHENTICATIONFAILED\nExiting with return value 8")
    counting.code = 8
    answer = _compare()
    assert answer["ok"] is False and "AUTHENTICATIONFAILED" in answer["error"]


def test_a_mailbox_that_is_being_copied_right_now_is_not_counted(server, counting, monkeypatch):
    """What it held a moment ago is worse than no answer: it reads as mail that is missing."""
    server.here = {"a.nl": ["old@a.nl"]}
    monkeypatch.setattr(migratectl, "_running_copy", lambda pidfile: 4321)
    answer = _compare()
    assert answer["ok"] is False and "being copied" in answer["error"]
    assert counting.commands == []


def test_counting_a_mailbox_that_is_not_on_this_server_says_to_move_the_mail_first(server, counting):
    server.here = {}
    answer = _compare()
    assert answer["ok"] is False and "Move the mail of its domain first" in answer["error"]
    assert counting.commands == []


def test_a_counting_request_that_makes_no_sense_is_refused(server, counting):
    server.here = {"a.nl": ["old@a.nl"]}
    for request in ({"address": "not an address"}, {"password": ""}, {"password": "two\nlines"},
                    {"host": ""}, {"host": "not a host name"}):
        assert _compare(**request)["ok"] is False
    assert counting.commands == []

# ---- How much mail the mailboxes here hold ----

@pytest.fixture
def vmail(monkeypatch, tmp_path):
    """The mailboxes of this server, in a place of their own."""
    monkeypatch.setattr(migratectl, "VMAIL", str(tmp_path / "vmail"))
    return tmp_path / "vmail"


def _mailbox(root, address, cur=(), new=(), tmp=(), beside=()):
    """A maildir as dovecot keeps it: the messages in cur and new, half-delivered ones in tmp, and dovecot's
    own indexes beside them."""
    local_part, _, domain = address.partition("@")
    maildir = root / domain / local_part / "Maildir"
    for name, sizes in (("cur", cur), ("new", new), ("tmp", tmp)):
        folder = maildir / name
        folder.mkdir(parents=True)
        for number, size in enumerate(sizes):
            (folder / ("%d.M1P1.here,S=%d" % (number, size))).write_bytes(b"x" * size)
    for name, size in beside:
        (maildir / name).write_bytes(b"x" * size)


def test_how_much_a_mailbox_holds_is_its_messages_and_not_this_servers_own_indexes(vmail):
    _mailbox(vmail, "old@a.nl", cur=[100, 200], new=[50], tmp=[9999],
             beside=[("dovecot.index.cache", 50000), ("dovecot-uidlist", 80)])

    answer = migratectl.answer({"request": "mail_sizes", "addresses": ["Old@A.nl"]})

    assert answer == {"ok": True, "mailboxes": {"old@a.nl": {"here": True, "bytes": 350, "messages": 3}}}


def test_an_account_without_a_mailbox_here_is_said_to_have_none(vmail):
    """Not "nothing": a mailbox of nothing and no mailbox at all are different answers to the old server."""
    _mailbox(vmail, "old@a.nl")

    answer = migratectl.answer({"request": "mail_sizes", "addresses": ["old@a.nl", "gone@a.nl", "nonsense"]})

    assert answer == {"ok": True, "mailboxes": {"old@a.nl": {"here": True, "bytes": 0, "messages": 0},
                                                "gone@a.nl": {"here": False}}}


def test_measuring_needs_a_list_of_addresses_and_not_an_endless_one(vmail):
    assert migratectl.answer({"request": "mail_sizes"})["ok"] is False
    too_many = ["a%d@a.nl" % number for number in range(migratectl.MAX_MAILBOXES + 1)]
    assert migratectl.answer({"request": "mail_sizes", "addresses": too_many})["ok"] is False


# ---- Moving a site's DNS, and whether it really moved ----

def _domainctl_add(monkeypatch, text="✓ example.nl is served from /home/example/public_html."):
    ran = []
    monkeypatch.setattr(migratectl, "_run", lambda command, what, **named: ran.append(command) or text)
    monkeypatch.setattr(migratectl, "_addresses", lambda: ["187.126.5.158", "2a02:4780:7e:6327::1"])
    return ran


def _resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(migratectl.socket, "getaddrinfo",
                        lambda *args, **named: [(0, 0, 0, "", (address, 0)) for address in addresses])


def test_point_dns_moves_the_dns_and_says_so(monkeypatch):
    ran = _domainctl_add(monkeypatch)
    _resolves_to(monkeypatch, "187.126.5.158")

    answer = migratectl.answer({"request": "point_dns", "domain": "Example.NL"})

    assert ran == [["domainctl", "add", "example.nl", "--yes"]]
    assert answer["ok"] is True
    assert answer["steps"] == ["Moved the DNS of example.nl to this server and finished the site."]


def test_point_dns_says_no_when_the_name_still_goes_to_the_old_server(monkeypatch):
    """domainctl sets the site up whatever the DNS says, and a domain whose records it can't change gets a
    certificate signed here. The old server asked for the DNS to move, so that is not a yes."""
    _domainctl_add(monkeypatch, text="! Couldn't change example.nl: example.nl isn't in the TransIP account "
                                     "majorlabel.\n✓ example.nl is set up and served from /home/example.")
    _resolves_to(monkeypatch, "178.21.114.195")

    answer = migratectl.answer({"request": "point_dns", "domain": "example.nl"})

    assert answer["ok"] is False
    assert "still doesn't point at this server" in answer["error"]
    assert "isn't in the TransIP account" in answer["text"]


def test_a_move_domainctl_itself_checked_is_not_called_a_failure_by_a_stale_cache(monkeypatch):
    """domainctl waits for the zone's own name servers before it says this, and Let's Encrypt only signs once
    the record is really out there. This machine's resolver may still hold the record as it was, with the time
    to live it was published with, and on its own it called a move that had just worked a failure."""
    _domainctl_add(monkeypatch, text="Took away the A record of example.nl: 178.21.114.195\n"
                                     "✓ Published 1 record(s) for example.nl in the zone example.nl.\n"
                                     "✓ example.nl points to this server.\n"
                                     "✓ https://example.nl is live with a Let's Encrypt certificate.")
    _resolves_to(monkeypatch, "178.21.114.195")  # the old address, still in this machine's cache

    answer = migratectl.answer({"request": "point_dns", "domain": "example.nl"})

    assert answer["ok"] is True
    assert answer["steps"] == ["Moved the DNS of example.nl to this server and finished the site."]


def test_point_dns_claims_nothing_when_the_name_cannot_be_looked_up(monkeypatch):
    _domainctl_add(monkeypatch)

    def no_answer(*args, **named):
        raise migratectl.socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(migratectl.socket, "getaddrinfo", no_answer)

    assert migratectl.answer({"request": "point_dns", "domain": "example.nl"})["ok"] is True


def test_a_linux_user_that_is_already_here_asks_before_the_site_is_made(machine, monkeypatch):
    """domainctl won't take over a user unless it is told to, so that a site never lands in somebody else's
    home directory by accident. Whether it may is the old server's question to answer."""
    monkeypatch.setattr(migratectl, "_user_taken", lambda user: user == "example")

    answer = migratectl.answer(APPLY_SITE)

    assert answer["ok"] is False and answer["ask"]["about"] == "user"
    assert "already on this server" in answer["ask"]["question"]
    assert machine.ran("domainctl") == []


def test_saying_yes_to_that_user_takes_it_over(machine, monkeypatch):
    monkeypatch.setattr(migratectl, "_user_taken", lambda user: user == "example")

    answer = migratectl.answer({**APPLY_SITE, "answers": {"user": "yes"}})

    assert answer["ok"] is True, answer
    assert ["domainctl", "add", "example.nl", "--user", "example", "--no-dns", "--yes",
            "--existing-user"] in machine.calls


# ---- A site that is nothing but a redirect ----

REDIRECT_RULES = ["RewriteCond %{HTTP_HOST} ^www\\.oud\\.nl$ [NC]",
                  "RewriteRule ^(.*)$ https://nieuw.nl/$1 [R=301,L]"]
APPLY_REDIRECT = {"request": "apply",
                  "item": {"id": "site:oud.nl", "kind": "site", "name": "oud.nl", "user": "oud",
                           "database": None, "redirect": {"to": "https://nieuw.nl/", "rules": REDIRECT_RULES,
                                                          "by_hand": []}}}


def test_a_redirect_is_made_as_a_site_with_its_rules_in_its_own_htaccess(machine):
    """Every site here is an instance of one shared OpenLiteSpeed template, which nothing may change per site.
    The template loads each site's .htaccess, so that is where a redirect goes."""
    answer = migratectl.answer(APPLY_REDIRECT)

    assert answer["ok"] is True and answer["redirect"] is True, answer
    assert ["domainctl", "add", "oud.nl", "--user", "oud", "--no-dns", "--yes"] in machine.calls
    assert machine.ran("mariadb") == [] and not (machine.homes / "oud" / "public_html" / "www" /
                                                 "wp-config.php").exists()
    written = (machine.homes / "oud" / "public_html" / "www" / ".htaccess").read_text()
    assert written.splitlines()[-2:] == REDIRECT_RULES
    assert "RewriteEngine On" in written and "autoLoadHtaccess" in written


def test_the_rules_it_writes_are_redirect_rules_and_nothing_else(machine):
    """They go into a file this server reads as configuration, so nothing else is ever written into it."""
    for rules in (["php -r 'unlink(\"/etc/passwd\");'"], ["Options +Indexes"],
                  ["RewriteRule a b\nInclude /etc/shadow"], ["RewriteRule " + "x" * 2100],
                  [None], ["RewriteRule a b"] * (migratectl.MAX_RULES + 1)):
        item = dict(APPLY_REDIRECT["item"], redirect={"rules": rules})
        answer = migratectl.answer({"request": "apply", "item": item})
        assert answer["ok"] is False and "redirect" in answer["error"], rules
    assert machine.calls == []


def test_a_site_with_files_of_its_own_is_still_asked_about_and_a_redirect_is_not(machine):
    """A redirect brings no files, so files already in the web root are not in its way."""
    root = machine.homes / "oud" / "public_html" / "www"
    root.mkdir(parents=True)
    (root / "index.php").write_text("<?php")

    assert migratectl.answer(APPLY_REDIRECT)["ok"] is True


# ---- A name that is a piSignage server, not a site ----

def _signage_item(**more):
    return dict({"id": "site:andre.milocast.nl", "kind": "site", "name": "andre.milocast.nl",
                 "user": "andremilocast", "signage": 3002}, **more)


@pytest.fixture
def signage_server(server, monkeypatch):
    """The new server with domainctl installed, so a site item is judged and not refused out of hand."""
    monkeypatch.setattr(migratectl.shutil, "which", lambda name: "/usr/local/bin/" + name)
    return server


def _state(item):
    return migratectl.answer({"request": "status", "items": [item]})["items"][item["id"]]


def test_a_piSignage_name_is_judged_by_the_list_of_signage_servers_not_of_sites(signage_server):
    """It has a virtual host of its own, so it is in neither of the other lists; asking for it as a site would
    say the user already has one here and refuse it."""
    assert _state(_signage_item())["state"] == "new"
    signage_server.signage = [("andremilocast", "andre.milocast.nl", "3002")]
    answer = _state(_signage_item())
    assert answer["state"] == "exists" and "already here" in answer["detail"]
    assert ["domainctl", "list"] not in signage_server.calls


def test_a_piSignage_server_under_another_user_name_is_still_found(signage_server):
    """What counts is the name it serves: the user the old server would pick may not be the one it has."""
    signage_server.signage = [("milocast", "andre.milocast.nl", "3002")]
    assert _state(_signage_item())["state"] == "exists"


def test_a_list_that_cannot_be_read_is_not_taken_for_an_empty_one(signage_server):
    """"Nothing there" would have the old server set up a second one over the first."""
    signage_server.break_lists = True
    answer = _state(_signage_item())
    assert answer["state"] == "refused" and "piSignage servers" in answer["detail"]


# ---- What the last copy of a mailbox really did ----

OLD_RUN = """
==== 2026-10-28 07:20:00: copying old@a.nl from 178.21.114.195 ====
msg INBOX/1 copied to INBOX/1
Messages transferred                    : 1
Messages found in host1 not in host2    : 0 messages
The sync looks good, all 1 identified messages in host1 are on host2.
Detected 0 errors
Exiting with return value 0 (EX_OK: successful termination)
"""
NEW_RUN = """
==== 2026-10-29 08:47:15: copying old@a.nl from 178.21.114.195 ====
Messages transferred                    : 0
Messages skipped                        : 9271
Messages found in host1 not in host2    : 0 messages
The sync looks good, all 8817 identified messages in host1 are on host2.
Detected 0 errors
Exiting with return value 0 (EX_OK: successful termination)
"""


def _logged(tmp_path, text):
    (tmp_path / "mail").mkdir(exist_ok=True)
    (tmp_path / "mail" / "old@a.nl.log").write_text(text)


def test_the_status_counts_this_copy_and_not_every_copy_there_has_ever_been(imapsync, tmp_path):
    """The log is appended to. Counting over all of it showed the numbers of a copy that ran days ago as if
    they were this one's, so a copy that moved nothing looked like one that moved thousands."""
    _logged(tmp_path, OLD_RUN + NEW_RUN)
    answer = migratectl.answer({"request": "copy_mail_status", "address": "old@a.nl"})
    assert answer["copied"] == 0 and answer["transferred"] == 0
    assert answer["good"] is True and answer["left"] == 0
    assert "8817 identified messages" in answer["text"]


def test_the_sizes_carry_what_imapsync_said_about_the_mailbox(imapsync, tmp_path, monkeypatch):
    """The old server counts the files in a maildir, which are not the messages. This is the answer of the
    program that opened both mailboxes."""
    _logged(tmp_path, OLD_RUN + NEW_RUN)
    monkeypatch.setattr(migratectl, "VMAIL", str(tmp_path / "vmail"))
    answer = migratectl.answer({"request": "mail_sizes", "addresses": ["old@a.nl"]})
    sync = answer["mailboxes"]["old@a.nl"]["sync"]
    assert sync["good"] is True and sync["left"] == 0 and sync["transferred"] == 0 and sync["errors"] == 0
    # How long ago it ran, never when: the two servers' clocks are their own.
    assert "at" not in sync and 0 <= sync["ago"] < 60


def test_a_copy_that_is_still_running_says_nothing_about_whether_it_is_across(imapsync, tmp_path, monkeypatch):
    """Half a run is not an answer: it would read as "nothing is missing" before anything was copied."""
    _logged(tmp_path, OLD_RUN + "\n==== 2026-10-29 09:00:00: copying old@a.nl from 178.21.114.195 ====\n"
                                "Host1: connecting\n")
    monkeypatch.setattr(migratectl, "VMAIL", str(tmp_path / "vmail"))
    answer = migratectl.answer({"request": "mail_sizes", "addresses": ["old@a.nl"]})
    assert "sync" not in answer["mailboxes"]["old@a.nl"]


# ---- A database for a site that is not WordPress ----

def _adopt(**more):
    item = {"kind": "site", "name": "boersma-advocaten.nl", "user": "boersmaadvocaten",
            "database": more.pop("database", "boersma")}
    return migratectl.answer(dict({"request": "adopt_database", "item": item, "dump": "boersma.sql"}, **more))


@pytest.fixture
def arrived(server, monkeypatch, tmp_path):
    """A site on this server whose files, the dump among them, have just arrived."""
    monkeypatch.setattr(migratectl, "HOMES", str(tmp_path))
    docroot = tmp_path / "boersmaadvocaten" / migratectl.WEB_ROOT
    docroot.mkdir(parents=True)
    (docroot / "boersma.sql").write_text("CREATE TABLE t (id int);")
    monkeypatch.setattr(migratectl, "_database_taken", lambda name: False)
    return docroot


def test_the_database_is_made_from_the_dump_that_came_with_the_files(arrived, server):
    answer = _adopt()

    assert answer["ok"] is True
    call = next(one for one in server.calls if one[:3] == ["domainctl", "database", "add"])
    assert call[3] == "boersmaadvocaten"
    assert call[call.index("--database") + 1] == "boersma"
    assert call[call.index("--dump") + 1] == str(arrived / "boersma.sql")
    # The password is made here and given back: this server can't write it into a site it can't read.
    assert call[call.index("--password") + 1] == answer["password"]
    assert len(answer["password"]) > 20 and answer["user"] == "boersma" and answer["host"] == "localhost"


def test_a_dump_that_is_not_there_says_the_files_have_to_come_first(arrived, server):
    (arrived / "boersma.sql").unlink()
    answer = _adopt()
    assert answer["ok"] is False and "copied over first" in answer["error"]
    assert not [one for one in server.calls if one[:2] == ["domainctl", "database"]]


def test_the_dump_is_a_file_name_and_never_a_path(arrived, server):
    """The old server names a file in the web root; nothing it sends can point at a file somewhere else."""
    for name in ("../../../etc/shadow", "/etc/passwd", "boersma.sql; rm -rf /", "boersma.txt"):
        answer = _adopt(dump=name)
        assert answer["ok"] is False and "isn't a name of a dump" in answer["error"]
    assert not [one for one in server.calls if one[:2] == ["domainctl", "database"]]


def test_a_database_that_is_already_there_is_asked_about(arrived, server, monkeypatch):
    monkeypatch.setattr(migratectl, "_database_taken", lambda name: name == "boersma")
    monkeypatch.setattr(migratectl, "_free_database", lambda name: "boersma_2")
    answer = _adopt()
    assert answer["ok"] is False and answer["ask"]["about"] == "database"
    assert "boersma_2 is free" in answer["ask"]["question"]
    assert _adopt(replace=True)["ok"] is True


def test_a_site_without_a_database_name_is_refused(arrived, server):
    assert _adopt(database=None)["ok"] is False
