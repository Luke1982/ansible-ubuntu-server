from migrate import migrate, migratectl

MAIL = {"id": "mail:example.nl", "kind": "mail_domain", "name": "example.nl",
        "data": {"addresses": [{"address": "ann@example.nl"},
                               {"address": "bob@example.nl"}]}}
SITE = {"id": "site:shop.nl", "kind": "site", "name": "shop.nl",
        "user": "shop", "docroot": "/var/www/shop"}
BROKEN = {"id": "site:broken.nl", "kind": "site", "name": "broken.nl",
          "user": "broken"}
JOB = {"id": "cron:1", "kind": "cron", "user": "root", "line": "* * * * * /bin/x",
       "problem": migrate.NO_SITE_JOB}


class Connection(object):
    host = "new.example.nl"

    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.asked = answer, fail, []

    def ask(self, request):
        self.asked.append(request)
        if self.fail:
            raise migrate.Stopped("gone")
        return self.answer or {"ok": True, "text": "TABLE for %s" % request["domain"]}


def _result():
    result = migrate.Report()
    result.moved = [(MAIL, {"ok": True}), (SITE, {"ok": True, "docroot": "/home/shop/public_html/www"})]
    return result


def test_an_imapsync_line_is_printed_for_every_account_that_moved():
    result = _result()
    result.left_out = ["bob@example.nl"]
    text = migrate.report(result, Connection(), old_server="old.example.nl")
    assert text.count("imapsync \\") == 1  # one command, not one mention of the word
    assert "--user1 ann@example.nl" in text and "bob@example.nl" not in text.split("imapsync")[1]
    assert "--host1 old.example.nl" in text and "--host2 new.example.nl" in text
    assert "--ssl1" in text and "--ssl2" in text and "TYPE-THE-PASSWORD-HERE" in text
    assert "TABLE for example.nl" in text
    assert "domainctl add shop.nl" in text
    assert "Linux user on the new server is shop" in text
    assert all(len(line) <= 79 for line in text.splitlines() if "TABLE" not in line), text


def test_what_was_left_out_is_named_with_the_reason():
    result = _result()
    result.files_to_copy = [BROKEN, {"id": "site:later.nl", "kind": "site", "name": "later.nl", "user": "later"}]
    result.failed = [(BROKEN, "The site was made, but its files were not brought over: disk full")]
    result.skipped = [(MAIL, "Left alone, as you asked.")]
    mail = migrate.Mail({}, [
        "The forward from @example.nl to ann@example.nl: it has no part before the @ (a catch-all address), "
        "which mailctl can't set up.",
        "ann@example.nl may send as info@example.nl, and no forward goes with it"], [])
    text = migrate.report(result, Connection(fail=True), mail=mail, items=[JOB],
                          found_sites=[{"name": "plain.nl", "docroot": "/var/www/plain", "wordpress": False},
                                       {"name": "shop.nl", "wordpress": True, "database": "shopdb"}],
                          databases={"mysql": 1, "shopdb": 2, "stray": 3, "mailserver": 4},
                          mail_database="mailserver", extra_notes=[migrate.NO_DELETE_NOTE])
    assert text.count("broken.nl") == 1 and "disk full" in " ".join(text.split())
    assert "later.nl" in text
    assert "catch-all" in text and "may send as info@example.nl" in text
    assert "plain.nl" in text and "stray" in text and "shopdb" not in text and "mailserver" not in text
    assert "* * * * * /bin/x" in text and text.count("BOTH servers") == 0
    assert "mailctl dns show example.nl" in text
    assert migrate.NO_DELETE_NOTE.split(".")[0] in " ".join(text.split())


def test_cron_is_said_once_when_a_job_moved():
    result = _result()
    result.moved.append(({"id": "cron:2", "kind": "cron", "user": "shop", "line": "x"}, {"ok": True}))
    assert migrate.report(result, Connection()).count("BOTH servers") == 1


def test_a_run_that_moved_nothing_says_so_and_prints_no_lines():
    text = migrate.report(migrate.Report(), Connection())
    assert text == "Nothing was moved.\n"


