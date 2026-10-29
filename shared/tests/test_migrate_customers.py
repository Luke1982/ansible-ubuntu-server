"""The screen the tool is worked from: one customer, one number, one next step.

The per-item screen that `migrate.py list` prints (items_board) has its own tests in
test_migrate_dashboard.py; these are about the customers, the four words their columns say, what a number does,
and the new mail passwords.
"""

import os
import time
import re

import pytest

from migrate import migrate


@pytest.fixture(autouse=True)
def notes_in_a_place_of_their_own(monkeypatch, tmp_path):
    """No test writes the real /var/lib/migrate/state.json."""
    monkeypatch.setattr(migrate, "STATE_PATH", str(tmp_path / "state.json"))


def _notes(tmp_path, **items):
    state = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    state.items = dict(items)
    return state


def _mail(domain, accounts=1, forwards=0):
    return {"id": "mail:" + domain, "kind": "mail_domain", "name": domain,
            "data": {"addresses": [{"address": "a%d@%s" % (n, domain)} for n in range(accounts)],
                     "forwards": [{"source": "f@" + domain}] * forwards}}


def _site(name, size=None, database="db1", **more):
    return dict({"id": "site:" + name, "kind": "site", "name": name, "user": name.split(".")[0],
                 "database": database, "size": size, "wordpress": True,
                 "docroot": "/var/www/" + name, "problem": None}, **more)


def _cron(name, site="keep", **more):
    site = name if site == "keep" else site
    return dict({"id": "cron:" + name, "kind": "cron", "user": name.split(".")[0], "old_user": "www",
                 "site": site, "line": "*/15 * * * * php /var/www/%s/wp-cron.php" % name,
                 "problem": None}, **more)


def _plain(name):
    return _site(name, wordpress=False, database=None,
                 problem=migrate.NOT_WORDPRESS % ("/var/www/" + name))


def _status(**states):
    return dict((key, {"state": state, "detail": "%s: %s" % (key, state)}) for key, state in states.items())


def _there(domain, accounts=1, forwards=0):
    return {"state": "exists",
            "detail": "%s is already here, with %d accounts and %d forwards." % (domain, accounts, forwards)}


def _one_customer():
    """A customer with all three parts on this server, and nothing of it on the new one."""
    return [_mail("example.nl", accounts=2), _site("example.nl", 2300000), _cron("example.nl")]


def _row(text, name):
    return [line for line in text.splitlines() if name in line][0]


def _cells(text, name):
    """One row of the screen, as its columns: they are two spaces apart or more."""
    return [cell for cell in re.split(r"\s{2,}", _row(text, name).strip()) if cell]


class Connection(object):
    def __init__(self, status=None):
        self.requests, self.status = [], status or {}

    def ask(self, request):
        self.requests.append(dict(request))
        if request["request"] == "status":
            return {"ok": True, "items": dict((item["id"], self.status.get(item["id"])
                                               or {"state": "new", "detail": "not here"})
                                              for item in request["items"])}
        return {"ok": True}


# ---- the rows ---------------------------------------------------------------

def test_one_row_for_each_customer_and_none_for_anything_else():
    """A website this tool can't move is still a website on this server, so it has a row saying so. What
    belongs to no customer at all - the server's own cron jobs, its webmail names - has none."""
    items = _one_customer() + [_mail("other.nl"), _plain("oude-site.nl"),
                               _site("no-root.nl", database=None, docroot=None, wordpress=False,
                                     problem="Its configuration has no DocumentRoot, so its files "
                                             "can't be found."),
                               _cron("root", site=None, problem=migrate.NO_SITE_JOB),
                               {"id": "aside:webmail.a.nl", "kind": "aside", "name": "webmail.a.nl"}]
    status = _status(**dict((item["id"], "new") for item in items if item["kind"] != "aside"))
    rows = migrate.customers(items, status)
    assert [(row.name, row.number) for row in rows] == [
        ("example.nl", 1), ("no-root.nl", 2), ("other.nl", 3), ("oude-site.nl", 4)]
    text = migrate.dashboard(items, status, old_server="vps1157", new_server="web03.majorlabel.nl")
    assert _cells(text, "oude-site.nl") == ["4", "oude-site.nl", "-", "by hand", "-", "by hand"]
    for left_out in ("webmail.a.nl", "/usr/lib", "wp-cron"):
        assert left_out not in text
    assert text.splitlines()[0] == "Old server  vps1157      New server  web03.majorlabel.nl"
    head = [line for line in text.splitlines() if line.startswith("  #")][0]
    assert head.split() == ["#", "Customer", "Mail", "Website", "Cron", "Next", "step"]


def test_a_sites_subdomain_and_its_mail_are_one_customer():
    items = [_mail("example.nl"), _site("shop.example.nl"), _cron("shop.example.nl")]
    rows = migrate.customers(items, _status(**{"mail:example.nl": "new", "site:shop.example.nl": "new"}))
    assert [row.name for row in rows] == ["example.nl"]
    assert [item["id"] for item in rows[0].every()] == [
        "mail:example.nl", "site:shop.example.nl", "cron:shop.example.nl"]


def test_every_column_says_one_of_five_words_and_nothing_else(tmp_path):
    items = [_mail("one.nl"), _site("one.nl", 10), _cron("one.nl"),
             _mail("two.nl", accounts=2), _site("two.nl", 10), _mail("three.nl"), _plain("three.nl")]
    notes = _notes(tmp_path, **{"site:one.nl": {"status": "made", "docroot": "/home/one"}})
    status = dict(_status(**{"site:one.nl": "exists", "site:two.nl": "new", "mail:three.nl": "new"}),
                  **{"mail:one.nl": _there("one.nl"), "mail:two.nl": _there("two.nl", accounts=1)})
    text = migrate.dashboard(items, status, notes, new_server="web03.majorlabel.nl")
    assert _cells(text, "one.nl") == ["1", "one.nl", "done", "part", "to do", "finish the website"]
    assert _cells(text, "three.nl") == ["2", "three.nl", "to do", "by hand", "-", "move the mail"]
    assert _cells(text, "two.nl") == ["3", "two.nl", "part", "to do", "-", "finish the mail"]
    words = set()
    for row in text.splitlines():
        cells = [cell for cell in re.split(r"\s{2,}", row.strip()) if cell]
        if cells and cells[0].isdigit():
            words.update(cells[2:-1])
    assert words <= {"to do", "part", "done", "-", "by hand"}


def test_a_column_with_nothing_to_say_is_left_out():
    items = [_mail("one.nl"), _mail("two.nl")]
    text = migrate.dashboard(items, _status(**{"mail:one.nl": "new", "mail:two.nl": "new"}))
    head = [line for line in text.splitlines() if line.startswith("  #")][0]
    assert head.split() == ["#", "Customer", "Mail", "Next", "step"]


@pytest.mark.parametrize("items, status, step", [
    ([_mail("a.nl"), _site("a.nl", 10), _cron("a.nl")], {}, "move everything"),
    ([_mail("a.nl")], {}, "move the mail"),
    ([_site("a.nl", 10)], {}, "copy the website"),
    ([_cron("a.nl"), _site("a.nl", 10)], {"site:a.nl": {"state": "exists", "detail": "here"}},
     "move the cron job"),
])
def test_the_next_step_says_what_the_number_will_do(items, status, step):
    rows = migrate.customers(items, status)
    assert migrate.next_step(rows[0])[1] == step
    assert _cells(migrate.dashboard(items, status), "a.nl")[-1] == step


def test_a_customer_with_everything_done_says_nothing_left_and_counts_as_done(tmp_path):
    items = [_mail("a.nl"), _site("a.nl", 10), _mail("b.nl")]
    notes = _notes(tmp_path, **{"site:a.nl": {"status": "moved", "docroot": "/home/a"}})
    status = dict(_status(**{"site:a.nl": "exists", "mail:b.nl": "new"}),
                  **{"mail:a.nl": _there("a.nl")})
    text = migrate.dashboard(items, status, notes)
    assert _cells(text, "a.nl") == ["1", "a.nl", "done", "done", "nothing to move"]
    assert text.splitlines()[-1] == "2 customers, 1 done, 1 to go."


def test_the_summary_counts_agree_with_the_rows():
    items = []
    for number in range(9):
        items.append(_mail("c%d.nl" % number))
    text = migrate.dashboard(items, _status(**dict((item["id"], "new") for item in items)))
    rows = [line for line in text.splitlines() if line.startswith("  ") and line.strip()[:1].isdigit()]
    assert len(rows) == 9
    assert text.splitlines()[-1] == "9 customers, 0 done, 9 to go."


def test_what_is_only_reported_is_one_sentence_of_counts():
    items = [_mail("a.nl")]
    items += [_plain("quiet-%d.nl" % n) for n in range(9)]
    items += [_site("c%d.nl" % n, wordpress=False, database=None,
                    problem=migrate.NOT_WORDPRESS % "/x") for n in range(6)]
    items += [_cron("loose-%d" % n, site=None, problem=migrate.NO_SITE_JOB) for n in range(16)]
    items += [{"id": "aside:w%d.nl" % n, "kind": "aside", "name": "w%d.nl" % n} for n in range(28)]
    status = _status(**dict((item["id"], "new") for item in items if item["kind"] != "aside"))
    text = migrate.dashboard(items, status, databases=["db%d" % n for n in range(18)])
    assert ("Also, only reported: 16 cron jobs of this server itself, 28 webmail names, "
            "18 databases that belong to no site. See: migrate.py list" in " ".join(text.split()))
    # The websites that aren't WordPress are not counted here any more: each has a row of its own now, saying
    # there is something left to do by hand.
    assert _cells(text, "c0.nl")[-1] == "by hand"
    assert text.splitlines()[-1] == "16 customers, 0 done, 16 to go."


def test_the_longest_real_names_still_fit_eighty_columns():
    names = ["autosleutels-west-betuwe.nl", "dakdekkersbedrijf-vanderheijden.nl",
             "uitvaartverzorgingdevriesenzonen.nl", "a" * 60 + ".example.com"]
    items, status = [], {}
    for name in names:
        items += [_mail(name, accounts=12), _site(name, 9200000), _cron(name)]
        status.update(_status(**{"mail:" + name: "new", "site:" + name: "new"}))
    text = migrate.dashboard(items, status, old_server="o" * 90,
                             new_server="web03.majorlabel.nl", addresses=["187.126.5.158"])
    assert all(len(line) <= 79 for line in text.splitlines())
    for name in names[:3]:
        assert name[-8:] in text  # cut in the middle, so the end of every name still shows


# ---- inside one customer ----------------------------------------------------

def test_inside_a_customer_is_sentences_with_no_numbers_and_no_tables():
    items = _one_customer()
    status = dict(_status(**{"site:example.nl": "new"}), **{"mail:example.nl": _there("example.nl", 1)})
    rows = migrate.customers(items, status)
    text = migrate.inside(rows[0], status, new_server="web03.majorlabel.nl")
    lines = text.splitlines()
    assert lines[0] == "example.nl"
    assert lines[2].startswith("  Mail       2 accounts")
    assert "On web03 with 1 account and 0 forwards so far" in " ".join(text.split())
    assert "  Website    WordPress, 2.3 MB plus its files. Not on web03 yet." in lines
    assert "every 15 minutes" in text and "wp-cron.php" in text
    assert lines[-1] == "Next step: finish the mail. Type 1 to do that."
    assert "No." not in text and "MAIL" not in text and "WEBSITES" not in text
    assert not any(re.match(r"\s*\d", line) for line in lines)  # no numbers of its own
    assert all(len(line) <= 79 for line in lines)


def test_inside_says_in_the_same_place_why_a_part_is_not_moved():
    items = [_mail("a.nl"), _plain("a.nl"), _cron("a.nl", problem=migrate.NO_SITE_JOB)]
    status = _status(**{"mail:a.nl": "new", "site:a.nl": "new", "cron:a.nl": "new"})
    row = migrate.customers(items, status)[0]
    text = " ".join(migrate.inside(row, status).split())
    assert "Website Not a WordPress site" in text or "Website No wp-config.php" in text
    assert "copy it to the new server by hand" in text.lower() or "by hand" in text
    assert "belongs to no site" in text
    assert "Next step: move the mail. Type 1 to do that." in text


def test_a_customer_with_nothing_left_to_move_does_not_claim_the_move_is_over(tmp_path):
    """Its DNS may still point here and its messages may still be here: neither is an item, so neither is a
    next step, and "nothing left to do" over a domain the world still comes here for is a lie."""
    items = [_mail("a.nl")]
    notes = _notes(tmp_path, **{"mail:a.nl": {"status": "moved"}})
    status = {"mail:a.nl": _there("a.nl")}
    row = migrate.customers(items, status, notes)[0]

    said = " ".join(" ".join(migrate.inside(row, status, notes).split()).split())

    assert "Nothing left of this customer to move." in said
    assert "in the list that t prints" in said
    assert migrate.next_step(row) == ([], "nothing to move")


