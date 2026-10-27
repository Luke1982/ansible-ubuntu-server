"""The screen the tool is worked from: one customer, one number, one next step.

The per-item screen that `migrate.py list` prints (items_board) has its own tests in
test_migrate_dashboard.py; these are about the customers, the four words their columns say, what a number does,
and the new mail passwords.
"""

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
    items = _one_customer() + [_mail("other.nl"), _plain("oude-site.nl"),
                               _site("no-root.nl", database=None, docroot=None, wordpress=False,
                                     problem="Its configuration has no DocumentRoot, so its files "
                                             "can't be found."),
                               _cron("root", site=None, problem=migrate.NO_SITE_JOB),
                               {"id": "aside:webmail.a.nl", "kind": "aside", "name": "webmail.a.nl"}]
    status = _status(**dict((item["id"], "new") for item in items if item["kind"] != "aside"))
    rows = migrate.customers(items, status)
    assert [(row.name, row.number) for row in rows] == [("example.nl", 1), ("other.nl", 2)]
    text = migrate.dashboard(items, status, old_server="vps1157", new_server="web03.majorlabel.nl")
    for left_out in ("oude-site.nl", "no-root.nl", "webmail.a.nl", "/usr/lib", "wp-cron"):
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


def test_every_column_says_one_of_four_words_and_nothing_else(tmp_path):
    items = [_mail("one.nl"), _site("one.nl", 10), _cron("one.nl"),
             _mail("two.nl", accounts=2), _site("two.nl", 10), _mail("three.nl"), _plain("three.nl")]
    notes = _notes(tmp_path, **{"site:one.nl": {"status": "made", "docroot": "/home/one"}})
    status = dict(_status(**{"site:one.nl": "exists", "site:two.nl": "new", "mail:three.nl": "new"}),
                  **{"mail:one.nl": _there("one.nl"), "mail:two.nl": _there("two.nl", accounts=1)})
    text = migrate.dashboard(items, status, notes, new_server="web03.majorlabel.nl")
    assert _cells(text, "one.nl") == ["1", "one.nl", "done", "part", "to do", "finish the website"]
    assert _cells(text, "three.nl") == ["2", "three.nl", "to do", "-", "-", "move the mail"]
    assert _cells(text, "two.nl") == ["3", "two.nl", "part", "to do", "-", "finish the mail"]
    words = set()
    for row in text.splitlines():
        cells = [cell for cell in re.split(r"\s{2,}", row.strip()) if cell]
        if cells and cells[0].isdigit():
            words.update(cells[2:-1])
    assert words <= {"to do", "part", "done", "-"}


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
    assert _cells(text, "a.nl") == ["1", "a.nl", "done", "done", "nothing left"]
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
    assert ("Also, only reported: 15 names with nothing to move, 15 sites that are not WordPress, "
            "16 cron jobs of this server itself, 28 webmail names, 18 databases that belong to no "
            "site. See: migrate.py list" in " ".join(text.split()))


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


def test_a_customer_with_nothing_left_says_so_inside(tmp_path):
    items = [_mail("a.nl")]
    notes = _notes(tmp_path, **{"mail:a.nl": {"status": "moved"}})
    status = {"mail:a.nl": _there("a.nl")}
    row = migrate.customers(items, status, notes)[0]
    assert "Nothing left to do for this customer." in migrate.inside(row, status, notes)


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
    assert "Moving 1 of 3: a.nl" in text and "Moving 3 of 3: c.nl" in text
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
        "q to leave:")
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
    done = migrate._point_dns(connection, row, ask=lambda question: True, say=said.append)
    assert done == ["example.nl"]
    assert connection.requests[-1] == {"request": "point_dns", "domain": "example.nl"}
    text = " ".join(" ".join(said).split())  # it is wrapped to the width
    assert "Visitors go to the new server" in text and "domainctl dns credentials" in text


def test_saying_no_to_the_dns_move_sends_nothing():
    row = migrate.customers(_one_customer(), _status(**{"site:example.nl": "exists"}))[0]
    connection, said = Connection(), []
    assert migrate._point_dns(connection, row, ask=lambda question: False, say=said.append) == []
    assert connection.requests == []
    assert "left alone" in " ".join(said)


def test_a_customer_with_no_website_has_no_dns_to_move():
    row = migrate.customers([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))[0]
    connection, said = Connection(), []
    assert migrate._point_dns(connection, row, ask=lambda question: True, say=said.append) == []
    assert connection.requests == [] and "no website here" in " ".join(said)