def test_a_run_where_everything_was_left_alone_still_gives_each_reason():
    result = migrate.Report()
    result.skipped = [(SITE, "Already on the new server.")]
    result.failed = [(MAIL, "refused")]
    text = " ".join(migrate.report(result).split())
    assert "Website shop.nl: Already on the new server." in text
    assert "Mail example.nl: refused" in text


def test_a_run_that_stopped_says_why_and_what_was_not_tried():
    result = migrate.Report()
    result.stopped = "The connection was lost."
    result.not_tried = [SITE]
    text = migrate.report(result)
    assert "The connection was lost." in text and "Website shop.nl" in text


def test_a_dns_failure_gives_its_reason_and_records_are_said_to_be_whole():
    text = " ".join(migrate.report(_result(), Connection(answer={"ok": False, "error": "no mailctl"})).split())
    assert "mailctl dns show example.nl" in text and "Why not: no mailctl" in text
    assert "printed whole on purpose" in " ".join(migrate.report(_result(), Connection()).split())


def test_a_send_as_permission_without_a_forward_is_found():
    left_out = []
    migrate._forwards([["a@x.nl", "b@x.nl"]], [["c@x.nl", "d@x.nl"], ["a@x.nl", "b@x.nl"]], left_out)
    assert left_out == ["d@x.nl may send as c@x.nl, and no forward goes with it"]
    left_out = []
    migrate._forwards([], [["info@x.nl", "info@x.nl"]], left_out)
    assert left_out == []


def test_migratectl_gives_the_dns_text_as_it_comes(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return type("Done", (), {"returncode": 0, "stdout": "a  b\n", "stderr": ""})()
    monkeypatch.setattr(migratectl.subprocess, "run", run)
    answer = migratectl.answer({"request": "dns", "domain": "Example.nl"})
    assert answer == {"ok": True, "text": "a  b\n"}
    assert seen["command"] == ["mailctl", "dns", "show", "example.nl"] and seen["env"]["COLUMNS"] == "80"
    assert migratectl.answer({"request": "dns", "domain": "-x; rm"})["ok"] is False


def test_the_report_says_where_a_moved_sites_dns_must_point():
    result = _result()
    result.moved = [(dict(SITE, aliases=["www.shop.nl"]), {"ok": True, "docroot": "/home/shop/public_html/www"})]
    text = migrate.report(result, Connection(), addresses=["81.4.127.10", "2a02:2770::21a:4aff:fe8c:1"])
    joined = " ".join(text.split())
    assert "A record for 81.4.127.10" in joined and "AAAA record for 2a02:2770::21a:4aff:fe8c:1" in joined
    assert "shop.nl, www.shop.nl" in joined
    assert all(len(line) <= 79 for line in text.splitlines())


def test_without_an_address_the_report_says_to_look_it_up_on_the_new_server():
    result = _result()
    result.moved = [(SITE, {"ok": True})]
    joined = " ".join(migrate.report(result, Connection(), addresses=[]).split())
    assert "didn't say which addresses it has" in joined and "ip addr" in joined


def test_a_run_without_a_site_says_nothing_about_a_records():
    result = migrate.Report()
    result.moved = [(MAIL, {"ok": True})]
    assert "A record" not in migrate.report(result, Connection(), addresses=["81.4.127.10"])


def test_migratectl_says_which_addresses_it_has(monkeypatch):
    def run(command, **kwargs):
        assert command[:3] == ["ip", "-json", "route"]
        address = "81.4.127.10" if command[-1].count(".") == 3 else "2a02:2770::1"
        return type("Done", (), {"returncode": 0, "stdout": '[{"prefsrc": "%s"}]' % address, "stderr": ""})()
    monkeypatch.setattr(migratectl.subprocess, "run", run)
    assert migratectl.answer({"request": "hello"})["addresses"] == ["81.4.127.10", "2a02:2770::1"]


def test_migratectl_leaves_an_address_out_rather_than_guessing(monkeypatch):
    def broken(command, **kwargs):
        raise OSError("no ip command here")
    monkeypatch.setattr(migratectl.subprocess, "run", broken)
    assert migratectl.answer({"request": "hello"})["addresses"] == []