# ---- what a number does -----------------------------------------------------

def test_a_bare_number_does_exactly_one_step():
    items = _one_customer()
    status = _status(**{"mail:example.nl": "new", "site:example.nl": "new"})
    rows = migrate.customers(items, status)
    # nothing of it is on the new server, so its one step is everything, in the order that works
    chosen = migrate.picked(rows, "1", say=lambda text: None)
    assert [item["kind"] for item in chosen.move] == ["mail_domain", "site", "cron"]
    assert chosen.look == [] and chosen.passwords == []
    # with its mail there, the number does the next step only
    status["mail:example.nl"] = _there("example.nl", 2)
    rows = migrate.customers(items, status)
    assert [item["kind"] for item in migrate.picked(rows, "1", say=print).move] == ["site"]


def test_a_number_with_all_does_every_step_that_customer_has_left():
    items = _one_customer()
    status = dict(_status(**{"site:example.nl": "new"}), **{"mail:example.nl": _there("example.nl", 1)})
    rows = migrate.customers(items, status)
    chosen = migrate.picked(rows, "1 all", say=lambda text: None)
    assert [item["kind"] for item in chosen.move] == ["mail_domain", "site", "cron"]


@pytest.mark.parametrize("word", ["a", "all"])
def test_a_and_all_do_the_next_step_of_every_customer(word):
    items = _one_customer() + [_mail("other.nl"), _site("third.nl", 10)]
    status = _status(**{"mail:example.nl": "new", "site:example.nl": "new", "mail:other.nl": "new",
                        "site:third.nl": "new"})
    rows = migrate.customers(items, status)
    chosen = migrate.picked(rows, word, say=lambda text: None)
    assert [item["id"] for item in chosen.move] == [
        "mail:example.nl", "site:example.nl", "cron:example.nl", "mail:other.nl", "site:third.nl"]


def test_a_part_by_name_does_that_part_alone():
    items = _one_customer()
    status = _status(**{"mail:example.nl": "new", "site:example.nl": "new"})
    rows = migrate.customers(items, status)
    for word, kinds in (("mail", ["mail_domain"]), ("site", ["site"]), ("cron", ["cron"])):
        chosen = migrate.picked(rows, "1 " + word, say=lambda text: None)
        assert [item["kind"] for item in chosen.move] == kinds


def test_ranges_and_commas_are_the_next_step_of_each():
    items = [_mail("a.nl"), _mail("b.nl"), _mail("c.nl"), _mail("d.nl")]
    status = _status(**dict((item["id"], "new") for item in items))
    rows = migrate.customers(items, status)
    assert [i["id"] for i in migrate.picked(rows, "2-3", say=print).move] == ["mail:b.nl", "mail:c.nl"]
    assert [i["id"] for i in migrate.picked(rows, "1,4", say=print).move] == ["mail:a.nl", "mail:d.nl"]


def test_i_and_p_take_a_number_and_say_so_when_they_do_not():
    items = _one_customer()
    status = _status(**{"mail:example.nl": "new", "site:example.nl": "new"})
    rows = migrate.customers(items, status)
    said = []
    assert [row.name for row in migrate.picked(rows, "i 1", said.append).look] == ["example.nl"]
    assert [row.name for row in migrate.picked(rows, "p1", said.append).passwords] == ["example.nl"]
    assert said == []
    assert migrate.picked(rows, "i", said.append).look == []
    assert '"i 1"' in said[-1]
    assert migrate.picked(rows, "7", said.append).move == []
    assert "7 is not on the list" in said[-1]
    assert migrate.picked(rows, "banana", said.append).move == []
    assert "I don't understand: banana" in said[-1]


# ---- the password of an account that already exists -------------------------

import crypt as _crypt

_REAL = "{SHA256-CRYPT}" + _crypt.crypt("HetEchteW8woord", _crypt.mksalt(_crypt.METHOD_SHA256))


def test_the_password_is_checked_against_what_this_server_already_has():
    """The hash carries its own salt, so the right password hashes to the same thing and a wrong one does not."""
    assert migrate.password_fits("HetEchteW8woord", _REAL)
    assert not migrate.password_fits("ietsAnders", _REAL)
    assert not migrate.password_fits("HetEchteW8woord", None)
    assert not migrate.password_fits("HetEchteW8woord", "{CRAM-MD5}nonsense")


def test_without_the_crypt_module_doveadm_does_the_checking(monkeypatch):
    """Python 3.13 took crypt out, and an old server may be newer than this tool."""
    monkeypatch.setattr(migrate, "crypt", None)
    seen = {}

    def fake_run(*words, **kwargs):
        seen["words"], seen["stdin"] = words, kwargs.get("stdin")
        return ""
    assert migrate.password_fits("HetEchteW8woord", _REAL, run=fake_run)
    assert seen["words"] == ("doveadm", "pw", "-t", _REAL)
    assert seen["stdin"] == "HetEchteW8woord\n"  # on standard input, never in the words of a command

    def fake_fail(*words, **kwargs):
        raise migrate.CommandFailed("password mismatch")
    assert not migrate.password_fits("ietsAnders", _REAL, run=fake_fail)


def test_a_wrong_password_changes_nothing_and_asks_again():
    connection, said, asked = Connection(), [], ["ietsAnders", "HetEchteW8woord"]
    got = migrate.give_password(connection, "a0@a.nl", _REAL, said.append, lambda prompt: asked.pop(0))
    assert got is True and not asked
    assert "not the password this server has for a0@a.nl" in " ".join(said)
    assert [r["request"] for r in connection.requests] == ["password"]  # only after it fitted
    assert connection.requests[0]["password"] == "HetEchteW8woord"


def test_enter_leaves_an_account_alone():
    connection, said = Connection(), []
    assert migrate.give_password(connection, "a0@a.nl", _REAL, said.append, lambda prompt: "") is False
    assert connection.requests == []


def test_the_customers_accounts_are_asked_for_one_by_one(monkeypatch):
    row = migrate.customers(_one_customer(), _status(**{"mail:example.nl": "new", "site:example.nl": "new"}))[0]
    connection, said = Connection(), []
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: _REAL)
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "HetEchteW8woord")
    done = migrate.set_passwords(connection, row, said.append, database="mailserver",
                                 old_server="old.nl", new_server="new.nl")
    assert done == ["a0@example.nl", "a1@example.nl"]
    text = " ".join(said)
    assert "no new password is made up" in text and "HetEchteW8woord" not in text
    assert "imapsync \\" in text and "THE-PASSWORD-OF-THIS-ACCOUNT" in text


def test_an_account_this_server_has_no_password_for_is_said_and_skipped(monkeypatch):
    row = migrate.customers([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))[0]
    connection, said = Connection(), []
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: None)
    assert migrate.set_passwords(connection, row, said.append, database="mailserver") == []
    assert "no password stored" in " ".join(said) and connection.requests == []


def test_the_password_never_reaches_a_command_a_note_or_the_report(tmp_path, monkeypatch):
    row = migrate.customers([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))[0]
    connection, state = Connection(), _notes(tmp_path)
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: _REAL)
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "HetEchteW8woord")
    done = migrate.set_passwords(connection, row, lambda text: None, database="mailserver")
    state.save()
    assert done and "HetEchteW8woord" not in open(state.path).read()
    result = migrate.Report()
    result.moved = [(_mail("a.nl"), {})]
    text = migrate.report(result, passwords=done)
    assert "Accounts that now open with the same password on both servers:" in text
    assert "a0@a.nl" in text and "HetEchteW8woord" not in text
    # the new server's half pipes it into mailctl's standard input, never into an argument
    from migrate import migratectl
    ran = []
    monkeypatch.setattr(migratectl, "_run",
                        lambda command, what, stdin=None, **more: ran.append((command, stdin)) or "")
    assert migratectl.password({"address": "a0@a.nl", "password": "HetEchteW8woord"})["ok"] is True
    assert ran == [(["mailctl", "address", "password", "a0@a.nl", "--password-stdin"],
                    "HetEchteW8woord\n")]
    assert migratectl.password({"address": "not an address", "password": "x"})["ok"] is False
    assert migratectl.password({"address": "a0@a.nl", "password": ""})["ok"] is False


# ---- what it costs to draw the screen ---------------------------------------

def test_starting_up_asks_the_new_server_about_no_cron_job(monkeypatch):
    items = _one_customer()
    monkeypatch.setattr(migrate, "_look_around",
                        lambda args, notes: (None, "mailserver", [], {}, items))
    connection = Connection()
    looked = migrate._inspect(migrate._arguments([]), connection, [], [""], {"server": "web03"})
    asked = [request for request in connection.requests if request["request"] == "status"]
    assert len(asked) == 1
    assert sorted(item["kind"] for item in asked[0]["items"]) == ["mail_domain", "site"]
    rows = migrate.customers(looked[4], looked[5])
    # the job nobody asked about can still be moved: its own step comes after the site's
    assert [item["kind"] for item in rows[0].to_move()] == ["mail_domain", "site", "cron"]


def test_looking_inside_asks_about_that_customer_only_and_only_once(capsys):
    items = _one_customer() + [_mail("other.nl"), _cron("other.nl")]
    status = _status(**{"mail:example.nl": "new", "site:example.nl": "new", "mail:other.nl": "new"})
    connection, done = Connection(), set()
    row = migrate.customers(items, status, None)[0]
    migrate._look_inside(connection, row, status, None, {"server": "web03"}, done)
    asked = [request for request in connection.requests if request["request"] == "status"]
    assert len(asked) == 1
    assert [item["id"] for item in asked[0]["items"]] == ["cron:example.nl"]
    migrate._look_inside(connection, row, status, None, {"server": "web03"}, done)
    assert len([r for r in connection.requests if r["request"] == "status"]) == 1
    text = capsys.readouterr().out
    assert "Asking web03 about example.nl ... answered" in text
    assert text.count("example.nl\n\n  Mail") == 2  # printed both times


# ---- the prompt and the live screen -----------------------------------------

def _wired(monkeypatch, items, status, typed, tmp_path):
    monkeypatch.setattr(migrate, "_look_around",
                        lambda args, notes: (None, "mailserver", [], {}, items))
    monkeypatch.setattr(migrate, "_typed_line", lambda prompt: typed.pop(0) if typed else None)
    monkeypatch.setattr(migrate, "_typed", lambda prompt: "yes")
    return Connection(status), migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)


def test_a_at_the_prompt_walks_every_customer(monkeypatch, capsys, tmp_path):
    items = [_mail("a.nl"), _mail("b.nl"), _mail("c.nl")]
    status = _status(**{"mail:a.nl": "new", "mail:b.nl": "new", "mail:c.nl": "new"})
    connection, state = _wired(monkeypatch, items, status, ["a", "q"], tmp_path)
    result = migrate._live(migrate._arguments([]), connection, state, [])
    assert [item["id"] for item, _ in result.moved] == ["mail:a.nl", "mail:b.nl", "mail:c.nl"]
    text = capsys.readouterr().out
    assert "Moving 1 of 3: Mail a.nl" in text and "Moving 3 of 3: Mail c.nl" in text
    assert "Go ahead?" not in text  # it asks nothing of its own: one keystroke, one plain line each
    assert text.count("Old server") == 2  # the screen it started with, and the one after the move


def test_move_all_from_the_command_line_does_the_same(monkeypatch, capsys, tmp_path):
    items = [_mail("a.nl"), _mail("b.nl")]
    status = _status(**{"mail:a.nl": "new", "mail:b.nl": "new"})
    connection, state = _wired(monkeypatch, items, status, [], tmp_path)
    result = migrate._work(migrate._arguments(["move", "all"]), connection, state, [])
    assert [item["id"] for item, _ in result.moved] == ["mail:a.nl", "mail:b.nl"]


def test_the_prompt_is_one_sentence(monkeypatch, capsys, tmp_path):
    items = [_mail("a.nl")]
    connection, state = _wired(monkeypatch, items, _status(**{"mail:a.nl": "new"}), ["q"], tmp_path)
    prompts = []
    monkeypatch.setattr(migrate, "_typed_line", lambda prompt: prompts.append(prompt) or "q")
    migrate._live(migrate._arguments([]), connection, state, [])
    assert " ".join(prompts[0].split()) == (
        "Type a customer's number to open it, a to do the next step of every customer, "
        "t for what is still to do, s to move the spam learning, q to leave:")
    assert all(len(line) <= 80 for line in prompts[0].splitlines())


