import pytest

from migrate import migrate


def _site(name, size=None, database="db1"):
    return {"id": "site:" + name, "kind": "site", "name": name, "user": name.split(".")[0], "database": database,
            "size": size}


def _mail(name):
    return {"id": "mail:" + name, "kind": "mail_domain", "name": name,
            "data": {"addresses": [{"address": "a@" + name}], "forwards": []}}


def _status(**states):
    return {key: {"state": state, "detail": "%s: %s" % (key, state)} for key, state in states.items()}


def _cron(name, site="keep"):
    site = name if site == "keep" else site
    return {"id": "cron:" + name, "kind": "cron", "user": name.split(".")[0], "old_user": name.split(".")[0],
            "site": site, "line": "*/15 * * * * php /var/www/%s/wp-cron.php" % name, "problem": None}


class Connection(object):
    def __init__(self, replies):
        self.replies, self.requests = replies, []

    def ask(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_the_worst_case_fits_eighty_columns_and_no_row_loses_its_number_column():
    name = "a-very-long-name-for-a-site." + ("b" * 60 + ".") * 3 + "com"
    items = [_site(name, 3 * 10 ** 9), _mail("e" * 60 + ".example.com"),
             dict(_cron("example.nl"), line="* * * * * " + "x" * 200)]
    status = _status(**{items[0]["id"]: "refused", items[1]["id"]: "new", items[2]["id"]: "new"})
    status[items[0]["id"]]["detail"] = "A very long reason. " * 20
    text = migrate.dashboard(items, status, new_server="w" * 100 + ".example.com", old_server="o" * 100,
                             addresses=["187.126.5.65", "2001:db8::1"])
    assert all(len(line) <= 79 for line in text.splitlines())
    assert "MAIL" in text and "WEBSITES" in text and "CRON JOBS" in text
    assert "A very long reason." in text  # the reason is under its row, in full


def test_the_dashboard_is_grouped_one_section_per_kind_with_the_new_servers_answer_in_a_column():
    items = [_site("example.nl", 13 * 10 ** 8), _mail("example.nl"),
             dict(_site("static.nl"), wordpress=False, problem="No wp-config.php was found in /var/www/static."),
             _cron("example.nl"),
             dict(_cron("root", site=None), problem=migrate.NO_SITE_JOB, line="0 3 * * * /usr/bin/backup.sh")]
    status = _status(**dict((i["id"], "new") for i in items))
    status[items[1]["id"]] = {"state": "exists", "detail": "example.nl is already here, with 2 accounts and 1 forwards."}
    text = migrate.dashboard(items, status, old_server="old.example.nl", new_server="web03.majorlabel.nl",
                             addresses=["187.126.5.65"])
    lines = text.splitlines()
    assert lines[0] == "Old server  old.example.nl"
    assert lines[1] == "New server  web03.majorlabel.nl (187.126.5.65)"
    assert [l for l in lines if l.isupper()] == ["MAIL", "WEBSITES", "CRON JOBS"]
    assert "On web03" in text and "To copy" in text
    assert "2 accounts, 1 forward - can be topped up" in text  # nouns agree with their counts
    assert "1.3 GB + files" in text and "not there yet" in text
    assert "every 15 minutes" in text
    assert "not in example's crontab" in text
    assert "static.nl" in text and "not WordPress" in text
    assert text.splitlines()[-1] == "3 of 4 can be moved. Type: migrate.py move 1-3"
    assert "Why some can't be moved" not in text and "What it takes" not in text


def test_only_what_can_be_moved_is_numbered_and_a_site_that_is_not_wordpress_never_is():
    items = [dict(_site("static.nl"), wordpress=False, problem="No wp-config.php was found in /var/www/static."),
             _site("shop.nl"), _mail("a.nl")]
    status = _status(**dict((i["id"], "new") for i in items))
    text = migrate.dashboard(items, status)
    row = [l for l in text.splitlines() if "static.nl" in l][0]
    assert row.split()[0] == "-"
    assert migrate.offered(items, status) == [items[2], items[1]]  # the mail first, as it is shown


def test_a_section_with_nothing_in_it_is_left_out_with_its_heading():
    text = migrate.dashboard([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))
    assert "MAIL" in text and "WEBSITES" not in text and "CRON JOBS" not in text
    assert migrate.dashboard([], {}) == "Nothing was found on this server to move."


def test_what_the_new_server_already_has_is_shown_but_not_offered():
    items = [_site("old.nl"), _site("new.nl", 5 * 10 ** 6), _site("bad.nl")]
    status = _status(**{"site:old.nl": "exists", "site:new.nl": "new", "site:bad.nl": "refused"})
    text = migrate.dashboard(items, status)
    rows = dict((l.split()[1], l) for l in text.splitlines() if l[:7].strip() in ("-", "1"))
    assert rows["old.nl"].split()[0] == "-" and "already on the new server" in rows["old.nl"]
    assert rows["new.nl"].split()[0] == "1" and "5.0 MB" in rows["new.nl"]
    assert rows["bad.nl"].split()[0] == "-" and "can't be moved" in rows["bad.nl"]
    assert migrate.offered(items, status) == [items[1]]


def test_one_reason_that_many_rows_share_is_said_once_with_their_names():
    reason = "No wp-config.php was found in /var/lib/roundcube."
    items = [dict(_site("w%d.nl" % n), wordpress=False, problem=reason) for n in range(5)]
    text = migrate.dashboard(items, _status(**dict((i["id"], "new") for i in items)))
    assert text.count(reason) == 1
    assert "Applies to: w0.nl, w1.nl, w2.nl, w3.nl, w4.nl" in " ".join(text.split())


def test_the_old_servers_own_webmail_is_one_counted_line_and_never_a_site():
    plumbing = [{"name": "webmail.a.nl", "docroot": "/var/lib/roundcube"},
                {"name": "x.nl", "docroot": "/usr/share/z-push/"}, {"name": "autodiscover.a.nl", "docroot": "/x"},
                {"name": "autoconfig.a.nl", "docroot": "/x"}, {"name": "mail.a.nl", "docroot": "/x"}]
    assert all(migrate.is_plumbing(site) for site in plumbing)
    assert not migrate.is_plumbing({"name": "shop.a.nl", "docroot": "/var/www/shop"})
    items = [_site("shop.a.nl")] + [{"id": "aside:" + s["name"], "kind": "aside", "name": s["name"]}
                                    for s in plumbing]
    status = _status(**{"site:shop.a.nl": "new"})
    text = migrate.dashboard(items, status)
    assert "webmail.a.nl" not in text and "5 addresses" in text and "1 of 1 can be moved" in text
    assert migrate.offered(items, status) == [items[0]]


def test_jobs_that_belong_to_no_site_are_one_counted_line_and_only_list_names_them():
    loose = [dict(_cron("root", site=None), id="cron:%d" % n, problem=migrate.NO_SITE_JOB,
                  line="0 3 * * * /usr/lib/job%d/run" % n) for n in range(3)]
    items = [_cron("example.nl")] + loose
    status = _status(**dict((i["id"], "new") for i in items))
    text = migrate.dashboard(items, status)
    assert "3 more jobs belong to no site" in text and "job1" not in text
    assert "1 of 1 can be moved" in text
    detail = migrate.dashboard(items, status, detail=True)
    assert "root: daily at 03:00  /usr/lib/job1/run" in detail
    assert all(len(line) <= 79 for line in detail.splitlines())


@pytest.mark.parametrize("fields, words", [
    (["*/15", "*", "*", "*", "*"], "every 15 minutes"),
    (["0", "3", "*", "*", "*"], "daily at 03:00"),
    (["30", "4", "*", "*", "1"], "every Monday at 04:30"),
    (["5", "4", "*", "1,6", "1-5"], "5 4 * 1,6 1-5"),
    (["@daily"], "daily at 00:00"),
])
def test_a_schedule_is_said_in_words_or_left_as_the_five_fields(fields, words):
    assert migrate.schedule_in_words(fields) == words


def test_a_cron_command_is_cut_from_the_left_so_the_script_name_stays():
    command = "/opt/plesk/php/8.1/bin/php " + "/very/long/path" * 10 + "/wp-cron.php"
    item = dict(_cron("example.nl"), line="*/15 * * * * " + command)
    text = migrate.dashboard([item], _status(**{item["id"]: "new"}))
    shown = [l for l in text.splitlines() if l.strip().startswith("...")][0]
    assert shown.endswith("wp-cron.php") and len(shown) <= 79


def test_colour_is_off_when_not_a_terminal_and_when_no_color_is_set(monkeypatch):
    class Terminal(object):
        def isatty(self):
            return True

    class Pipe(object):
        def isatty(self):
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert migrate.use_colour(Terminal()) is True and migrate.use_colour(Pipe()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert migrate.use_colour(Terminal()) is False
    plain = migrate.dashboard([_mail("a.nl")], _status(**{"mail:a.nl": "new"}))
    assert "\x1b" not in plain
    coloured = migrate.dashboard([_mail("a.nl")], _status(**{"mail:a.nl": "new"}), colour=True)
    assert "\x1b[1m" in coloured
    import re
    assert re.sub(r"\x1b\[[0-9;]*m", "", coloured) == plain  # the colour carries no words


class _Out(object):
    def __init__(self, tty):
        self.tty, self.text = tty, ""

    def isatty(self):
        return self.tty

    def write(self, text):
        self.text += text

    def flush(self):
        pass


def test_the_loader_prints_one_finished_line_with_no_spinner_when_not_a_terminal():
    out = _Out(False)
    assert migrate._step("Looking at the mail", lambda: 7, lambda n: "%d domains" % n, out) == 7
    assert out.text == "Looking at the mail ... 7 domains\n"


def test_the_loader_finishes_its_line_when_the_step_raises_and_the_error_goes_on():
    for tty in (False, True):
        out = _Out(tty)

        def broken():
            raise RuntimeError("no database")

        with pytest.raises(RuntimeError):
            migrate._step("Looking at the mail", broken, out=out)
        assert out.text.endswith("failed\x1b[K\n" if tty else "failed\n") and out.text.count("\n") == 1
        out = _Out(tty)

        def stopped():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            migrate._step("Looking at the cron jobs", stopped, out=out)
        assert out.text.endswith("stopped\x1b[K\n" if tty else "stopped\n")
    assert "\r" not in _Out(False).text


def test_on_a_terminal_the_loader_spins_with_carriage_returns_and_ends_on_a_whole_line():
    import time
    out = _Out(True)
    migrate._step("Asking web03 what it has", lambda: time.sleep(0.25), lambda _: "answered", out)
    assert "\r" in out.text and out.text.endswith("Asking web03 what it has ... answered\x1b[K\n")
    assert out.text.count("\n") == 1


def test_each_subcommand_reaches_its_own_work_and_nothing_else(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(migrate, "own_key", lambda *a: calls.append("key") or "ssh-ed25519 AAAA migrate")
    monkeypatch.setattr(migrate, "Connection", lambda host: object())
    monkeypatch.setattr(migrate, "check", lambda connection, show_key=True: calls.append("check") or
                        {"server": "web03", "tools": {"mailctl": True, "domainctl": True}, "version": 2})
    monkeypatch.setattr(migrate, "State", lambda: calls.append("state") or None)
    monkeypatch.setattr(migrate, "_list", lambda *a, **k: calls.append("list"))
    result = migrate.Report()
    monkeypatch.setattr(migrate, "_work", lambda args, *a, **k: calls.append(
        ("work", args.command, args.numbers, args.domain, args.again)) or result)
    monkeypatch.setattr(migrate, "_result_from_notes", lambda state: calls.append("notes") or result)
    root = lambda: 0
    assert migrate.main(["key"], geteuid=root) == 0
    assert calls == ["key"] and "ssh-ed25519 AAAA" in capsys.readouterr().out
    del calls[:]
    assert migrate.main(["check", "--host", "new"], geteuid=root) == 0
    assert calls == ["key", "check"] and "web03" in capsys.readouterr().out
    del calls[:]
    assert migrate.main(["list", "--host", "new"], geteuid=root) == 0
    assert calls == ["key", "check", "state", "list"]
    assert "ssh-ed25519" not in capsys.readouterr().out  # the key is for `key` and for a refused login only
    del calls[:]
    assert migrate.main(["move", "1-3", "5", "--host", "new"], geteuid=root) == 0
    assert calls[-1] == ("work", "move", ["1-3", "5"], None, False)
    del calls[:]
    assert migrate.main(["again", "a.nl", "--host", "new"], geteuid=root) == 0
    assert calls[-1] == ("work", "again", [], "a.nl", True)
    del calls[:]
    assert migrate.main(["report"], geteuid=root) == 0
    assert calls == ["state", "notes"]
    capsys.readouterr()


def test_bare_migrate_prints_the_help_and_does_nothing(capsys):
    assert migrate.main([], geteuid=lambda: 1) == 0
    text = capsys.readouterr().out
    for word in ("key", "check", "list", "move", "again", "report"):
        assert word in text


def test_a_move_says_what_was_picked_and_what_will_happen_before_it_asks(capsys):
    mail = dict(_mail("a.nl"), data={"addresses": [{}, {}, {}], "forwards": [{}]})
    site = dict(_site("b.nl", 52 * 10 ** 6), user="b")
    job = dict(_cron("b.nl"), user="b")
    lines = [mail, site, job]
    asked = []
    assert migrate._confirmed(lines, lines, lambda prompt: asked.append(prompt) or "yes") is True
    text = " ".join(capsys.readouterr().out.split())
    assert "3 accounts and 1 forward" in text and "filters and spam settings come too" in text
    assert "its files and its database of 52.0 MB are copied" in text
    assert "goes into the crontab of b on the new server" in text
    assert asked == ["Go ahead? Type yes or no: "]
    assert migrate._confirmed(lines, lines, lambda prompt: "no") is False


def test_the_report_after_one_mail_domain_is_about_that_move_only():
    mail = migrate.Mail({"domains": ["a.nl"]}, ["The forward from @a.nl: a catch-all.",
                                                "The domain Bad_Name.nl: it isn't valid.",
                                                "The server-wide spam setting x is 4 on this server."], [])
    result = migrate.Report()
    result.moved = [(_mail("a.nl"), {})]
    kwargs = dict(mail=mail, found_sites=[{"name": "plain.nl", "wordpress": False, "docroot": "/w"}],
                  databases={"stray": 1}, notes=["doveadm is missing."],
                  items=[dict(_cron("root", site=None), problem=migrate.NO_SITE_JOB)])
    text = migrate.report(result, **kwargs)
    assert "a.nl" in text and "catch-all" in text
    for left_out in ("plain.nl", "stray", "doveadm", "Bad_Name", "server-wide", "belong to no site"):
        assert left_out not in text
    everything = migrate.report(result, everything=True, **kwargs)
    for there in ("plain.nl", "stray", "doveadm", "Bad_Name", "server-wide", "belong to no site"):
        assert there in everything


def test_list_shows_what_a_move_does_not_print(capsys, monkeypatch):
    connection, state = _wired(monkeypatch, [])
    migrate._list(_Args(), connection, state, ["a note from looking around"])
    text = capsys.readouterr().out
    assert "MAIL" in text and "plain.nl" in text and "loose_db" in text and "a note from looking around" in text


def test_numbers_ranges_and_all_pick_the_right_items():
    lines = [{"id": str(n)} for n in range(1, 7)]
    said = []
    assert migrate.pick(lines, "1 3", said.append) == [lines[0], lines[2]]
    assert migrate.pick(lines, "2-4,6", said.append) == [lines[1], lines[2], lines[3], lines[5]]
    assert migrate.pick(lines, "all", said.append) == lines
    assert said == []
    assert migrate.pick(lines, "5-9 0 12", said.append) == [lines[4], lines[5]]
    assert "5-9 is taken as 5-6" in said[0] and "Not on the list: 7-9" in said[0]
    assert "0 is not on the list" in said[1] and "12 is not on the list" in said[2]
    assert not any("left alone" in text for text in said)
    assert migrate.pick(lines, "banana", said.append) == []


def test_a_question_from_the_new_server_stops_only_that_item():
    site, mail = _site("a.nl"), _mail("b.nl")
    question = {"ok": False, "ask": {"question": "Overwrite?", "about": "files"}}
    connection = Connection([question, {"ok": True, "steps": []}])
    report = migrate.run(connection, [site, mail], lambda text: None)
    assert [item["name"] for item, _ in report.failed] == ["a.nl"]
    assert "no answer" in report.failed[0][1]
    assert [item["name"] for item, _ in report.moved] == ["b.nl"]
    # and an answer is sent back with the item
    connection = Connection([question, {"ok": True}])
    report = migrate.run(connection, [site], lambda text: "yes")
    assert connection.requests[1]["answers"] == {"files": "yes"}
    assert report.moved == [] and report.files_to_copy == [site]


def test_one_item_that_fails_does_not_stop_the_rest():
    items = [_mail("a.nl"), _site("b.nl"), _mail("c.nl")]
    moved = []
    connection = Connection([{"ok": False, "error": "mailctl broke"}, {"ok": True}, {"ok": True}])
    report = migrate.run(connection, items, lambda text: None, move=lambda item, answer: moved.append(item))
    assert report.failed == [(items[0], "mailctl broke")]
    assert [item["name"] for item, _ in report.moved] == ["b.nl", "c.nl"]
    assert moved == [items[1]] and report.files_to_copy == []


def test_a_lost_connection_ends_the_run():
    items = [_mail("a.nl"), _mail("b.nl"), _mail("c.nl")]
    connection = Connection([{"ok": True}, migrate.Stopped("I can't reach it."), {"ok": True}])
    report = migrate.run(connection, items, lambda text: None)
    assert len(report.moved) == 1 and report.stopped == "I can't reach it."
    assert report.not_tried == [items[2]]


def test_pick_says_something_for_empty_reversed_duplicate_and_odd_input():
    lines = [{"id": str(n)} for n in range(1, 6)]
    said = []
    assert migrate.pick(lines, "  ", said.append) == [] and "nothing" in said[-1]
    assert migrate.pick(lines, "3-1", said.append) == lines[:3] and "backwards" in said[-1]
    assert migrate.pick(lines, "2 2 2-3 3", said.append) == lines[1:3]
    assert migrate.pick(lines, "0-2", said.append) == lines[:2] and "Not on the list: 0" in said[-1]
    said[:] = []
    assert migrate.pick(lines, "\uff11", said.append) == [] and "understand" in said[0]


def test_the_numbers_on_the_dashboard_are_the_ones_pick_resolves():
    items = [_site("old.nl"), _mail("one.nl"), _site("bad.nl"), _mail("two.nl"), _site("good.nl")]
    status = _status(**{"site:old.nl": "exists", "mail:one.nl": "new", "site:bad.nl": "refused",
                        "mail:two.nl": "new", "site:good.nl": "new"})
    text = migrate.dashboard(items, status)
    numbered = [line.split() for line in text.splitlines() if line.startswith(" ") and line.split()[:1] in (["1"], ["2"], ["3"])]
    assert [(line[0], line[1]) for line in numbered] == [("1", "one.nl"), ("2", "two.nl"), ("3", "good.nl")]
    assert migrate.pick(migrate.offered(items, status), "2") == [items[3]]
    assert migrate.pick(migrate.offered(items, status), "3") == [items[4]]


def test_a_domain_of_253_characters_and_a_huge_size_fit_and_the_headers_are_whole():
    name = ("a" * 60 + ".") * 3 + "b" * 60 + ".nl"
    name = name[:253]
    items = [_site(name, 10 ** 15), _site("z" * 20 + "." + "y" * 40 + ".com")]
    text = migrate.dashboard(items, _status(**{"site:" + name: "new", items[1]["id"]: "exists"}))
    assert all(len(line) <= 79 for line in text.splitlines())
    header = [l for l in text.splitlines() if l.startswith("  No.")][0]
    assert header.split() == ["No.", "Domain", "Here", "On", "the", "new", "server", "To", "copy"]
    assert name[-10:] in text  # cut in the middle, so the end of the name still shows


def test_a_no_reply_is_skipped_and_the_rest_goes_on():
    site, mail = _site("a.nl"), _mail("b.nl")
    refused = {"ok": False, "skipped": True, "error": "Left a.nl alone, as you asked."}
    question = {"ok": False, "ask": {"question": "Overwrite?", "about": "files"}}
    connection = Connection([question, refused, {"ok": True}])
    report = migrate.run(connection, [site, mail], lambda text: "no")
    assert report.skipped == [(site, "Left a.nl alone, as you asked.")] and len(report.moved) == 1
    assert connection.requests[1]["answers"] == {"files": "no"}


def test_an_answer_that_is_neither_ok_nor_a_question_fails_that_item():
    connection = Connection([{"ok": False}, {"ok": False, "needs_password": ["a@b.nl"]}])
    report = migrate.run(connection, [_mail("a.nl"), _mail("b.nl")], lambda text: None)
    assert [message for _, message in report.failed][0] == "The new server gave no reason."
    assert len(report.failed) == 2 and report.moved == []


def test_a_site_whose_files_could_not_be_moved_is_recorded_as_made_with_files_outstanding():
    site, other = _site("a.nl"), _site("b.nl")

    def move(item, answer):
        if item is site:
            raise RuntimeError("the copy broke")

    report = migrate.run(Connection([{"ok": True}, {"ok": True}]), [site, other], lambda text: None, move=move)
    assert report.files_to_copy == [site]
    assert [item for item, _ in report.moved] == [other]
    assert "the copy broke" in report.failed[0][1] and "made" in report.failed[0][1]


def test_a_connection_lost_while_moving_files_still_records_the_site_as_made():
    sites = [_site("a.nl"), _site("b.nl")]

    def move(item, answer):
        raise migrate.Stopped("I can't reach it.")

    report = migrate.run(Connection([{"ok": True}]), sites, lambda text: None, move=move)
    assert report.files_to_copy == [sites[0]] and report.moved == []
    assert report.stopped == "I can't reach it." and report.not_tried == [sites[1]]
    assert "was made" in report.failed[0][1] and "I can't reach it." in report.failed[0][1]


def test_a_site_without_a_move_is_files_to_copy_and_not_moved():
    site = _site("a.nl")
    report = migrate.run(Connection([{"ok": True}]), [site], lambda text: None)
    assert report.files_to_copy == [site] and report.moved == []


def test_help_says_how_to_get_the_file_onto_the_old_server_and_what_it_needs(capsys):
    with pytest.raises(SystemExit) as stopped:
        migrate.main(["--help"])
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "scp migrate.py root@" in text and "as root" in text
    assert "/home/ansible/.ssh/authorized_keys" in text
    assert "rsync" in text and "Python 3.6" in text and "ssh-keygen" in text
    for written in ("migrate_key", "migrate_key.pub", "migrate_known_hosts", "/var/lib/migrate/state.json"):
        assert written in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_not_being_root_is_one_line_and_a_stop(capsys):
    assert migrate.main(["list", "--host", "new"], geteuid=lambda: 1000) == 1
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_a_stop_anywhere_prints_its_message_and_exits_nonzero(capsys, monkeypatch):
    def broken():
        raise migrate.Stopped("No key today.")

    monkeypatch.setattr(migrate, "own_key", broken)
    assert migrate.main(["list", "--host", "new"], geteuid=lambda: 0) == 1
    assert capsys.readouterr().out.strip() == "No key today."


def test_left_out_lines_of_a_domain_that_did_not_move_are_dropped_and_the_rest_kept():
    lines = ["The forward from @a.nl: a catch-all.", "The forward from info@b.nl to x: no.",
             "The domain Bad_Name.nl: it isn't a valid domain name.", "The spam setting x for $GLOBAL: bad value."]
    mail = migrate.Mail({"domains": ["a.nl", "b.nl"], "spam": []}, lines, [])
    assert migrate._left_out_of(mail, ["a.nl"]).left_out == [lines[0], lines[2], lines[3]]
    assert migrate._left_out_of(mail, []).left_out == lines[2:]


def test_server_wide_spam_settings_are_reported_not_moved():
    file = {"version": 1, "domains": ["a.nl"], "addresses": [], "forwards": [], "sieve": [],
            "spam": [{"target": "server", "setting": "required_score", "value": "4"}]}
    mail = migrate.Mail(file, [], [])
    assert all(item["data"]["spam"] == [] for item in migrate._domain_items(mail))
    said = migrate._left_out_of(mail, ["a.nl"]).left_out
    assert len(said) == 1 and "required_score" in said[0] and "4" in said[0] and "by hand" in said[0]


def test_each_mail_domain_gets_only_its_own_part_of_the_file():
    file = {"version": 1, "domains": ["a.nl", "b.nl"],
            "addresses": [{"address": "x@a.nl"}, {"address": "y@b.nl"}],
            "forwards": [{"source": "x@a.nl", "destination": "y@b.nl"}],
            "spam": [{"target": "server"}, {"target": "b.nl"}], "sieve": [{"address": "y@b.nl"}]}
    first, second = migrate._domain_items(migrate.Mail(file, [], []))
    assert first["data"]["addresses"] == [{"address": "x@a.nl"}] and len(first["data"]["forwards"]) == 1
    assert first["data"]["spam"] == [] and second["data"]["spam"] == [{"target": "b.nl"}]
    assert second["data"]["sieve"] == [{"address": "y@b.nl"}] and second["data"]["forwards"] == []


def test_the_mail_notes_reach_the_report_and_a_domain_filter_narrows_everything(monkeypatch):
    mail = migrate.Mail({"version": 1, "domains": ["a.nl"], "addresses": [], "forwards": [], "spam": [],
                         "sieve": []}, [], ["doveadm is missing."])
    sites = [{"name": "a.nl", "aliases": [], "docroot": "/w/a", "wordpress": True},
             {"name": "www.a.nl", "aliases": [], "docroot": "/w/wa", "wordpress": True},
             {"name": "b.nl", "aliases": [], "docroot": "/w/b", "wordpress": True}]
    jobs = [{"user": "u", "line": "* * * * * php /w/a/x.php", "site": "a.nl", "interpreter": "php"},
            {"user": "u", "line": "* * * * * php /w/b/x.php", "site": "b.nl", "interpreter": "php"}]
    monkeypatch.setattr(migrate, "mail_database", lambda run, notes: "mailserver")
    monkeypatch.setattr(migrate, "mail_inventory", lambda run, database, domain=None: mail)
    monkeypatch.setattr(migrate, "sites", lambda run, notes: sites)
    monkeypatch.setattr(migrate, "database_sizes", lambda run, notes: {"loose": 1})
    monkeypatch.setattr(migrate, "cron_jobs", lambda run, found, notes: jobs)
    monkeypatch.setattr(migrate, "site_items", lambda found, **rest: [{"id": "site:" + s["name"], "kind": "site",
                                                                       "name": s["name"]} for s in found])
    notes = []
    args = migrate._arguments(["list", "--domain", "a.nl"])
    _, _, found, sizes, items = migrate._look_around(args, notes)
    assert [i["id"] for i in items if i["kind"] == "site"] == ["site:a.nl", "site:www.a.nl"]
    assert [i["line"] for i in items if i["kind"] == "cron"] == [jobs[0]["line"]]
    assert sizes is None and "doveadm is missing." in notes


class _Args(object):
    domain, again = None, False


def _wired(monkeypatch, typed, status_state="new", state=None, items=None):
    mail = migrate.Mail({"version": 1, "domains": ["a.nl", "b.nl"], "addresses": [], "forwards": [], "spam": [],
                         "sieve": []},
                        ["The forward from @a.nl: a catch-all.", "The forward from @b.nl: a catch-all."], [])
    found = [{"name": "plain.nl", "docroot": "/w/p", "wordpress": False}]
    items = items if items is not None else [_mail("a.nl"), _mail("b.nl")]
    monkeypatch.setattr(migrate, "_look_around", lambda args, notes: (
        mail, "mailserver", found, {"loose_db": 5, "mailserver": 9}, items))
    monkeypatch.setattr(migrate, "_typed", lambda prompt: typed.pop(0))

    class Fake(Connection):
        host = "new.example"

        def ask(self, request):
            if request["request"] == "status":
                return {"ok": True, "items": _status(**{i["id"]: status_state for i in items})}
            if request["request"] == "dns":
                return {"ok": True, "text": "DNS-TABLE-FOR-" + request["domain"]}
            return {"ok": True}

    return Fake([]), state or migrate.State(path="/nonexistent-dir-for-test/state.json", say=lambda t: None)


def test_the_report_gets_everything_it_needs(monkeypatch, capsys):
    connection, state = _wired(monkeypatch, ["1", "yes"])
    state.unwritable = lambda: None
    state.save = lambda: None
    monkeypatch.setattr(migrate.socket, "gethostname", lambda: "old.example")
    migrate._work(_Args(), connection, state, ["a note from looking around"])
    text = capsys.readouterr().out
    assert "DNS-TABLE-FOR-a.nl" in text and "--host1 old.example" in text and "--host2 new.example" in text
    assert "You picked:" in text and "Go ahead?" not in text  # the question is the prompt, not output
    assert "The forward from @a.nl" in text and "The forward from @b.nl" not in text
    # The lists about the whole server are not part of the report of a move.
    report = text.split("Report\n======")[1]
    for left_out in ("plain.nl", "loose_db", "a note from looking around", "mailserver"):
        assert left_out not in report


def test_again_asks_before_replacing_and_no_leaves_the_site_alone(monkeypatch, capsys, tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / "wp-config.php").write_text("<?php\ndefine('DB_NAME', 'db1');\ndefine('DB_USER', 'u');\n"
                                        "define('DB_PASSWORD', 'p');\ndefine('DB_HOST', 'localhost');\n")
    site = dict(_site("a.nl"), docroot=str(root))
    state = migrate.State(path=str(tmp_path / "state.json"), say=lambda t: None)
    state.made(site, {"docroot": "/home/a/public_html/www", "database": "db1"})
    state.moved(site)
    connection, state = _wired(monkeypatch, ["1", "no"], status_state="exists", state=state, items=[site])
    args = _Args()
    args.again = True
    migrate._work(args, connection, state, [])
    text = " ".join(capsys.readouterr().out.split())
    assert "will be emptied and replaced" in text and "Left alone" in text
    assert not any(r["request"] != "status" for r in connection.requests)


def test_an_unexpected_error_is_one_plain_message_and_not_a_traceback(capsys, monkeypatch):
    monkeypatch.setattr(migrate, "own_key", lambda: (_ for _ in ()).throw(KeyError("oops")))
    assert migrate.main(["list", "--host", "new"], geteuid=lambda: 0) == 1
    text = capsys.readouterr().out
    assert "making the key" in text and len(text.splitlines()) <= 2
    assert "KeyError" in text and "Traceback" not in text and all(len(l) <= 80 for l in text.splitlines())


def test_ctrl_c_in_the_middle_of_a_batch_still_gives_a_report():
    def move(item, answer):
        raise KeyboardInterrupt()

    sites = [_site("a.nl"), _site("b.nl")]
    report = migrate.run(Connection([{"ok": True}]), sites, lambda text: None, move=move)
    assert report.stopped and report.not_tried == [sites[1]] and report.files_to_copy == [sites[0]]


def test_a_refused_login_prints_the_key_only_once(monkeypatch, capsys):
    class Refusing(object):
        host, user, key = "new", "ansible", "/x/migrate_key"

        def ask(self, request):
            raise migrate.Stopped("Not accepted.", refused=True)

    monkeypatch.setattr(migrate, "own_key", lambda *a: "ssh-ed25519 AAAA migrate")
    monkeypatch.setattr(migrate, "Connection", lambda host: Refusing())
    assert migrate.main(["list", "--host", "new"], geteuid=lambda: 0) == 1
    assert capsys.readouterr().out.count("ssh-ed25519 AAAA") == 1


def test_do_again_stops_at_ctrl_c_and_says_so():
    root_less = dict(_site("a.nl"), docroot="/nowhere")

    class State(object):
        def entry(self, item):
            return {"docroot": "/x"}

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    other = _site("b.nl")
    original = migrate.move_site
    migrate.move_site = interrupted
    try:
        report = migrate.do_again(Connection([]), [root_less, other], State(), lambda text: "yes")
    finally:
        migrate.move_site = original
    assert report.interrupted and report.not_tried == [other] and report.failed[0][0] is root_less


def test_an_interrupted_run_prints_its_report_and_exits_non_zero(monkeypatch, capsys):
    result = migrate.Report()
    result.interrupted = True
    monkeypatch.setattr(migrate, "own_key", lambda *a: "ssh-ed25519 AAAA migrate")
    monkeypatch.setattr(migrate, "Connection", lambda host: object())
    monkeypatch.setattr(migrate, "check", lambda connection, show_key=True: {})
    monkeypatch.setattr(migrate, "State", lambda: None)
    monkeypatch.setattr(migrate, "_work", lambda *a: result)
    assert migrate.main(["move", "--host", "new"], geteuid=lambda: 0) == 1
    assert "You stopped the run" in capsys.readouterr().out


def test_a_mail_domain_that_is_half_there_can_be_sent_again_with_the_counts_in_sight():
    """mailctl import only adds what is missing, so this is the only way a half-imported domain is finished."""
    mail, site = _mail("example.nl"), _site("example.nl")
    status = {"mail:example.nl": {"state": "exists",
                                  "detail": "example.nl is already here, with 50 accounts and 1 forwards."},
              "site:example.nl": {"state": "exists", "detail": "The site example.nl is already here."}}
    text = migrate.dashboard([mail, site], status)
    assert migrate.offered([mail, site], status) == [mail]
    assert "50 accounts, 1 forward - can be topped up" in text
    assert "adds the accounts" in " ".join(text.split())
    # The site is a different matter: sending it again would empty its database, so it is not offered.
    assert "The site example.nl is already here." in " ".join(text.split())