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


class Connection(object):
    def __init__(self, replies):
        self.replies, self.requests = replies, []

    def ask(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_every_item_is_on_one_line_within_eighty_columns():
    items = [_site("a-very-long-name-for-a-site.example.com", 3 * 10 ** 9), _mail("example.nl"),
             {"id": "cron:1", "kind": "cron", "user": "root", "line": "* * * * * " + "x" * 200}]
    status = _status(**{"site:a-very-long-name-for-a-site.example.com": "refused", "mail:example.nl": "new",
                        "cron:1": "new"})
    status["site:a-very-long-name-for-a-site.example.com"]["detail"] = "A very long reason. " * 20
    text = migrate.dashboard(items, status)
    assert all(len(line) <= 80 for line in text.splitlines())
    table = text.split("\n\n")[0].splitlines()[1:]
    assert len(table) == 3
    assert "1 mailbox" in text and "Why some can't be moved" in text


def test_what_the_new_server_already_has_is_shown_but_not_offered():
    items = [_site("old.nl"), _site("new.nl", 5 * 10 ** 6), _site("bad.nl")]
    status = _status(**{"site:old.nl": "exists", "site:new.nl": "new", "site:bad.nl": "refused"})
    text = migrate.dashboard(items, status)
    lines = text.splitlines()
    assert lines[1].split()[0] == "Website" and "already on the new server" in lines[1]
    assert lines[2].lstrip().startswith("1 ") and "5.0 MB" in lines[2]
    assert lines[3].split()[0] == "Website" and "can't be moved" in lines[3]
    assert migrate.offered(items, status) == [items[1]]


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
    items = [_site("old.nl"), _mail("one.nl"), _site("bad.nl"), _mail("two.nl")]
    status = _status(**{"site:old.nl": "exists", "mail:one.nl": "new", "site:bad.nl": "refused",
                        "mail:two.nl": "new"})
    text = migrate.dashboard(items, status)
    numbered = [line.split() for line in text.splitlines() if line.split()[:1] in (["1"], ["2"])]
    assert [line[2] for line in numbered] == ["one.nl", "two.nl"]
    assert migrate.pick(migrate.offered(items, status), "2") == [items[3]]


def test_a_domain_of_253_characters_and_a_huge_size_fit_and_the_header_is_whole():
    name = ("a" * 60 + ".") * 3 + "b" * 60 + ".nl"
    name = name[:253]
    items = [_site(name, 10 ** 15), _site("z" * 20 + "." + "y" * 40 + ".com")]
    text = migrate.dashboard(items, _status(**{"site:" + name: "new", items[1]["id"]: "exists"}))
    assert all(len(line) <= 79 for line in text.splitlines())
    assert text.splitlines()[0].endswith("What it takes")
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
    assert migrate.main(["--host", "new"], geteuid=lambda: 1000) == 1
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_a_stop_anywhere_prints_its_message_and_exits_nonzero(capsys, monkeypatch):
    def broken():
        raise migrate.Stopped("No key today.")

    monkeypatch.setattr(migrate, "own_key", broken)
    assert migrate.main(["--host", "new"], geteuid=lambda: 0) == 1
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
    args = migrate._arguments(["--domain", "a.nl"])
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
    connection, state = _wired(monkeypatch, ["1"])
    state.unwritable = lambda: None
    state.save = lambda: None
    monkeypatch.setattr(migrate.socket, "gethostname", lambda: "old.example")
    migrate._work(_Args(), connection, state, ["a note from looking around"])
    text = capsys.readouterr().out
    assert "DNS-TABLE-FOR-a.nl" in text and "--host1 old.example" in text and "--host2 new.example" in text
    assert "mailserver" not in text  # the mail database is not a loose one
    assert "Still to do by hand" in text and "a note from looking around" in text
    assert "The forward from @a.nl" in text and "The forward from @b.nl" not in text
    assert "plain.nl" in text and "loose_db" in text


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
    assert migrate.main(["--host", "new"], geteuid=lambda: 0) == 1
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
    assert migrate.main(["--host", "new"], geteuid=lambda: 0) == 1
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
    monkeypatch.setattr(migrate, "own_key", lambda: "ssh-ed25519 AAAA migrate")
    monkeypatch.setattr(migrate, "Connection", lambda host: object())
    monkeypatch.setattr(migrate, "check", lambda connection, show_key=True: {})
    monkeypatch.setattr(migrate, "State", lambda: None)
    monkeypatch.setattr(migrate, "_work", lambda *a: result)
    assert migrate.main(["--host", "new"], geteuid=lambda: 0) == 1
    assert "You stopped the run" in capsys.readouterr().out