def test_p_at_the_prompt_sets_the_passwords_and_the_report_says_them(monkeypatch, capsys, tmp_path):
    items = [_mail("a.nl", accounts=2)]
    connection, state = _wired(monkeypatch, items, _status(**{"mail:a.nl": "new"}), ["p 1", "1 all", "q"],
                               tmp_path)
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: _REAL)
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "HetEchteW8woord")
    migrate._live(migrate._arguments([]), connection, state, [])
    text = capsys.readouterr().out
    assert [r["request"] for r in connection.requests].count("password") == 2
    assert "no new password is made up" in text
    assert "Accounts that now open with the same password on both servers:" in text  # in the report too
    assert "HetEchteW8woord" not in text  # the customer's own password is never printed back
    # The imapsync lines need both servers' names, which this fixture has not got; they are covered by
    # test_the_customers_accounts_are_asked_for_one_by_one.




def test_the_stored_hash_is_really_read_from_this_servers_database():
    """Every other test fakes this away, which is how a missing helper got as far as the old server."""
    seen = {}

    def fake_run(*words, **kwargs):
        seen["words"], seen["stdin"] = words, kwargs.get("stdin")
        return "{SHA256-CRYPT}$5$zout$hasj\n"
    assert migrate._stored_hash("o'brien@a.nl", "mailserver", run=fake_run) == "{SHA256-CRYPT}$5$zout$hasj"
    assert seen["words"] == ("mysql", "--batch", "--skip-column-names", "mailserver")
    assert seen["stdin"] == "SELECT password FROM virtual_users WHERE email='o\\'brien@a.nl';\n"
    assert migrate._stored_hash("a@a.nl", "mailserver", run=lambda *w, **k: "  \n") is None


def test_giving_a_password_works_with_nothing_faked_but_the_two_servers(monkeypatch, capsys):
    """The whole of option 5 for one account, with only the database and the new server stood in for."""
    import crypt as c
    real = "{SHA512-CRYPT}" + c.crypt("W8woord!", c.mksalt(c.METHOD_SHA512))
    row = migrate.customers([_mail("a.nl")], _status(**{"mail:a.nl": "exists"}))[0]
    connection = Connection()
    monkeypatch.setattr(migrate, "run_command", lambda *w, **k: real)
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "W8woord!")
    done = migrate.set_passwords(connection, row, database="mailserver",
                                 old_server="old.nl", new_server="new.nl")
    assert done == ["a0@a.nl"]
    assert connection.requests[-1]["password"] == "W8woord!"


def test_the_dns_move_asks_first_and_says_what_it_does():
    """The one thing this tool does that the whole internet sees, so it is never a keystroke on its own."""
    items = _one_customer()
    row = migrate.customers(items, _status(**{"mail:example.nl": "new", "site:example.nl": "exists"}))[0]
    connection, said = Connection(), []
    done = migrate._point_dns(connection, row, ask=lambda question: "yes", say=said.append)
    assert done == ["example.nl"]
    assert connection.requests[-1] == {"request": "point_dns", "domain": "example.nl"}
    text = " ".join(" ".join(said).split())  # it is wrapped to the width
    assert "Visitors go to the new server" in text and "domainctl dns credentials" in text


class Refusing(Connection):
    """A new server that set the site up but couldn't change the records."""

    def ask(self, request):
        Connection.ask(self, request)
        return {"ok": False, "error": "example.nl still doesn't point at this server, so its DNS was not moved.",
                "text": "! Couldn't change example.nl: example.nl isn't in the TransIP account majorlabel.\n"
                        "✓ example.nl is set up and served from /home/example/public_html."}


def test_a_dns_move_that_did_not_happen_says_so_with_what_the_new_server_said():
    row = migrate.customers(_one_customer(), _status(**{"site:example.nl": "exists"}))[0]
    connection, said = Refusing(), []

    assert migrate._point_dns(connection, row, ask=lambda question: "yes", say=said.append) == []

    text = " ".join(" ".join(said).split())
    assert "The DNS of example.nl was not moved" in text
    assert "isn't in the TransIP account majorlabel" in text  # the reason, from the new server itself
    assert "now points at the new server" not in text


def test_saying_no_to_the_dns_move_sends_nothing():
    row = migrate.customers(_one_customer(), _status(**{"site:example.nl": "exists"}))[0]
    connection, said = Connection(), []
    assert migrate._point_dns(connection, row, ask=lambda question: "no", say=said.append) == []
    assert connection.requests == []
    assert "left alone" in " ".join(said)


def test_a_site_this_tool_does_not_move_says_so_before_its_dns_is_touched():
    """A customer's names are asked about one at a time, and a site that is not WordPress is one of them: its
    DNS moving is the one way this tool could take a site that is live off the internet."""
    items = [_mail("example.nl"), _site("example.nl"), _plain("old.example.nl")]
    row = migrate.customers(items, _status(**{"site:example.nl": "exists"}))[0]
    connection, said = Connection(), []

    assert migrate._point_dns(connection, row, ask=lambda question: "no", say=said.append) == []

    text = " ".join(" ".join(said).split())
    assert "old.example.nl is not on the new server" in text
    assert "hasn't got the site" in text
    # The site that is moved is asked about in the same words as ever, with no warning of its own.
    assert "example.nl is not on the new server" not in text.replace("old.example.nl is not on the new "
                                                                     "server", "")


def test_a_customer_with_no_website_has_no_dns_to_move():
    row = migrate.customers([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))[0]
    connection, said = Connection(), []
    assert migrate._point_dns(connection, row, ask=lambda question: "yes", say=said.append) == []
    assert connection.requests == [] and "no website here" in " ".join(said)


# ---- Stopping this server from keeping a moved domain's mail ----------------

NEW_ADDRESS = "187.126.5.158"
OLD_ADDRESS = "178.21.114.195"


class Postfix(object):
    """The commands this server's Postfix answers, and what its DNS says about the mail of example.nl."""

    def __init__(self, mail_at=NEW_ADDRESS, maps="", answers=None, dig=True, host=True):
        self.calls, self.maps, self.mail_at = [], maps, mail_at
        self.dig, self.host = dig, host
        self.answers = answers or {}

    def __call__(self, *args, **named):
        self.calls.append(args)
        # Where mail.example.nl points is left to dns_says: the name servers give no address for it here.
        if args[0] == "dig":
            if not self.dig:
                raise migrate.CommandFailed("dig isn't installed", missing=True)
            return {"NS": "ns0.example.net.\n", "MX": "10 mail.example.nl.\n"}.get(args[4], "")
        if args[0] == "host":
            if not self.host:
                raise migrate.CommandFailed("host isn't installed", missing=True)
            return {"NS": "example.nl name server ns0.example.net.\n",
                    "MX": "example.nl mail is handled by 10 mail.example.nl.\n"}.get(args[4], "")
        if args[:3] == ("postconf", "-h", "transport_maps"):
            return self.maps + "\n"
        if args[:2] == ("postmap", "-q"):
            return self.answers.get(args[3], "")
        if args[0] == "postconf" and args[1] == "-e":
            self.maps = args[2].split("=", 1)[1].strip()
            return ""
        return ""

    def ran(self, program):
        return [call for call in self.calls if call[0] == program]


@pytest.fixture
def dns_says(monkeypatch):
    """What mail.example.nl resolves to here, which is how the tool tells where the mail goes now."""
    def point_at(address):
        monkeypatch.setattr(migrate, "_addresses_of", lambda name, look=None: {address})
    return point_at


def _done_customer():
    """A customer whose mail is all on the new server already."""
    return migrate.customers([_mail("example.nl", accounts=2)],
                             {"mail:example.nl": _there("example.nl", accounts=2)})[0]


def test_the_menu_offers_to_stop_keeping_the_mail_here():
    keys = dict((key, (what, note)) for key, what, note in migrate.customer_menu(_done_customer()))
    assert keys["7"] == ("stop keeping this domain's mail here", "")
    half = migrate.customers(_one_customer(), _status())[0]
    assert dict((key, note) for key, _, note in migrate.customer_menu(half))["7"] == "move the mail first"
    assert migrate.customer_action(_done_customer(), "7")[1] == "handover"


def test_handing_a_domain_over_writes_the_transport_and_reloads_postfix(tmp_path):
    """One line, read before any map Postfix already reads, and Postfix told to read it again."""
    run = Postfix(maps="mysql:/etc/postfix/mysql-transport.cf")
    path = str(tmp_path / "migrate-transport")
    maps = migrate.hand_mail_over("example.nl", "new.example.nl", run=run, path=path)
    assert maps == "hash:%s mysql:/etc/postfix/mysql-transport.cf" % path
    text = open(path).read()
    assert "example.nl\tsmtp:[new.example.nl]" in text and "postmap %s" % migrate.TRANSPORT_FILE in text
    assert run.ran("postmap")[0] == ("postmap", path)
    assert run.ran("postfix") == [("postfix", "reload")]


def test_handing_the_same_domain_over_twice_leaves_one_line(tmp_path):
    run = Postfix()
    path = str(tmp_path / "migrate-transport")
    migrate.hand_mail_over("example.nl", "first.example.nl", run=run, path=path)
    migrate.hand_mail_over("example.nl", "new.example.nl", run=run, path=path)
    lines = [line for line in open(path).read().splitlines() if line and not line.startswith("#")]
    assert lines == ["example.nl\tsmtp:[new.example.nl]"]


def test_a_domain_whose_mail_still_comes_here_is_not_handed_over(tmp_path, dns_says, monkeypatch):
    """The customer is still reading it here, so sending it on would hide every message from them."""
    dns_says(OLD_ADDRESS)
    monkeypatch.setattr(migrate, "TRANSPORT_FILE", str(tmp_path / "migrate-transport"))
    run, said, asked = Postfix(), [], []
    done = migrate._hand_over_domains(Connection(), ["example.nl"], {"server": "new.nl",
                                                                     "addresses": [NEW_ADDRESS]},
                                      ask=lambda question: asked.append(question) or "yes",
                                      say=said.append, run=run)
    assert done == [] and asked == []
    assert "still delivered to this server" in " ".join(" ".join(said).split())
    assert run.ran("postfix") == [] and not os.path.exists(str(tmp_path / "migrate-transport"))


def test_the_offer_after_a_mail_move_asks_once_the_dns_is_moved(tmp_path, dns_says, monkeypatch):
    dns_says(NEW_ADDRESS)
    path = str(tmp_path / "migrate-transport")
    monkeypatch.setattr(migrate, "TRANSPORT_FILE", path)
    run, said, asked = Postfix(), [], []
    done = migrate._offer_handover(Connection(), ["example.nl"], {"server": "new.nl",
                                                                  "addresses": [NEW_ADDRESS]},
                                   ask=lambda question: asked.append(question) or "yes",
                                   say=said.append, run=run)
    assert done == ["example.nl"] and len(asked) == 1
    assert "example.nl\tsmtp:[new.nl]" in open(path).read()


def test_the_offer_after_a_mail_move_only_names_a_domain_that_has_to_wait(dns_says):
    """Nothing to decide yet: the question would have one safe answer, so it isn't asked."""
    dns_says(OLD_ADDRESS)
    said, asked = [], []
    done = migrate._offer_handover(Connection(), ["example.nl"], {"server": "new.nl",
                                                                  "addresses": [NEW_ADDRESS]},
                                   ask=lambda question: asked.append(question) or "yes",
                                   say=said.append, run=Postfix())
    assert done == [] and asked == []
    text = " ".join(" ".join(said).split())
    assert "lands in the old mailbox here" in text and "once its DNS has been moved" in text


def test_the_offer_after_a_mail_move_asks_nothing_it_cannot_look_up(dns_says):
    """Without dig and without host there is no way to see where the mail goes, and a question nobody can
    answer from the screen is how mail ends up in a mailbox nobody reads."""
    dns_says(NEW_ADDRESS)
    said, asked = [], []
    run = Postfix(dig=False, host=False)
    done = migrate._offer_handover(Connection(), ["example.nl"], {"server": "new.nl",
                                                                  "addresses": [NEW_ADDRESS]},
                                   ask=lambda question: asked.append(question) or "yes",
                                   say=said.append, run=run)
    assert done == [] and asked == []
    assert "can't look up the MX" in " ".join(" ".join(said).split())


def test_a_customer_whose_mail_is_only_half_there_is_not_handed_over():
    row = migrate.customers(_one_customer(), _status())[0]
    said = []
    assert migrate._hand_over(Connection(), row, {"server": "new.nl"}, say=said.append, run=Postfix()) == []
    assert "isn't all on the new server yet" in " ".join(" ".join(said).split())


