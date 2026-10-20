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
    assert "5-9" in said[0] and "12" in said[0] and "0" in said[0]
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
    assert report.moved and report.files_to_copy == [site]


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
