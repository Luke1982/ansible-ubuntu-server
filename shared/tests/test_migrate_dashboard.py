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
    report = migrate.run(connection, items, lambda text: None, move=moved.append)
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

    def move(item):
        if item is site:
            raise RuntimeError("the copy broke")

    report = migrate.run(Connection([{"ok": True}, {"ok": True}]), [site, other], lambda text: None, move=move)
    assert report.files_to_copy == [site]
    assert [item for item, _ in report.moved] == [other]
    assert "the copy broke" in report.failed[0][1] and "made" in report.failed[0][1]


def test_a_connection_lost_while_moving_files_still_records_the_site_as_made():
    sites = [_site("a.nl"), _site("b.nl")]

    def move(item):
        raise migrate.Stopped("I can't reach it.")

    report = migrate.run(Connection([{"ok": True}]), sites, lambda text: None, move=move)
    assert report.files_to_copy == [sites[0]] and report.moved == []
    assert report.stopped == "I can't reach it." and report.not_tried == [sites[1]]
    assert "was made" in report.failed[0][1] and "I can't reach it." in report.failed[0][1]


def test_a_site_without_a_move_is_files_to_copy_and_not_moved():
    site = _site("a.nl")
    report = migrate.run(Connection([{"ok": True}]), [site], lambda text: None)
    assert report.files_to_copy == [site] and report.moved == []