def test_mail_the_world_sends_on_while_this_server_still_keeps_it_is_flagged_when_a_customer_is_opened(
        dns_says, monkeypatch):
    """The quietest way to lose mail in a move: the MX is moved, so the world delivers to the new server, but
    Postfix here still has the domain in its own tables, so anything sent from a mailbox here stays behind."""
    dns_says(NEW_ADDRESS)
    run = Postfix()
    row = _done_customer()

    dns = migrate.dns_now(row, [NEW_ADDRESS], ours={OLD_ADDRESS}, run=run)
    kept = migrate.kept_here(dns, run=run)

    assert dns["mail_there"] == ["example.nl"] and kept == ["example.nl"]
    screen = migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl", dns=dns, kept=kept)
    text = " ".join(" ".join(screen.split()).split())
    assert "The world sends the mail of example.nl to the new server now" in text
    assert "Number 7 stops that." in text


def test_a_domain_that_is_already_handed_over_is_not_flagged(dns_says):
    dns_says(NEW_ADDRESS)
    ours = "hash:%s" % migrate.TRANSPORT_FILE
    run = Postfix(maps=ours, answers={ours: "smtp:[web03.majorlabel.nl]"})

    dns = migrate.dns_now(_done_customer(), [NEW_ADDRESS], ours={OLD_ADDRESS}, run=run)

    assert dns["mail_there"] == ["example.nl"] and migrate.kept_here(dns, run=run) == []


def test_a_domain_the_world_still_sends_here_is_not_flagged_either(dns_says):
    """Until its DNS is moved, this server is where that mail belongs."""
    dns_says(OLD_ADDRESS)
    run = Postfix()

    dns = migrate.dns_now(_done_customer(), [NEW_ADDRESS], ours={OLD_ADDRESS}, run=run)

    assert dns["mail_there"] == [] and migrate.kept_here(dns, run=run) == []


def test_a_postfix_that_cannot_be_asked_claims_nothing():
    def no_postfix(*args, **named):
        raise migrate.CommandFailed("postconf isn't installed", missing=True)

    assert migrate.handed_over("example.nl", run=no_postfix) is None
    assert migrate.kept_here({"mail_there": ["example.nl"]}, run=no_postfix) == []


def test_a_map_that_answers_before_ours_is_said_out_loud(tmp_path, dns_says, monkeypatch):
    """Postfix goes by the first map that answers, so a transport already there wins and this says so."""
    dns_says(NEW_ADDRESS)
    path = str(tmp_path / "migrate-transport")
    monkeypatch.setattr(migrate, "TRANSPORT_FILE", path)
    run = Postfix(maps="hash:/etc/postfix/transport",
                  answers={"hash:/etc/postfix/transport": "smtp:[somewhere.else]"})
    said = []
    done = migrate._hand_over_domains(Connection(), ["example.nl"], {"server": "new.nl",
                                                                     "addresses": [NEW_ADDRESS]},
                                      ask=lambda question: "yes", say=said.append, run=run)
    text = " ".join(" ".join(said).split())
    assert done == [] and "somewhere.else" in text and "not to the new server" in text


def test_without_dig_it_asks_but_says_it_could_not_look(tmp_path, dns_says, monkeypatch):
    dns_says(NEW_ADDRESS)
    monkeypatch.setattr(migrate, "TRANSPORT_FILE", str(tmp_path / "migrate-transport"))
    run = Postfix(dig=False)
    said = []
    done = migrate._hand_over_domains(Connection(), ["example.nl"], {"server": "new.nl",
                                                                     "addresses": [NEW_ADDRESS]},
                                      ask=lambda question: "yes", say=said.append, run=run)
    assert done == ["example.nl"]  # host answered instead
    assert run.ran("host")


def test_the_mail_hosts_of_both_dig_and_host_are_read():
    assert migrate._mx_hosts("10 mail.example.nl.\n20 mail2.example.nl.\n") == ["mail.example.nl",
                                                                                "mail2.example.nl"]
    assert migrate._mx_hosts("example.nl mail is handled by 10 mail.example.nl.") == ["mail.example.nl"]
    assert migrate._mx_hosts("") == [] and migrate._mx_hosts("not found: 3(NXDOMAIN)") == []


def test_where_the_mail_goes_is_asked_of_the_domains_own_name_servers(monkeypatch):
    """Not of this server's resolver, which may keep the old answer or the whole zone itself."""
    monkeypatch.setattr(migrate, "_addresses_of", lambda name, look=None: {"10.0.0.1"})  # what this server thinks
    asked = []

    def run(*args, **named):
        asked.append(args)
        kind, name, server = args[4], args[5], args[6]
        return {("NS", "@1.1.1.1"): "ns0.transip.net.\n",
                ("MX", "@ns0.transip.net"): "10 mail.example.nl.\n",
                ("A", "@ns0.transip.net"): NEW_ADDRESS + "\n"}.get((kind, server), "")

    assert migrate.mail_answered_there("example.nl", [NEW_ADDRESS], run=run) is True
    assert ("dig", "+short", "+time=5", "+tries=1", "MX", "example.nl", "@ns0.transip.net") in asked


class Repairs(object):
    """The new server, running a repair and then saying how the domain stands."""

    def __init__(self, answer):
        self.requests, self.answer = [], answer

    def ask(self, request):
        self.requests.append(request)
        return self.answer


def test_eight_runs_mailctl_repair_there_and_says_the_domain_is_set_up():
    """The repair's own account of what it found and put right runs to fifty lines and says nothing more."""
    row = _done_customer()
    assert migrate.customer_action(row, "8")[1] == "repair"
    assert dict((key, note) for key, _, note in migrate.customer_menu(row))["8"] == "mailctl repair --yes"
    server, said = Repairs({"ok": True, "right": True, "text": "✓ example.nl  everything is set up.\n"}), []

    assert migrate._repair(server, row, say=said.append) == ["example.nl"]

    assert server.requests == [{"request": "repair", "domain": "example.nl"}]
    assert "The mail of example.nl is set up on the new server" in " ".join(said)
    assert not any("Still not right" in line for line in said)


def test_what_is_still_not_right_after_a_repair_is_what_the_new_server_checked_afterwards():
    row, said = _done_customer(), []
    server = Repairs({"ok": True, "right": False,
                      "text": "example.nl\n  ✗ Certificate  mail.example.nl isn't in the certificate.\n"})

    assert migrate._repair(server, row, say=said.append) == ["example.nl"]

    text = " ".join(" ".join(said).split())
    assert "Still not right: Certificate." in text
    assert "did what it could" in text


def test_a_new_server_that_cannot_say_how_a_repaired_domain_stands_says_that():
    row, said = _done_customer(), []
    server = Repairs({"ok": True, "right": None, "text": ""})

    assert migrate._repair(server, row, say=said.append) == ["example.nl"]

    text = " ".join(" ".join(said).split())
    assert "couldn't say how the domain stands now" in text and "mailctl doctor example.nl" in text


def test_every_line_of_the_menu_fits_and_lines_up():
    """A line longer than the screen is cut, and loses its indent and the gap before its note."""
    for row in (_done_customer(), migrate.customers(_one_customer(), _status())[0]):
        menu = [line for line in migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl")
                .splitlines() if re.match(r"  [0-9bcq]  ", line)]
        assert len(menu) == len(migrate.customer_menu(row))
        assert all(len(line) <= 79 for line in menu)


# ---- Where the names of a customer point now --------------------------------

class Dns(object):
    """What the name servers of example.nl answer here, asked with dig."""

    def __init__(self, site=OLD_ADDRESS, mail=OLD_ADDRESS, dig=True):
        self.site, self.mail, self.dig, self.asked = site, mail, dig, []

    def __call__(self, *args, **named):
        self.asked.append(args)
        if args[0] != "dig" or not self.dig:
            raise migrate.CommandFailed("%s isn't installed" % args[0], missing=True)
        kind, name = args[4], args[5]
        if kind == "NS":
            return "ns0.example.net.\n"
        if kind == "MX":
            return "10 mail.example.nl.\n" if self.mail else ""
        if kind == "A":
            return "%s\n" % ((self.mail if name == "mail.example.nl" else self.site) or "")
        return ""  # nothing anywhere has an IPv6 address


@pytest.fixture(autouse=True)
def nothing_remembered_about_the_dns():
    """Where a name points is remembered for a couple of minutes, and not from one test to the next."""
    migrate._DNS_SEEN.clear()
    yield
    migrate._DNS_SEEN.clear()


def _mail_and_site():
    return migrate.customers([_mail("example.nl"), _site("example.nl")], _status())[0]


def _points(row, run):
    return migrate.dns_now(row, [NEW_ADDRESS], ours=[OLD_ADDRESS], run=run)


def test_the_screen_says_which_of_the_names_the_world_is_sent_here_for():
    """Every part can be on the new server while the whole world still comes here: that is the DNS, and the
    table says where it stands like it says it of the rest."""
    row = _mail_and_site()
    found = _points(row, Dns())
    assert migrate._dns_cells(found) == ["DNS", "website, mail", "-", "to do"]
    text = migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl", dns=found)
    assert _cells(text, "DNS") == ["DNS", "website, mail", "-", "to do"]
    assert all(len(line) <= 79 for line in text.splitlines())


def test_the_dns_row_says_done_once_both_names_answer_at_the_new_server():
    found = _points(_mail_and_site(), Dns(site=NEW_ADDRESS, mail=NEW_ADDRESS))
    assert migrate._dns_cells(found) == ["DNS", "-", "website, mail", "done"]
    assert migrate._dns_notes(found) == []


def test_a_customer_whose_website_moved_but_whose_mail_did_not_is_half_there():
    found = _points(_mail_and_site(), Dns(site=NEW_ADDRESS))
    assert migrate._dns_cells(found) == ["DNS", "mail", "website", "part"]


def test_a_name_that_points_at_neither_server_is_said_under_the_table():
    row = migrate.customers([_site("example.nl")], _status())[0]
    found = _points(row, Dns(site="203.0.113.9"))
    assert migrate._dns_cells(found) == ["DNS", "-", "-", "to do"]
    assert migrate._dns_notes(found) == ["The website of example.nl is served from 203.0.113.9, which is "
                                         "neither this server nor the new one."]


def test_a_server_that_cannot_look_a_name_up_says_so_instead_of_guessing():
    found = _points(_mail_and_site(), Dns(dig=False))
    assert migrate._dns_cells(found) == ["DNS", "-", "-", "can't tell"]
    assert len(migrate._dns_notes(found)) == 2
    assert all("can't be looked up from this server" in note for note in migrate._dns_notes(found))


def test_a_customer_with_no_website_and_no_mail_has_a_dash_in_the_dns_row():
    row = migrate.customers([_cron("example.nl")], _status())[0]
    run = Dns()
    assert migrate._dns_cells(_points(row, run)) == ["DNS", "-", "-", "-"]
    assert [args for args in run.asked if args[4] in ("A", "MX")] == []


def test_where_the_names_point_is_looked_up_once_and_asked_again_when_it_changes(monkeypatch):
    """The screen is drawn again after every number that is typed; a handful of lookups each time crawls."""
    monkeypatch.setattr(migrate, "_our_addresses", lambda: {OLD_ADDRESS})
    row, run, hello = _mail_and_site(), Dns(), {"addresses": [NEW_ADDRESS]}
    first = migrate.dns_seen(row, hello, run=run, now=1000)
    asked = len(run.asked)
    assert migrate.dns_seen(row, hello, run=run, now=1060) is first and len(run.asked) == asked
    migrate.forget_dns(row.name)
    assert migrate.dns_seen(row, hello, run=run, now=1060) is not first and len(run.asked) > asked
    asked = len(run.asked)
    migrate.dns_seen(row, hello, run=run, now=1060 + migrate.DNS_SEEN_FOR + 1)
    assert len(run.asked) > asked  # an answer that old is asked again by itself


def test_the_mail_of_a_domain_is_said_to_be_here_as_well_as_there():
    run = Dns()
    assert migrate.mail_points_at("example.nl", [NEW_ADDRESS], [OLD_ADDRESS], run=run) == \
        ("here", "mail.example.nl")
    assert migrate.mail_answered_there("example.nl", [NEW_ADDRESS], run=run) is False
    run = Dns(mail=NEW_ADDRESS)
    assert migrate.mail_points_at("example.nl", [NEW_ADDRESS], [OLD_ADDRESS], run=run) == \
        ("there", "mail.example.nl")
    assert migrate.mail_answered_there("example.nl", [NEW_ADDRESS], run=run) is True


def test_a_long_note_in_the_menu_keeps_its_line_indented_and_its_note_in_the_column(monkeypatch):
    """A line that runs past the edge of the screen is cut, and loses the indent and the gap inside it: the
    note ends up against the words it belongs beside, and the number out in the margin."""
    menu = [("1", "move the mail", "adds only what is missing"),
            ("2", "put the mail right on the new server", "mailctl repair, yes to every step"),
            ("b", "back to the list", "")]
    monkeypatch.setattr(migrate, "customer_menu", lambda row: menu)
    lines = [line for line in migrate.customer_screen(_done_customer(), _status(),
                                                      new_server="web03.majorlabel.nl").splitlines()
             if re.match(r"  [0-9bcq]  ", line)]
    assert len(lines) == len(menu)
    assert all(len(line) <= 79 for line in lines)
    columns = dict((key, line.index(note)) for line, (key, _, note) in zip(lines, menu) if note)
    assert columns["2"] == columns["1"]  # the long note stands in the same column as the short one


def test_a_line_that_is_too_long_is_cut_but_keeps_the_indent_in_front_of_it():
    assert migrate._fit_line("  " + "a" * 100).startswith("  a")
    assert len(migrate._fit_line("  " + "a" * 100)) == 79


# ---- Copying the messages of a customer's mailboxes over --------------------

def _sizes_answer(held, addresses):
    """What the new server answers a mail_sizes request: every address it was asked about, with what its
    mailbox there holds, or that it has no mailbox for it."""
    return {"ok": True, "mailboxes": dict(
        (address, {"here": True, "bytes": sum(held[address]), "messages": len(held[address])}
         if address in held else {"here": False}) for address in addresses)}


class Copies(object):
    """The new server, answering about the mail it is copying and the mail it already holds."""

    def __init__(self, status=None, refuse=False, held=None, refuse_short=False, refuse_passwords=False):
        self.requests, self.status, self.refuse = [], dict(status or {}), refuse
        self.held = dict(held or {})
        # The new server's own rule: a password of fewer than eight characters is refused.
        self.refuse_short, self.refuse_passwords = refuse_short, refuse_passwords

    def ask(self, request, timeout=None):
        self.requests.append(dict(request))
        kind = request["request"]
        if kind == "password" and (self.refuse_passwords or
                                   (self.refuse_short and len(request["password"]) < 8)):
            return {"ok": False, "error": "✗ The password needs at least 8 characters."}
        if kind == "mail_sizes":
            return _sizes_answer(self.held, request["addresses"])
        if kind == "copy_mail_status":
            return dict({"ok": True, "started": False, "running": False},
                        **self.status.get(request["address"], {}))
        if kind == "copy_mail":
            if self.refuse:
                return {"ok": False, "error": "no mailbox here"}
            self.status[request["address"]] = {"started": True, "running": True, "copied": 0}
        return {"ok": True}

    def asked_for(self, kind):
        return [request for request in self.requests if request["request"] == kind]


@pytest.fixture
def the_password_is_known(monkeypatch):
    """This server has a password stored for every account, and 'secret' is the one that fits it."""
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: "{SHA512-CRYPT}$6$x$y")
    monkeypatch.setattr(migrate, "password_fits", lambda password, stored, run=None: password == "secret")
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "secret")
    monkeypatch.setattr(migrate, "_our_addresses", lambda: {OLD_ADDRESS})


def test_the_menu_offers_to_copy_the_mail_messages_across():
    keys = dict((key, (what, note)) for key, what, note in migrate.customer_menu(_done_customer()))
    assert keys["9"] == ("copy the mail messages across", "")
    half = migrate.customers(_one_customer(), _status())[0]
    assert dict((key, note) for key, _, note in migrate.customer_menu(half))["9"] == "move the mail first"
    assert migrate.customer_action(_done_customer(), "9")[1] == "copy_mail"


def test_copying_the_mail_asks_for_each_password_and_gives_it_to_both_servers(the_password_is_known):
    """imapsync opens the mailbox on both servers, so the same password has to work on both."""
    server, said, watched = Copies(), [], []
    done = migrate._copy_mail(server, _done_customer(), {"addresses": [NEW_ADDRESS]}, database="mailserver",
                              ask=lambda question: "yes", say=said.append,
                              watch=lambda connection, addresses, **named: watched.append(list(addresses)))
    assert done == ["a0@example.nl", "a1@example.nl"]
    assert [request["address"] for request in server.asked_for("password")] == done
    copies = server.asked_for("copy_mail")
    assert [request["address"] for request in copies] == done
    assert all(request["password"] == "secret" and request["host"] == OLD_ADDRESS for request in copies)
    assert watched == [done]


def test_the_mail_is_not_copied_before_the_accounts_are_on_the_new_server():
    row = migrate.customers(_one_customer(), _status())[0]
    server, said = Copies(), []
    assert migrate._copy_mail(server, row, say=said.append) == []
    assert "Move the mail first" in " ".join(" ".join(said).split()) and server.requests == []


def test_a_mailbox_that_is_already_being_copied_is_watched_and_not_started_again(the_password_is_known):
    server = Copies({"a0@example.nl": {"started": True, "running": True, "copied": 12}})
    said, watched = [], []
    done = migrate._copy_mail(server, _done_customer(), database="mailserver",
                              ask=lambda question: "no", say=said.append,
                              watch=lambda connection, addresses, **named: watched.append(list(addresses)))
    assert done == [] and server.asked_for("copy_mail") == []
    assert watched == [["a0@example.nl"]]
    assert "12 messages so far" in " ".join(" ".join(said).split())


def test_a_copy_the_new_server_will_not_start_is_said_and_the_rest_goes_on(the_password_is_known):
    server, said = Copies(refuse=True), []
    done = migrate._copy_mail(server, _done_customer(), database="mailserver", ask=lambda question: "yes",
                              say=said.append, watch=lambda *args, **named: None)
    assert done == [] and "no mailbox here" in " ".join(said)
    assert len(server.asked_for("copy_mail")) == 2  # the second account is still offered


def test_an_account_this_server_has_no_password_for_is_left_alone(monkeypatch):
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: None)
    monkeypatch.setattr(migrate, "_our_addresses", lambda: {OLD_ADDRESS})
    server, said = Copies(), []
    assert migrate._copy_mail(server, _done_customer(), database="mailserver", ask=lambda question: "yes",
                              say=said.append, watch=lambda *args, **named: None) == []
    assert server.asked_for("copy_mail") == []
    assert "nothing to check a password against" in " ".join(" ".join(said).split())


def test_a_mailbox_that_is_not_copied_for_want_of_a_password_says_so(monkeypatch):
    """Enter at the password prompt used to send the screen straight back, which read as if 9 only measured."""
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: "{SHA512-CRYPT}$6$x$y")
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "")
    monkeypatch.setattr(migrate, "_our_addresses", lambda: {OLD_ADDRESS})
    server, said = Copies(), []

    assert migrate._copy_mail(server, _done_customer(), database="mailserver", ask=lambda question: "yes",
                              say=said.append, watch=lambda *args, **named: None) == []

    assert server.asked_for("copy_mail") == []
    assert "No password, so the mail of a0@example.nl was not copied." in " ".join(" ".join(said).split())


def test_how_far_a_copy_is_said_in_a_few_words():
    assert migrate._copy_words({"started": False}) == "not copied here yet"
    assert migrate._copy_words({"started": True, "running": True, "copied": 1}) == "1 message so far"
    assert migrate._copy_words({"started": True, "running": False, "copied": 9, "exit": 0}) == \
        "9 messages, no errors"
    assert migrate._copy_words({"started": True, "running": False, "copied": 9, "errors": 2}) == \
        "9 messages, 2 errors"
    assert migrate._copy_words({"started": True, "running": False, "copied": 3, "exit": 1}) == \
        "stopped after 3 messages"


def test_watching_the_copies_says_each_change_and_stops_when_they_are_over():
    answers = {"a0@example.nl": [{"ok": True, "started": True, "running": True, "copied": 5},
                                 {"ok": True, "started": True, "running": True, "copied": 5},
                                 {"ok": True, "started": True, "running": False, "copied": 9, "exit": 0}]}

    class Watched(Copies):
        def ask(self, request, timeout=None):
            self.requests.append(dict(request))
            return answers[request["address"]].pop(0)

    said = []
    server = Watched()
    assert migrate._watch_copies(server, ["a0@example.nl"], say=said.append, wait=lambda seconds: None) == \
        ["a0@example.nl"]
    lines = [line for line in said if line.startswith("  ")]
    assert lines == ["  a0@example.nl: 5 messages so far", "  a0@example.nl: 9 messages, no errors"]


def test_ctrl_c_stops_the_watching_and_leaves_the_copies_running():
    def stop(seconds):
        raise KeyboardInterrupt()

    said = []
    assert migrate._watch_copies(Copies(), ["a0@example.nl"], say=said.append, wait=stop) == []
    assert "keep running on the new server" in " ".join(" ".join(said).split())


def test_the_new_server_is_told_the_address_it_can_reach_this_one_at():
    assert migrate._this_server(look=lambda: {"2a02:2770::21a", OLD_ADDRESS}) == OLD_ADDRESS
    assert migrate._this_server(look=lambda: set()) == migrate.socket.gethostname()


def test_the_screen_says_it_is_looking_the_names_up_only_when_it_really_has_to(monkeypatch):
    """Name servers on the other side of the world take a moment; a screen that stops dead looks hung."""
    row, steps = _mail_and_site(), []
    monkeypatch.setattr(migrate, "dns_seen", lambda row, hello: {"here": [], "there": [], "away": [],
                                                                 "unknown": []})
    monkeypatch.setattr(migrate, "_step", lambda label, work, summary=None, out=None: steps.append(label)
                        or work())
    migrate._DNS_SEEN[row.name] = (migrate.time.time(), {"here": []})
    migrate._dns_for(row, {})
    assert steps == []  # it was looked up a moment ago, so the screen is drawn straight away
    migrate.forget_dns(row.name)
    migrate._dns_for(row, {})
    assert len(steps) == 1 and "example.nl" in steps[0]


# ---- How much mail the mailboxes hold, here and there -----------------------

def _mailboxes(held, maildir=True, age=0):
    """Mailboxes on this server, as dovecot keeps them: /var/vmail/<domain>/<user>/Maildir/cur. An old server
    that keeps the maildir in the account's own folder instead is the same mailbox, one folder higher up.
    'age' is how many seconds ago the messages landed, for the screens that ask whether mail came in after a
    copy."""
    for address, messages in held.items():
        local_part, _, domain = address.partition("@")
        parts = [migrate.VMAIL, domain, local_part] + (["Maildir"] if maildir else []) + ["cur"]
        folder = os.path.join(*parts)
        os.makedirs(folder)
        for number, size in enumerate(messages):
            path = os.path.join(folder, "%d.host,S=%d" % (number, size))
            with open(path, "wb") as file:
                file.write(b"x" * size)
            if age:
                os.utime(path, (time.time() - age, time.time() - age))


def _without_time(found):
    """What a mailbox holds, without when its newest message landed: that is the clock, not the mailbox."""
    return dict((key, value) for key, value in found.items() if key != "newest")


def _held_by(address):
    return _without_time(migrate.mailbox_size(address))


def _mailbox_names(screen):
    """The first cell of every row of the screen that is one mailbox, in the order they stand in."""
    return [re.split(r"\s{2,}", line.strip())[0] for line in screen.splitlines()
            if line.startswith("  " + migrate._under(""))]


class Mailboxes(object):
    """The new server, saying how much mail its own mailboxes hold."""

    def __init__(self, held=None, refuse=False, answer=None):
        self.held, self.refuse, self.requests = dict(held or {}), refuse, []
        self.answer = answer  # a whole answer, for the things a held-list can't say

    def ask(self, request, timeout=None):
        self.requests.append(dict(request))
        if self.refuse:
            return {"ok": False, "error": "this server couldn't say"}
        return dict(self.answer) if self.answer else _sizes_answer(self.held, request["addresses"])


def test_how_much_a_mailbox_holds_is_its_messages_and_not_dovecots_own_files():
    """imapsync copies the messages; the indexes beside them are the new server's own, and counting them in
    would make a mailbox that is wholly across look a different size from here."""
    _mailboxes({"a0@example.nl": [2000, 300]})
    folder = os.path.join(migrate.VMAIL, "example.nl", "a0", "Maildir")
    os.makedirs(os.path.join(folder, "tmp"))
    with open(os.path.join(folder, "tmp", "half-delivered"), "wb") as file:
        file.write(b"x" * 9999)
    for name in ("dovecot.index.cache", "dovecot-uidlist"):
        with open(os.path.join(folder, name), "wb") as file:
            file.write(b"x" * 50000)

    assert _held_by("a0@example.nl") == {"bytes": 2300, "messages": 2}


def test_a_maildir_in_the_accounts_own_folder_is_found_as_well():
    _mailboxes({"a0@example.nl": [1000]}, maildir=False)
    assert _held_by("a0@example.nl") == {"bytes": 1000, "messages": 1}


def test_an_account_with_no_mailbox_on_this_server_is_not_measured():
    _mailboxes({"a0@example.nl": [1000]})
    assert migrate.mailbox_size("a1@example.nl") is None
    found = migrate.mailboxes_here(["a0@example.nl", "a1@example.nl"])
    assert list(found) == ["a0@example.nl"] and _without_time(found["a0@example.nl"]) == \
        {"bytes": 1000, "messages": 1}


def test_the_messages_row_holds_what_is_here_beside_what_the_new_server_has():
    _mailboxes({"a0@example.nl": [2000000], "a1@example.nl": [500000]})
    server = Mailboxes({"a0@example.nl": [2000000]})
    row = _done_customer()

    screen = migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl",
                                     mail=migrate.mail_sizes(row, server))

    assert _cells(screen, "Messages") == ["Messages", "2.5 MB", "2.0 MB", migrate.PART]
    assert [request["addresses"] for request in server.requests] == [["a0@example.nl", "a1@example.nl"]]


def test_every_mailbox_has_a_row_of_its_own_under_it():
    """Added up says whether anything is left; one row each says which account that is."""
    _mailboxes({"a0@example.nl": [2000000], "a1@example.nl": [500000]})
    row = _done_customer()

    screen = migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl",
                                     mail=migrate.mail_sizes(row, Mailboxes({"a0@example.nl": [2000000]})))

    # The mailbox that is still to copy stands first, and the customer's own domain is left off the name.
    assert _cells(screen, "a1") == [migrate._under("a1"), "500.0 KB", "-", migrate.TO_DO]
    assert _cells(screen, "a0") == [migrate._under("a0"), "2.0 MB", "2.0 MB", migrate.DONE]
    assert _mailbox_names(screen) == [migrate._under("a1"), migrate._under("a0")]


def test_more_mailboxes_than_the_screen_holds_are_one_row_together():
    """The menu under the table has to stay in view, and what is left to copy stands first, so the rows that
    are folded away are mailboxes that are across."""
    held = dict(("a%02d@example.nl" % number, [1000]) for number in range(migrate.MESSAGE_ROWS + 3))
    _mailboxes(held)
    row = migrate.customers([_mail("example.nl", accounts=0)], {"mail:example.nl": _there("example.nl")})[0]
    row.items["mail"][0]["data"]["addresses"] = [{"address": address} for address in sorted(held)]

    screen = migrate.customer_screen(row, _status(), new_server="web03.majorlabel.nl",
                                     mail=migrate.mail_sizes(row, Mailboxes(held)))

    assert len(_mailbox_names(screen)) == migrate.MESSAGE_ROWS + 1
    assert _cells(screen, "and 3 more") == [migrate._under("and 3 more"), "3.0 KB", "3.0 KB", migrate.DONE]


@pytest.mark.parametrize("there, word", [
    ({"a0@example.nl": [2000], "a1@example.nl": [1000]}, migrate.DONE),
    ({"a0@example.nl": [2000]}, migrate.PART),
    ({"a0@example.nl": [1900]}, migrate.PART),  # all but a hair of it: the rest is one message, not a mailbox
    ({}, migrate.TO_DO),
])
def test_the_messages_row_says_where_the_copying_stands(there, word):
    _mailboxes({"a0@example.nl": [2000], "a1@example.nl": [1000]})
    row = _done_customer()
    assert migrate._messages_cells(*migrate.mail_sizes(row, Mailboxes(there)))[3] == word


def test_a_new_server_that_cannot_say_leaves_the_rows_honest():
    _mailboxes({"a0@example.nl": [2000]})
    row = _done_customer()
    found = migrate.mail_sizes(row, Mailboxes(refuse=True))
    assert migrate._messages_cells(*found) == ["Messages", "2.0 KB", migrate.CANT_TELL, migrate.CANT_TELL]
    assert migrate._mailbox_rows(row, *found) == \
        [[migrate._under("a0"), "2.0 KB", migrate.CANT_TELL, migrate.CANT_TELL]]
    assert "couldn't be asked" in migrate._messages_notes(*found)[0]


def test_a_customer_with_no_mailboxes_here_has_nothing_to_say_about_messages():
    row = _done_customer()
    found = migrate.mail_sizes(row, Mailboxes())
    assert migrate._messages_cells(*found) == ["Messages", "-", "-", migrate.NONE]
    assert migrate._mailbox_rows(row, *found) == []
    assert migrate._messages_notes(*found) == []


def test_the_same_thing_in_a_sentence_inside_a_customer():
    _mailboxes({"a0@example.nl": [2000000], "a1@example.nl": [500000]})
    row = _done_customer()

    said = migrate.inside(row, _status(), new_server="web03.majorlabel.nl",
                          mail=migrate.mail_sizes(row, Mailboxes({"a0@example.nl": [2000000]})))

    assert "Messages" in _row(said, "Messages")
    assert "2.5 MB here, 2.0 MB on web03 so far" in " ".join(" ".join(said.split()).split())


def test_the_mailboxes_are_measured_once_and_then_remembered():
    """The screen is drawn again after every number typed, and walking tens of gigabytes each time crawls."""
    row, walked = _done_customer(), []
    measure = lambda addresses: walked.append(list(addresses)) or {}

    for _ in range(3):
        migrate.mail_sizes(row, Mailboxes(), measure=measure)
    assert len(walked) == 1

    migrate.forget_sizes(row.name)
    migrate.mail_sizes(row, Mailboxes(), measure=measure)
    assert len(walked) == 2


def test_the_screen_says_it_is_measuring_only_when_it_really_has_to(monkeypatch):
    row, steps = _done_customer(), []
    monkeypatch.setattr(migrate, "_step", lambda label, work, summary=None, out=None: steps.append(label)
                        or work())
    migrate._mail_for(row, Mailboxes())
    assert len(steps) == 1 and "example.nl" in steps[0]
    migrate._mail_for(row, Mailboxes())
    assert len(steps) == 1  # it was measured a moment ago, so the screen is drawn straight away


def test_copying_a_mailbox_says_what_it_holds_on_both_servers(the_password_is_known):
    """The one thing on that screen that answers "is this one across already", so it is a block of its own
    above the question and not a sentence inside a paragraph of warnings."""
    _mailboxes({"a0@example.nl": [2000000], "a1@example.nl": [500000]})
    server, said = Copies(held={"a0@example.nl": [2000000]}), []

    migrate._copy_mail(server, _done_customer(), {"server": "web03.majorlabel.nl"}, database="mailserver",
                       ask=lambda question: "no", say=said.append, watch=lambda *args, **named: None)

    lines = [line for block in said for line in block.splitlines()]
    assert "a0@example.nl" in lines and "a1@example.nl" in lines
    assert "  here   2.0 MB in 1 message" in lines and "  web03  2.0 MB in 1 message" in lines
    assert "  here   500.0 KB in 1 message" in lines and "  web03  nothing yet" in lines
    whole = " ".join(" ".join(said).split())
    assert "Its messages are across already" in whole


def test_a_mailbox_that_is_a_message_short_is_not_called_done():
    """The sizes alone can agree to the byte and still hide a message that never came over, so the count is
    what the word goes by."""
    _mailboxes({"a0@example.nl": [1000, 1000]})
    row = _done_customer()
    found = migrate.mail_sizes(row, Mailboxes({"a0@example.nl": [1990]}))

    assert migrate._messages_cells(*found)[3] == migrate.PART
    assert migrate._mailbox_rows(row, *found)[0][3] == migrate.PART
    note = migrate._messages_notes(*found)[0]
    assert "2 messages here against 1 on the new server" in note
    assert "1 message is not across yet" in note


def test_a_mailbox_whose_messages_are_all_there_is_done_though_the_sizes_differ_a_hair():
    """imapsync hands a message over with line endings of two characters where a maildir keeps one, so the
    same mailbox weighs a little differently on each server."""
    _mailboxes({"a0@example.nl": [1000, 1000]})
    found = migrate.mail_sizes(_done_customer(), Mailboxes({"a0@example.nl": [1010, 1010]}))

    assert migrate._messages_cells(*found)[3] == migrate.DONE
    assert migrate._messages_notes(*found) == []


def test_the_sentence_names_the_messages_that_are_still_to_come():
    _mailboxes({"a0@example.nl": [1000, 1000]})
    row = _done_customer()

    said = migrate.inside(row, _status(), new_server="web03.majorlabel.nl",
                          mail=migrate.mail_sizes(row, Mailboxes({"a0@example.nl": [1990]})))

    assert "1 message still to come" in " ".join(said.split())


def test_copying_a_mailbox_that_is_a_message_short_says_how_many(the_password_is_known):
    _mailboxes({"a0@example.nl": [1000, 1000]})
    server, said = Copies(held={"a0@example.nl": [1990]}), []

    migrate._copy_mail(server, _done_customer(), {"server": "web03.majorlabel.nl"}, database="mailserver",
                       ask=lambda question: "no", say=said.append, watch=lambda *args, **named: None)

    assert "1 message still to come" in " ".join(" ".join(said).split())

# ---- Counting the messages on both servers ----------------------------------

def _counted(folders, here, there):
    return {"ok": True, "folders": folders,
            "here": {"bytes": here[0], "messages": here[1]},
            "there": {"bytes": there[0], "messages": there[1]}}


ACROSS = _counted([{"name": "INBOX", "name_there": "INBOX",
                    "here": {"bytes": 2000, "messages": 2}, "there": {"bytes": 2010, "messages": 2}}],
                  (2000, 2), (2010, 2))
SHORT = _counted([{"name": "INBOX", "name_there": "INBOX",
                   "here": {"bytes": 3000, "messages": 3}, "there": {"bytes": 2000, "messages": 2}},
                  {"name": "INBOX.Oud archief", "name_there": "INBOX.Oud archief",
                   "here": {"bytes": 500, "messages": 1}, "there": None}],
                 (3500, 4), (2000, 2))


class Counts(object):
    """The new server, counting a mailbox on both servers when it is asked to."""

    def __init__(self, answer=ACROSS):
        self.requests, self.answer = [], answer

    def ask(self, request, timeout=None):
        self.requests.append(dict(request))
        if request["request"] == "compare_mail":
            return dict(self.answer)
        return {"ok": True}

    def asked_for(self, kind):
        return [request for request in self.requests if request["request"] == kind]


def _counting(answer=ACROSS, **named):
    """Counts one customer's mailboxes and gives back (the new server, what was printed)."""
    server, said = Counts(answer), []
    migrate._compare_mail(server, _done_customer(), {"server": "web03.majorlabel.nl"}, database="mailserver",
                          say=said.append, step=lambda label, work, summary=None: work(), **named)
    return server, " ".join(" ".join(said).split())


def test_the_menu_offers_to_count_the_messages_on_both_servers():
    keys = dict((key, (what, note)) for key, what, note in migrate.customer_menu(_done_customer()))
    assert keys["c"] == ("count the messages on both servers", "")
    half = migrate.customers(_one_customer(), _status())[0]
    assert dict((key, note) for key, _, note in migrate.customer_menu(half))["c"] == "move the mail first"


def test_counting_asks_the_new_server_to_count_the_mailbox_on_both_sides(the_password_is_known):
    server, said = _counting()
    asked = server.asked_for("compare_mail")
    assert [request["address"] for request in asked] == ["a0@example.nl", "a1@example.nl"]
    assert asked[0]["password"] == "secret" and asked[0]["host"]
    assert server.asked_for("copy_mail") == []  # it counts; it must not copy
    assert "Every message of a0@example.nl is across" in said


def test_counting_names_the_folder_the_messages_are_missing_from(the_password_is_known):
    _server, said = _counting(SHORT)
    assert "2 messages of a0@example.nl are not there yet" in said
    assert "Oud archief" in said and "not there yet" in said
    assert "number 9 copies them" in said


def test_a_mailbox_whose_password_nobody_types_is_not_counted(monkeypatch, the_password_is_known):
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "")
    server, said = _counting()
    assert server.asked_for("compare_mail") == []
    assert "Nothing was counted for a0@example.nl" in said


def test_a_new_server_that_cannot_count_says_why(the_password_is_known):
    _server, said = _counting({"ok": False, "error": "imapsync counted nothing of a0@example.nl: no login"})
    assert "no login" in said


def test_the_folders_stand_in_a_table_that_fits_the_screen():
    lines = migrate._compare_rows(SHORT["folders"], "web03")
    assert all(len(line) <= 79 for line in lines)
    assert "Folder" in lines[0] and "On web03" in lines[0] and "Missing" in lines[0]
    inbox = next(line for line in lines if line.strip().startswith("INBOX"))
    assert inbox.split() == ["INBOX", "3", "2", "1"]
    # A folder under INBOX is named by what it is called, not by what it hangs under.
    assert any(line.strip().startswith("Oud archief") for line in lines)


# A mailbox that has been moved from server to server before opens its sent mail under every name it was ever
# given, and imapsync counts it once for each of them. Three names for one folder means three times the
# missing messages, which reads as mail that was never copied.
THREE_NAMES = _counted(
    [{"name": "INBOX", "name_there": "INBOX",
      "here": {"bytes": 1000, "messages": 10}, "there": {"bytes": 1000, "messages": 10}},
     {"name": "Sent", "name_there": "Sent",
      "here": {"bytes": 9000, "messages": 90}, "there": {"bytes": 8800, "messages": 88}},
     {"name": "Verzonden items", "name_there": "Verzonden items",
      "here": {"bytes": 9000, "messages": 90}, "there": {"bytes": 8700, "messages": 87}},
     {"name": "INBOX.Sent", "name_there": "INBOX.Sent",
      "here": {"bytes": 9000, "messages": 90}, "there": {"bytes": 8600, "messages": 86}}],
    (28000, 280), (27100, 271))


def test_a_folder_that_answers_to_several_names_is_counted_once():
    folders = migrate._compare_groups(THREE_NAMES["folders"])
    assert [folder["name"] for folder in folders] == ["INBOX", "Sent"]
    sent = folders[1]
    assert sent["names"] == ["Sent", "INBOX.Sent", "Verzonden items"]
    # What the new server holds in it is the fullest of the three, not the emptiest and not their sum.
    assert sent["here"] == {"bytes": 9000, "messages": 90} and sent["there"]["messages"] == 88


def test_the_missing_messages_of_a_folder_with_several_names_are_not_counted_three_times():
    said = []
    missing = migrate._compare_said("a0@example.nl", THREE_NAMES, "web03", say=said.append)
    assert missing == 2  # 90 here against the 88 of the fullest name there, not 2 + 3 + 4
    whole = " ".join(" ".join(said).split())
    assert "2 messages of a0@example.nl are not there yet" in whole
    # The totals are added up from the folders, so the same folder is not weighed three times either.
    assert "100 messages" in whole and "98 messages" in whole
    assert "1 folder here answers to more than one name" in whole


def test_a_folder_with_several_names_stands_in_the_table_once():
    lines = migrate._compare_rows(migrate._compare_groups(THREE_NAMES["folders"]), "web03")
    assert len([line for line in lines if "Sent" in line]) == 1
    assert not any("Verzonden" in line for line in lines)


def test_empty_folders_are_not_taken_for_one_another():
    """Every empty folder weighs the same as every other one, and they are still separate folders."""
    empties = [{"name": name, "name_there": name,
                "here": {"bytes": 0, "messages": 0}, "there": {"bytes": 0, "messages": 0}}
               for name in ("Drafts", "Templates", "Archive")]
    assert [folder["name"] for folder in migrate._compare_groups(empties)] == \
        ["Drafts", "Templates", "Archive"]


def test_the_command_to_copy_by_hand_maps_the_folders_under_inbox():
    """Pasted and run as it stands, it must not make the second Sent folder the copy here never makes."""
    line = " ".join(migrate._imapsync_line("178.21.114.195", "web03.majorlabel.nl", "info@a.nl"))
    assert "--automap" in line and "--regextrans2" in line
    assert "^INBOX\\.(Sent|" in line and "Verzonden items" in line


def test_counting_a_customer_whose_mail_is_not_moved_yet_says_to_move_it_first():
    said = []
    migrate._compare_mail(Counts(), migrate.customers(_one_customer(), _status())[0],
                          {"server": "web03.majorlabel.nl"}, say=said.append)
    assert "Move the mail first" in " ".join(" ".join(said).split())

class Mysql(object):
    """This server's own commands, and the SQL each one was given."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **named):
        self.calls.append((args, named.get("stdin") or ""))
        return ""

    def sql(self):
        return [given for args, given in self.calls if args[0] == "mysql"]


@pytest.fixture
def the_password_is_too_short(monkeypatch):
    """This server takes the password the customer has; the new server won't, as it is under eight letters."""
    monkeypatch.setattr(migrate, "_stored_hash", lambda address, database, run=None: "{SHA256-CRYPT}$5$a$b")
    monkeypatch.setattr(migrate, "password_fits", lambda password, stored, run=None: True)
    monkeypatch.setattr(migrate, "_typed_password", lambda prompt: "short")
    monkeypatch.setattr(migrate, "_our_addresses", lambda: {OLD_ADDRESS})


def _one_account():
    return migrate.customers([_mail("example.nl", accounts=1)], {"mail:example.nl": _there("example.nl")})[0]


def test_a_password_the_new_server_will_not_take_is_replaced_on_both_servers(
        the_password_is_too_short):
    """Its rules are its own, and a password it won't take is a mailbox imapsync can't open there."""
    _mailboxes({"a0@example.nl": [2000]})
    server, said, run = Copies(refuse_short=True), [], Mysql()

    done = migrate._copy_mail(server, _one_account(), database="mailserver", ask=lambda question: "yes",
                              say=said.append, watch=lambda *args, **named: None, run=run)

    assert done == ["a0@example.nl"]
    # Set here too, labelled the way the hash here is labelled, and it is the one imapsync is given.
    assert re.match(r"UPDATE virtual_users SET password='\{SHA512-CRYPT\}\$6\$", run.sql()[0])
    sent = [request["password"] for request in server.asked_for("password")][-1]
    assert server.asked_for("copy_mail")[0]["password"] == sent
    whole = " ".join(" ".join(said).split())
    assert sent in whole and "Write it down and give it to the customer" in whole


def test_a_new_password_the_new_server_refuses_too_is_undone_here(the_password_is_too_short):
    _mailboxes({"a0@example.nl": [2000]})
    server, said, run = Copies(refuse_passwords=True), [], Mysql()

    done = migrate._copy_mail(server, _one_account(), database="mailserver", ask=lambda question: "yes",
                              say=said.append, watch=lambda *args, **named: None, run=run)

    assert done == [] and server.asked_for("copy_mail") == []
    assert "{SHA256-CRYPT}$5$a$b" in run.sql()[-1]  # the hash it had is back
    whole = " ".join(" ".join(said).split())
    assert "is as it was" in whole and "the same password has to open the mailbox on both servers" in whole


def test_saying_no_to_a_new_password_changes_nothing():
    server, said, run = Copies(refuse_short=True), [], Mysql()

    assert migrate.new_password_on_both(server, "a0@example.nl", "mailserver", say=said.append,
                                        ask=lambda question: "no", run=run) is None

    assert run.calls == [] and server.requests == []
    assert "keeps the password it has" in " ".join(" ".join(said).split())


# ---- A part that is here but cannot be moved ----

def test_a_part_that_is_here_but_cannot_be_moved_doesnt_say_there_is_nothing_to_do():
    """A cron job whose paths point outside the site can't be moved, and the reason stands above the menu.
    Saying "nothing to do" beside its number reads as if the tool had never seen the job."""
    row = migrate.Customer("gvv-geldermalsen.nl")
    row.add("cron", {"kind": "cron", "name": "sportlink", "user": "gvv",
                     "problem": migrate.OUTSIDE_SITE % "/home/gvv/sportlink-cron.log"}, None)

    notes = dict((number, note) for number, _, note in migrate.customer_menu(row))

    assert notes["3"] == migrate.STUCK_NOTE
    assert migrate.why_nothing(row, "3") == "Nothing of that can be moved. Why not is in the note above the menu."


def test_a_part_this_server_has_nothing_of_still_says_there_is_nothing_to_do():
    row = migrate.Customer("gvv-geldermalsen.nl")
    row.add("site", {"kind": "site", "name": "gvv-geldermalsen.nl", "user": "gvv"}, migrate.TO_DO)

    notes = dict((number, note) for number, _, note in migrate.customer_menu(row))

    assert notes["3"] == "nothing to do"
    assert migrate.why_nothing(row, "3") == "There is nothing to do for that."


# ---- What the new server said, relayed to this screen ----

REPAIRED = """  ✗ MX           Mail is delivered to mail.a.nl, which isn't this server.
    The records that put this right:
    A mail.a.nl
      187.126.5.158
  ! SRV          There are no SRV records, so mail programs can't look them up.
    SRV _imaps._tcp.a.nl
      0 1 993 mail.a.nl.
! a.nl isn't in the TransIP account majorlabel.
  See the records to publish by hand with: mailctl dns show a.nl
✓ The certificate has 12 names: web03.majorlabel.nl, mail.b.nl, mail.c.nl, mail.d.nl, mail.e.nl, mail.f.nl.
"""


def test_what_the_new_server_said_comes_back_as_a_few_lines_not_a_wall():
    """Every check there lists the records that would put it right. Nobody types those in from this screen,
    and five checks failing for one reason is five sentences saying it."""
    said = []

    problems = migrate._say_relayed(REPAIRED, said.append)

    assert problems == ["MX", "SRV"]
    text = "\n".join(said)
    assert text.splitlines()[0] == "Still not right: MX, SRV."
    assert "a.nl isn't in the TransIP account majorlabel" in text
    assert "mailctl dns show a.nl" in text  # the one thing to do, whole, on one line
    assert "187.126.5.158" not in text and "0 1 993" not in text  # the records themselves are left out
    assert len(said) <= 8
    assert all(len(line) <= 79 for line in said)
    assert "Those commands run on the new server, not here." in text




# ---- The list of what is still to do, over every customer -------------------

def _todo_rows():
    """Three customers: one whose mail is half there, one whose redirect isn't set up, and one that is
    finished as far as the board can tell."""
    items = [_mail("half.nl", accounts=3), _mail("done.nl", accounts=1),
             _site("done.nl"), dict(_site("oud.nl"), wordpress=False, database=None,
                                    redirect={"to": "https://new.nl/", "rules": ["Redirect 301 / https://new.nl/"],
                                              "by_hand": []})]
    status = {"mail:half.nl": _there("half.nl", accounts=1), "mail:done.nl": _there("done.nl"),
              "site:done.nl": {"state": "exists", "detail": "there"},
              "site:oud.nl": {"state": "new", "detail": "not here"}}
    return migrate.customers(items, status), status


class Looks(Copies):
    """The new server, and a world that still sends everything to this one."""

    def __init__(self, held=None):
        Copies.__init__(self, held=held)


def _dns_here(monkeypatch, where=None):
    monkeypatch.setattr(migrate, "dns_seen", lambda row, hello, run=None: dict(
        {"here": [migrate.WEBSITE], "there": [], "away": [], "unknown": [], "mail_there": []}, **(where or {})))


def test_the_todo_list_says_what_the_board_has_no_column_for(monkeypatch):
    """The board says where the mail, the website and the jobs of each customer stand. Whether the messages
    are across, whether a redirect is set up, whether this server still delivers mail it has handed over and
    where the names point are the rest of the move, and this is where they are said."""
    _mailboxes({"a0@done.nl": [2000000]})
    _dns_here(monkeypatch)
    monkeypatch.setattr(migrate, "kept_here", lambda seen, run=None: ["done.nl"])
    rows, status = _todo_rows()

    found = migrate.todo(rows, status, Looks(), {}, short="web03")

    assert [row.name for row, _what in found["mail"]] == ["half.nl"]
    assert dict((row.name, what) for row, what in found["mail"])["half.nl"] == "1 of 3 accounts on web03"
    assert [row.name for row, _what in found["messages"]] == ["done.nl"]
    assert [row.name for row, _what in found["redirect"]] == ["oud.nl"]
    assert [what for _row, what in found["redirect"]] == ["redirect to new.nl"]
    assert [row.name for row, _what in found["delivery"]] == ["done.nl", "half.nl", "oud.nl"]
    assert [what for _row, what in found["dns"]] == ["website"] * 3


def test_a_customer_with_nothing_left_anywhere_is_in_no_section(monkeypatch):
    _dns_here(monkeypatch, {"here": [], "there": [migrate.WEBSITE, migrate.MAIL]})
    monkeypatch.setattr(migrate, "kept_here", lambda seen, run=None: [])
    rows, status = _todo_rows()

    found = migrate.todo([row for row in rows if row.name == "done.nl"], status, Looks(), {})

    assert all(not lines for lines in found.values())
    said = []
    migrate.show_todo(found, say=said.append)
    assert migrate.TODO_DONE in " ".join(" ".join(said).split())


def test_the_list_puts_each_customers_own_number_in_front_of_it(monkeypatch):
    _dns_here(monkeypatch)
    monkeypatch.setattr(migrate, "kept_here", lambda seen, run=None: [])
    rows, status = _todo_rows()
    said = []

    migrate.show_todo(migrate.todo(rows, status, Looks(), {}), say=said.append)

    lines = [line for line in " \n".join(said).splitlines() if line.startswith("  ")]
    assert lines and all(re.match(r"^\s+[0-9]+\s+\S", line) for line in lines)
    assert any(line.split()[1] == "oud.nl" for line in lines)
    assert all(len(line) <= 100 for line in lines)


# ---- What the board says about a customer with nothing left to move ---------

class Dig:
    """The world's answers: which names come here, which go to the new server, and what Postfix here does."""

    def __init__(self, here=(), mail_here=(), maps="", answers=None):
        self.here, self.mail_here = set(here), set(mail_here)
        self.maps, self.answers, self.calls = maps, answers or {}, []

    def __call__(self, *args, **named):
        self.calls.append(args)
        if args[:3] == ("postconf", "-h", "transport_maps"):
            return self.maps + "\n"
        if args[:2] == ("postmap", "-q"):
            return self.answers.get(args[3], "")
        if args[0] != "dig":
            raise migrate.CommandFailed("no " + args[0], missing=True)
        kind, name = args[4], args[5]
        if kind == "A":
            return (OLD_ADDRESS if name in self.here else NEW_ADDRESS) + "\n"
        if kind == "MX":
            return "10 mail.%s.\n" % name
        return ""


def _mail_at(monkeypatch, dig):
    """mail.<domain> resolves to the old server for the domains whose mail is still delivered here."""
    monkeypatch.setattr(migrate, "_addresses_of", lambda name, look=None: set())
    monkeypatch.setattr(migrate, "_lookup", lambda run, kind, name, server: (
        [OLD_ADDRESS] if kind in ("A", "AAAA") and name.startswith("mail.")
        and name[5:] in dig.mail_here else
        [NEW_ADDRESS] if kind in ("A", "AAAA") and name.startswith("mail.") else
        ["10 mail.%s" % name] if kind == "MX" else
        [OLD_ADDRESS] if kind == "A" and name in dig.here else
        [NEW_ADDRESS] if kind == "A" else []))


def test_a_customer_whose_parts_are_all_across_is_not_called_finished_while_the_world_comes_here(monkeypatch):
    """Its mail and its website are on the new server and there is no item left, and yet every message still
    arrives here. The board called that "nothing to move", which reads as done."""
    items = [_mail("nielsvanlaar.nl"), _site("nielsvanlaar.nl")]
    status = dict(_status(**{"site:nielsvanlaar.nl": "exists"}),
                  **{"mail:nielsvanlaar.nl": _there("nielsvanlaar.nl")})
    row = migrate.customers(items, status)[0]
    dig = Dig(mail_here=["nielsvanlaar.nl"])
    _mail_at(monkeypatch, dig)

    migrate.quick_look([row], [NEW_ADDRESS], ours={OLD_ADDRESS}, run=dig)

    assert migrate.still_here("nielsvanlaar.nl") == migrate.QUICK_NOTES[("mail", "here")]
    assert migrate.next_step(row) == ([], "point the mail's DNS")
    board = migrate.dashboard(items, status)
    assert "point the mail's DNS" in board
    assert "1 done" not in board  # it is not done, so it is not counted as done


def test_a_customer_the_world_already_sends_to_the_new_server_says_nothing_to_move(monkeypatch):
    row = migrate.customers([_mail("done.nl"), _site("done.nl")],
                            dict(_status(**{"site:done.nl": "exists"}),
                                 **{"mail:done.nl": _there("done.nl")}))[0]
    dig = Dig(maps="hash:%s" % migrate.TRANSPORT_FILE,
              answers={"hash:%s" % migrate.TRANSPORT_FILE: "smtp:[web03]"})
    _mail_at(monkeypatch, dig)

    migrate.quick_look([row], [NEW_ADDRESS], ours={OLD_ADDRESS}, run=dig)

    assert migrate.still_here("done.nl") is None
    assert migrate.next_step(row) == ([], "nothing to move")


def test_mail_the_world_sends_on_while_this_server_keeps_it_is_on_the_board_too(monkeypatch):
    row = migrate.customers([_mail("done.nl")], {"mail:done.nl": _there("done.nl")})[0]
    dig = Dig()  # the MX is the new server, and no transport map sends it there
    _mail_at(monkeypatch, dig)

    migrate.quick_look([row], [NEW_ADDRESS], ours={OLD_ADDRESS}, run=dig)

    assert migrate.still_here("done.nl") == migrate.HAND_OVER


def test_the_quick_look_asks_the_public_resolver_once_per_name(monkeypatch):
    """A customer's own screen asks that customer's own name servers for every name it has; this is a line in
    a table, for every customer at once, so it is the cheap version."""
    rows = migrate.customers([_mail("a.nl"), _site("a.nl")], _status())
    asked = []
    monkeypatch.setattr(migrate, "_lookup",
                        lambda run, kind, name, server: asked.append((kind, name, server)) or [NEW_ADDRESS])
    monkeypatch.setattr(migrate, "handed_over", lambda domain, run=None: True)

    migrate.quick_look(rows, [NEW_ADDRESS], ours={OLD_ADDRESS}, run=lambda *a, **k: "")

    assert [(kind, name) for kind, name, _server in asked] == [
        ("A", "a.nl"), ("MX", "a.nl"), ("A", "187.126.5.158"), ("AAAA", "187.126.5.158")]
    assert all(server == migrate.PUBLIC_RESOLVER for _kind, _name, server in asked)


REPAIR_THAT_COULD_NOT = """\
  ✗ MX           Mail is delivered to mail.a.nl, which isn't this server.
    The records that put this right:
    A mail.a.nl
      187.126.5.158
  ! SRV          There are no SRV records, so mail programs can't look them up.
! TransIP: Internal error occurred, please contact our support (HTTP 500 on POST /auth)
TransIP's own API is failing, not this server, and nothing was changed. Try again later.
✓ Put mail.a.nl in this server's certificate.
"""


def test_why_a_repair_could_not_finish_is_said_and_not_only_what_is_wrong():
    """The check afterwards says what is still wrong; only the repair itself knows what stopped it."""
    row, said = _done_customer(), []
    server = Repairs({"ok": True, "right": False, "repair": REPAIR_THAT_COULD_NOT,
                      "text": "example.nl\n  ✗ MX  Mail is delivered elsewhere.\n"})

    migrate._repair(server, row, say=said.append)

    text = " ".join(" ".join(said).split())
    assert "TransIP: Internal error occurred" in text
    assert "TransIP's own API is failing" in text  # the sentence under it, which says what to do
    assert "Still not right: MX." in text
    # The checks it listed before it did anything are indented, and stay out of the reasons.
    assert migrate._repair_reasons(REPAIR_THAT_COULD_NOT) == [
        "TransIP: Internal error occurred, please contact our support (HTTP 500 on POST /auth) "
        "TransIP's own API is failing, not this server, and nothing was changed. Try again later."]


def test_a_repair_that_finished_says_nothing_about_why():
    row, said = _done_customer(), []
    server = Repairs({"ok": True, "right": True, "text": "✓ example.nl  everything is set up.\n"})

    migrate._repair(server, row, say=said.append)

    assert "TransIP" not in " ".join(said)


def test_imapsyncs_own_word_settles_whether_a_mailbox_is_across():
    """The files in a maildir are not the messages: dovecot doesn't serve every one of them, so the two
    servers never add up to the same number. imapsync opened both mailboxes; its count is the one that counts."""
    _mailboxes({"a0@example.nl": [1000] * 10}, age=7200)
    row = _done_customer()
    there = {"a0@example.nl": {"here": True, "bytes": 9000, "messages": 9,
                               "sync": {"good": True, "left": 0, "transferred": 0, "errors": 0, "ago": 60}}}
    found = migrate.mail_sizes(row, Mailboxes(answer={"ok": True, "mailboxes": there}))

    assert migrate._messages_cells(*found)[3] == migrate.DONE
    note = migrate._messages_notes(*found)[0]
    assert "nothing is missing" in note and "doesn't serve as messages" in note


def test_a_sync_that_still_found_messages_here_says_how_many():
    _mailboxes({"a0@example.nl": [1000] * 10})
    row = _done_customer()
    there = {"a0@example.nl": {"here": True, "bytes": 4000, "messages": 4,
                              "sync": {"good": False, "left": 3, "transferred": 4, "errors": 0, "ago": 60}}}
    found = migrate.mail_sizes(row, Mailboxes(answer={"ok": True, "mailboxes": there}))

    assert migrate._messages_cells(*found)[3] == migrate.PART
    # Three, which is what imapsync saw, and not the six the files would suggest.
    assert "3 messages are not across yet" in migrate._messages_notes(*found)[0]


def test_mail_that_came_in_after_the_copy_is_still_to_copy():
    """"Everything is across" was true when imapsync ran. A message that landed here since is not across, and
    a row that still said done would hide it for good."""
    _mailboxes({"a0@example.nl": [1000] * 10})  # the newest of them landed just now
    row = _done_customer()
    there = {"a0@example.nl": {"here": True, "bytes": 9000, "messages": 9,
                               "sync": {"good": True, "left": 0, "transferred": 9, "errors": 0, "ago": 3600}}}
    found = migrate.mail_sizes(row, Mailboxes(answer={"ok": True, "mailboxes": there}))

    assert migrate._messages_cells(*found)[3] == migrate.PART


def test_the_folders_that_are_short_stand_first_and_the_rest_are_one_line():
    """A mailbox can have a hundred folders. What is missing has to be on the screen, not on page four."""
    folders = [{"name": "INBOX.Folder%02d" % number, "name_there": "INBOX.Folder%02d" % number,
                "here": {"bytes": 10, "messages": 1}, "there": {"bytes": 10, "messages": 1}}
               for number in range(migrate.COMPARE_ROWS + 5)]
    folders.append({"name": "INBOX.Cliënten", "name_there": "INBOX.Cliënten",
                    "here": {"bytes": 900, "messages": 9}, "there": {"bytes": 200, "messages": 2}})
    folders.append({"name": "INBOX.Oud", "name_there": "INBOX.Oud",
                    "here": {"bytes": 50, "messages": 5}, "there": None})

    lines = migrate._compare_rows(folders, "web03")

    assert all(len(line) <= 79 for line in lines)
    assert lines[2].strip().startswith("Cliënten")  # the one that is short, above the rule's two lines
    assert lines[3].strip().startswith("Oud")  # then the one that isn't there at all
    assert len([line for line in lines if line.strip()]) == migrate.COMPARE_ROWS + 3
    assert "and 7 more, all across" in lines[-1]


def test_the_board_does_not_call_the_mail_done_while_the_messages_are_here():
    """The accounts being on the new server is not the mail being there: without this the row says done and
    offers to point the mail's DNS at a server that hasn't got the messages."""
    _mailboxes({"a0@example.nl": [1000] * 10})
    row = _done_customer()
    there = {"a0@example.nl": {"here": True, "bytes": 2000, "messages": 2}}
    migrate.mail_sizes(row, Mailboxes(answer={"ok": True, "mailboxes": there}))

    assert migrate.messages_behind(row.name) == 8
    assert migrate._board_word(row, "mail") == migrate.PART
    assert migrate.next_step(row)[1] == "copy the messages"
    assert migrate._nothing_left(row) is False


def test_a_customer_whose_messages_are_across_is_left_alone():
    _mailboxes({"a0@example.nl": [1000] * 10})
    row = _done_customer()
    there = {"a0@example.nl": {"here": True, "bytes": 10000, "messages": 10}}
    migrate.mail_sizes(row, Mailboxes(answer={"ok": True, "mailboxes": there}))

    assert migrate.messages_behind(row.name) == 0
    assert migrate._board_word(row, "mail") == migrate.DONE


def test_a_customer_nobody_measured_says_nothing_about_its_messages():
    """A guess here would be the same mistake the other way round."""
    migrate.forget_sizes("example.nl")
    assert migrate.messages_behind("example.nl") is None
    assert migrate._board_word(_done_customer(), "mail") == migrate.DONE
