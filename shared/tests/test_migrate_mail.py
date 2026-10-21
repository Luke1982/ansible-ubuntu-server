import json

import pytest

from migrate import migrate


class Server:
    """Stands in for the old server's commands: answers by the first part of the query it is asked."""

    def __init__(self, answers, missing=()):
        self.answers = answers
        self.missing = missing
        self.asked = []

    def __call__(self, *args, stdin=None):
        self.asked.append(args)
        if args[0] in self.missing:
            raise migrate.CommandFailed("%s isn't installed" % args[0], missing=True)
        text = " ".join(args)
        for part, answer in self.answers.items():
            if part in text:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise migrate.CommandFailed("no answer for " + text)


HASH = "{SHA512-CRYPT}$6$salt$hash"


def _server(**changes):
    answers = {
        "information_schema": "mailserver\t4\nother\t2\n",
        "virtual_domains": "example.nl\nother.nl\n",
        "virtual_users": "info@example.nl\t%s\nme@other.nl\t%s\n" % (HASH, HASH),
        "virtual_sender_aliases": "sales@example.nl\tinfo@example.nl, x@y.nl\n",
        "virtual_aliases": "sales@example.nl\tinfo@example.nl\nhi@other.nl\tme@other.nl\n",
        "SHOW DATABASES": "spamassassin\n",
        "userpref": "$GLOBAL\trequired_score\t5\n%example.nl\twelcomelist_from\t*@partner.nl\n"
                    "me@other.nl\tuse_bayes\t0\n",
        "doveadm sieve list -u info@example.nl": "roundcube ACTIVE\nold\n",
        "doveadm sieve list -u me@other.nl": "",
        "doveadm sieve get -u info@example.nl roundcube": "sieve script:\nrequire \"fileinto\";\n",
        "doveadm sieve get -u info@example.nl old": "keep;\n",
    }
    answers.update(changes)
    return Server(answers)


def test_the_mail_database_is_the_one_with_the_four_tables():
    assert migrate.mail_database(_server()) == "mailserver"


def test_a_server_without_those_tables_says_what_it_looked_for():
    notes = []
    assert migrate.mail_database(_server(information_schema=""), notes) is None
    assert "virtual_sender_aliases" in notes[0]


def test_a_server_without_a_mysql_client_has_no_mail_database_and_no_exception():
    notes = []
    assert migrate.mail_database(Server({}, missing=("mariadb", "mysql")), notes) is None
    assert "client" in notes[0]


def test_the_inventory_is_the_file_mailctl_reads():
    """Same keys, same shapes, so mailctl's own reader takes it."""
    inventory = migrate.mail_inventory(_server(), "mailserver", mail_root="/nonexistent")
    assert inventory.file == {
        "version": 1,
        "domains": ["example.nl", "other.nl"],
        "addresses": [{"address": "info@example.nl", "password_hash": HASH},
                      {"address": "me@other.nl", "password_hash": HASH}],
        "forwards": [{"source": "sales@example.nl", "destination": "info@example.nl", "send_as": True},
                     {"source": "hi@other.nl", "destination": "me@other.nl", "send_as": False}],
        "spam": [{"target": "server", "setting": "required_score", "value": "5"},
                 {"target": "example.nl", "setting": "welcomelist_from", "value": "*@partner.nl"},
                 {"target": "me@other.nl", "setting": "use_bayes", "value": "0"}],
        "sieve": [{"address": "info@example.nl", "name": "roundcube", "active": True,
                   "content": "require \"fileinto\";\n"},
                  {"address": "info@example.nl", "name": "old", "active": False, "content": "keep;\n"}],
    }
    json.dumps(inventory.file)


def test_one_domain_leaves_the_others_out():
    server = _server(**{
        "virtual_domains": "example.nl\n",
        "virtual_users": "info@example.nl\t%s\n" % HASH,
        "virtual_sender_aliases": "",
        "virtual_aliases": "sales@example.nl\tme@other.nl\n",
    })
    inventory = migrate.mail_inventory(server, "mailserver", domain="Example.nl", mail_root="/nonexistent")
    assert inventory.file["domains"] == ["example.nl"]
    assert [account["address"] for account in inventory.file["addresses"]] == ["info@example.nl"]
    assert inventory.file["forwards"][0]["destination"] == "me@other.nl"  # a forward belongs to its source's domain
    assert [value["target"] for value in inventory.file["spam"]] == ["example.nl"]  # not the server's settings
    assert {script["address"] for script in inventory.file["sieve"]} == {"info@example.nl"}
    queries = " ".join(" ".join(args) for args in server.asked)
    assert "= 'example.nl'" in queries


def test_a_catch_all_address_is_left_out_and_reported():
    server = _server(**{
        "virtual_aliases": "@example.nl\tinfo@example.nl\nsales@example.nl\tinfo@example.nl\n",
        "virtual_sender_aliases": "",
        "virtual_users": "info@example.nl\t%s\n@example.nl\t%s\n" % (HASH, HASH),
    })
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert [forward["source"] for forward in inventory.file["forwards"]] == ["sales@example.nl"]
    assert [account["address"] for account in inventory.file["addresses"]] == ["info@example.nl"]
    assert len(inventory.left_out) == 2
    assert all("catch-all" in reason for reason in inventory.left_out)


def test_values_with_escaped_tabs_newlines_and_backslashes_come_back_as_they_were():
    server = _server(userpref="$GLOBAL\tadd_header\ta\\tb\\nc\\\\d\n")
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert inventory.file["spam"][0]["value"] == "a\tb\nc\\d"


def test_a_server_without_doveadm_reads_the_filters_from_the_sieve_folder(tmp_path):
    home = tmp_path / "example.nl" / "info"
    (home / "sieve").mkdir(parents=True)
    (home / "sieve" / "roundcube.sieve").write_text("keep;\n")
    (home / ".dovecot.sieve").symlink_to(home / "sieve" / "roundcube.sieve")
    server = Server({}, missing=("doveadm",))
    assert migrate.filters_of(server, "info@example.nl", str(tmp_path)) == [
        {"name": "roundcube", "active": True, "content": "keep;\n"}]


def test_a_server_without_doveadm_or_a_sieve_folder_says_so_and_goes_on(tmp_path):
    notes = []
    assert migrate.filters_of(Server({}, missing=("doveadm",)), "info@example.nl", str(tmp_path), notes) == []
    assert "doveadm" in notes[0]


def test_a_server_without_the_spam_database_still_gives_its_mail():
    inventory = migrate.mail_inventory(_server(**{"SHOW DATABASES": ""}), "mailserver", mail_root="/nonexistent")
    assert inventory.file["spam"] == [] and "spamassassin" in inventory.notes[0]
    assert len(inventory.file["addresses"]) == 2


def test_a_second_active_filter_is_kept_but_switched_off():
    server = _server(**{"doveadm sieve list -u info@example.nl": "a ACTIVE\nb ACTIVE\n",
                        "doveadm sieve get -u info@example.nl": "keep;\n"})
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert [script["active"] for script in inventory.file["sieve"]] == [True, False]
    assert "one active filter" in inventory.left_out[0]


def test_what_is_returned_can_be_written_straight_to_a_file_mailctl_reads():
    inventory = migrate.mail_inventory(_server(**{"virtual_aliases": "@example.nl\tinfo@example.nl\n"}), "mailserver",
                                       mail_root="/nonexistent")
    assert inventory.left_out
    assert set(json.loads(json.dumps(inventory.file))) == {
        "version", "domains", "addresses", "forwards", "spam", "sieve"}


def test_a_spam_setting_mailctl_would_refuse_is_left_out_with_the_reason():
    server = _server(userpref="$GLOBAL\trequired_score\tabc\n$GLOBAL\twhitelist_from\tnonsense\n"
                              "$GLOBAL\tblocklist_from\t*@spam.nl\n$GLOBAL\trequired_score\t4.5\n")
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert [(v["setting"], v["value"]) for v in inventory.file["spam"]] == [
        ("blocklist_from", "*@spam.nl"), ("required_score", "4.5")]
    assert "'abc' isn't a number" in inventory.left_out[0]
    assert "nonsense" in inventory.left_out[1]


def test_an_unreadable_sieve_folder_is_a_note_not_an_exception(tmp_path, monkeypatch):
    (tmp_path / "example.nl" / "info" / "sieve").mkdir(parents=True)

    def refuse(path):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(migrate.os, "listdir", refuse)
    notes = []
    assert migrate.filters_of(Server({}, missing=("doveadm",)), "info@example.nl", str(tmp_path), notes) == []
    assert "Permission denied" in notes[0]


def test_filters_read_from_the_folder_are_reported_as_such_once(tmp_path):
    for user in ("info", "sales"):
        (tmp_path / "example.nl" / user / "sieve").mkdir(parents=True)
    notes, folders = [], []
    server = Server({}, missing=("doveadm",))
    for user in ("info", "sales"):
        migrate.filters_of(server, user + "@example.nl", str(tmp_path), notes, folders)
    assert folders == ["info@example.nl", "sales@example.nl"]
    assert sum("holds no scripts" in note for note in notes) == 2


def test_doveadm_failing_for_an_account_with_a_folder_is_said_in_the_inventory(tmp_path):
    home = tmp_path / "example.nl" / "info"
    (home / "sieve").mkdir(parents=True)
    (home / "sieve" / "a.sieve").write_text("keep;\n")
    (home / ".dovecot.sieve").write_text("keep;\n")  # a plain file, not a link into the folder
    server = _server(**{"doveadm sieve list -u info@example.nl": migrate.CommandFailed("doveadm failed: no user")})
    inventory = migrate.mail_inventory(server, "mailserver", mail_root=str(tmp_path))
    assert [script["name"] for script in inventory.file["sieve"]] == ["a"]
    assert any("read from" in note and "info@example.nl" in note for note in inventory.notes)
    assert any(".dovecot.sieve is not one of the scripts" in note for note in inventory.notes)


def test_what_the_new_server_says_is_on_screen_before_a_password_is_asked_for():
    """The ruling that keeps over-asking for passwords was priced on the person being told why."""
    note = "I couldn't read which accounts are already on this server, so every account was judged."
    item = {"id": "mail:a.nl", "kind": "mail_domain", "name": "a.nl",
            "data": {"addresses": [{"address": "x@a.nl", "password_hash": "{SSHA}x"}]}}
    replies = [{"ok": False, "needs_password": ["x@a.nl"], "note": note}, {"ok": True, "note": note}]
    screen = []

    class Connection(object):
        def ask(self, request):
            return replies.pop(0)

    answer, skipped = migrate.apply_item(
        Connection(), item, ask_password=lambda prompt: screen.append("asked: " + prompt) or "a password",
        say=screen.append)

    assert answer["ok"] is True and skipped == []
    assert "couldn't read which accounts" in screen[0]
    assert [line for line in screen if line.startswith("asked: ")]
    assert screen.index([line for line in screen if line.startswith("asked: ")][0]) > 0
    assert len([line for line in screen if "couldn't read" in line]) == 1  # said once, not after every reply


def test_a_note_from_a_successful_apply_reaches_the_report():
    item = {"id": "mail:a.nl", "kind": "mail_domain", "name": "a.nl", "data": {}}

    class Connection(object):
        def ask(self, request):
            return {"ok": True, "note": "The database's password is in the site's own wp-config.php."}

    extra = []
    report = migrate.run(Connection(), [item], lambda text: None, extra_notes=extra)
    assert len(report.moved) == 1 and extra == ["The database's password is in the site's own wp-config.php."]
    assert "wp-config.php" in migrate.report(report, extra_notes=extra)


def test_a_mail_domain_deleted_on_the_new_server_is_offered_again(tmp_path):
    item = {"id": "mail:a.nl", "kind": "mail_domain", "name": "a.nl", "data": {"addresses": [], "forwards": []}}
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    notes.items[item["id"]] = {"status": "moved"}
    gone = {item["id"]: {"state": "new", "detail": "a.nl is not on this server yet."}}

    assert migrate.offered([item], gone, notes) == [item]
    assert migrate.GONE_THERE in " ".join(migrate.dashboard([item], gone, notes).split())


# ---- Where the mail messages are, and copying them --------------------------

MAIL_PATH = "/var/vmail/%{user | domain}/%{user | username}/Maildir"


def _maildir(root, domain, local, inner="Maildir"):
    place = root / domain / local / inner if inner else root / domain / local
    (place / "cur").mkdir(parents=True)
    (place / "new").mkdir()
    return place


def test_the_layout_is_checked_per_account_and_never_assumed(tmp_path):
    """Maildir/cur, a bare cur, a folder that is no maildir at all, and nothing: four different answers."""
    root = tmp_path / "vmail"
    inner = _maildir(root, "a.nl", "info")
    home = _maildir(root, "a.nl", "sales", inner=None)
    (root / "a.nl" / "mdbox").mkdir()
    places = {"mail_path": MAIL_PATH}

    assert migrate.maildir_of("info@a.nl", places, str(root)) == (str(inner), None)
    assert migrate.maildir_of("sales@a.nl", places, str(root)) == (str(home), None)
    place, problem = migrate.maildir_of("mdbox@a.nl", places, str(root))
    assert place is None and "no 'cur' folder" in problem and "not something I copy" in problem
    place, problem = migrate.maildir_of("nobody@a.nl", places, str(root))
    assert place is None and "found no mail folder" in problem
    assert str(root / "a.nl" / "nobody" / "Maildir") in problem  # it says where it looked


def test_mail_kept_as_mdbox_or_mbox_is_said_and_not_copied(tmp_path):
    for driver, settings in (("mdbox", {"mail_location": "mdbox:/var/vmail/%d/%n/mdbox"}),
                             ("mbox", {"mail_driver": "mbox", "mail_path": MAIL_PATH})):
        place, problem = migrate.maildir_of("info@a.nl", settings, str(tmp_path))
        assert place is None and driver in problem and "not as a maildir" in problem
        assert "by hand" in problem


def test_dovecots_own_settings_say_where_to_look_first(tmp_path):
    """A server that keeps mail somewhere else entirely is still found, because doveconf is asked."""
    elsewhere = tmp_path / "srv" / "mail" / "a.nl" / "info"
    (elsewhere / "cur").mkdir(parents=True)
    server = Server({"doveconf": "mail_home = /srv/mail/%d/%n\nmail_location = maildir:~/\n"
                                 "service imap {\n  mail_path = /nowhere\n}\n"})
    places = migrate.mail_places(server)
    assert places["mail_home"] == "/srv/mail/%d/%n" and places["mail_location"] == "maildir:~/"
    found = migrate.maildir_of("info@a.nl", dict(places, mail_home=str(tmp_path) + "/srv/mail/%d/%n"),
                               str(tmp_path / "vmail"))
    assert found == (str(elsewhere), None)


def test_without_doveconf_it_says_so_and_looks_in_the_usual_place(tmp_path):
    root = tmp_path / "vmail"
    inner = _maildir(root, "a.nl", "info")
    notes = []
    places = migrate.mail_places(Server({}, missing=("doveconf",)), notes)
    assert places == {} and "Couldn't ask Dovecot" in notes[0] and "/var/vmail" in notes[0]
    assert migrate.maildir_of("info@a.nl", places, str(root)) == (str(inner), None)


def test_one_mailbox_item_per_account_with_its_size(tmp_path):
    root = tmp_path / "vmail"
    inner = _maildir(root, "a.nl", "info")
    server = Server({"du": "67500000000\t%s\n" % inner})
    mail = migrate.Mail({"addresses": [{"address": "info@a.nl"}, {"address": "nobody@a.nl"}]}, [], [])
    items = migrate.mailbox_items(mail, {"mail_path": MAIL_PATH}, mail_root=str(root),
                                  size_of=lambda place: migrate.directory_size(server, place))
    assert [item["id"] for item in items] == ["mailbox:info@a.nl", "mailbox:nobody@a.nl"]
    assert items[0]["kind"] == "mailbox" and items[0]["maildir"] == str(inner)
    assert items[0]["size"] == 67500000000 and items[0]["problem"] is None
    assert items[1]["maildir"] is None and items[1]["problem"] and items[1]["size"] is None
    assert ("du", "-s", "-b", str(inner)) in server.asked


def test_a_size_that_du_cannot_give_is_a_note_and_not_a_failure(tmp_path):
    notes = []
    assert migrate.directory_size(Server({}, missing=("du",)), "/var/vmail/a", notes) is None
    assert "Couldn't measure /var/vmail/a" in notes[0]


class NewServer(migrate.Connection):
    """The new server, with the answers it gives to each request."""

    def __init__(self, tmp_path, replies=None):
        super().__init__("web03.example.nl", key=str(tmp_path / "migrate_key"))
        self.requests, self.replies = [], replies or {}

    def ask(self, request, timeout=None):
        self.requests.append(request)
        return self.replies.get(request["request"], {"ok": True, "messages": 1234, "steps": ["Read it again."]})


HELLO = {"mail": {"path": MAIL_PATH, "driver": "maildir", "owner": "vmail", "group": "users",
                  "uid": 5000, "gid": 100, "mode": "0700"}}


class Rsync(object):
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, *args):
        self.calls.append(list(args))
        return self.code


def _box(**more):
    return dict({"id": "mailbox:info@a.nl", "kind": "mailbox", "name": "info@a.nl", "domain": "a.nl",
                 "maildir": "/var/vmail/a.nl/info/Maildir", "size": 67500000000, "problem": None}, **more)


def _copy(tmp_path, rsync=None, item=None, hello=HELLO, **more):
    rsync = rsync or Rsync()
    connection = more.pop("connection", None) or NewServer(tmp_path)
    notes = more.pop("notes", None) or migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    result = migrate.move_mailbox(connection, item or _box(), hello, notes=notes, runner=rsync, **more)
    return result, connection, rsync


def test_the_mail_goes_with_rsync_over_the_connection_it_already_has(tmp_path):
    result, connection, rsync = _copy(tmp_path)
    words = rsync.calls[0]
    assert words[:4] == ["rsync", "-a", "-s", "--info=progress2"]
    assert words[words.index("--rsync-path") + 1] == "sudo -n rsync"
    assert "-i " + str(tmp_path / "migrate_key") in words[words.index("-e") + 1]
    excluded = [words[place + 1] for place, word in enumerate(words) if word == "--exclude"]
    assert excluded == ["dovecot.index*", "dovecot.list.index*", "sieve", ".dovecot.sieve", ".dovecot.svbin"]
    # The message numbers stay as they are, so no mail client downloads everything a second time.
    assert "dovecot-uidlist" not in " ".join(words)
    assert "--delete" not in words  # nothing on the new server is ever removed without being asked
    # Slashes on both ends: the messages land IN the maildir the new server named, which came from hello.
    assert words[-2:] == ["/var/vmail/a.nl/info/Maildir/",
                          "ansible@web03.example.nl:/var/vmail/a.nl/info/Maildir/"]
    assert connection.requests == [{"request": "finish_mailbox", "address": "info@a.nl"}]
    assert result["ok"] is True and result["messages"] == 1234
    assert "Read it again." in result["steps"]


def test_the_place_it_goes_comes_from_the_new_server_and_nowhere_else(tmp_path):
    _, _, rsync = _copy(tmp_path, hello={"mail": {"path": "/srv/mail/%d/%n", "driver": "maildir"}})
    assert rsync.calls[0][-1] == "ansible@web03.example.nl:/srv/mail/a.nl/info/"
    for hello, words in ((None, "didn't say where it keeps mail"),
                         ({"mail": {"path": MAIL_PATH, "driver": "mdbox"}}, "keeps mail as mdbox")):
        with pytest.raises(migrate.MoveFailed) as stopped:
            _copy(tmp_path, hello=hello)
        assert words in stopped.value.message


def test_a_mailbox_whose_folder_was_not_found_is_never_copied(tmp_path):
    with pytest.raises(migrate.MoveFailed) as stopped:
        _copy(tmp_path, item=_box(maildir=None, problem="I found no mail folder for info@a.nl."))
    assert "found no mail folder" in stopped.value.message


def test_messages_that_vanish_while_copying_are_not_a_failure(tmp_path):
    result, _, _ = _copy(tmp_path, rsync=Rsync(24))
    assert result["ok"] is True and any("in use" in step for step in result["steps"])
    with pytest.raises(migrate.MoveFailed) as stopped:
        _copy(tmp_path, rsync=Rsync(23))
    assert "code 23" in stopped.value.message


def test_only_again_removes_what_the_new_server_has_and_this_one_has_not(tmp_path):
    asked = []
    result, _, rsync = _copy(tmp_path, again=True, ask=lambda question: asked.append(question) or "yes")
    assert "--delete" in rsync.calls[0] and result["ok"] is True
    assert "REMOVES" in asked[0] and "not on this server any more" in asked[0]
    no = Rsync()
    result, connection, _ = _copy(tmp_path, rsync=no, again=True, ask=lambda question: "no")
    assert result["skipped"] is True and no.calls == [] and connection.requests == []


def test_a_copied_mailbox_is_written_down_as_moved_and_finished_off(tmp_path):
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    _copy(tmp_path, notes=notes)
    assert notes.entry(_box()) == {"status": "moved"}
    broken = NewServer(tmp_path, {"finish_mailbox": {"ok": False, "error": "Dovecot said no."}})
    notes = migrate.State(path=str(tmp_path / "other.json"), say=lambda text: None)
    with pytest.raises(migrate.MoveFailed) as stopped:
        _copy(tmp_path, connection=broken, notes=notes)
    assert "is across" in stopped.value.message and "Dovecot said no." in stopped.value.message
    assert notes.entry(_box()) == {}  # not moved, so it is still offered


def test_a_mailbox_runs_through_run_and_do_again_with_the_copier(tmp_path):
    connection = NewServer(tmp_path)
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    rsync = Rsync()
    copy = migrate.copier(connection, notes, HELLO, lambda question: "yes", [], runner=rsync)
    report = migrate.run(connection, [_box()], lambda text: "yes", copy=copy, notes=notes)
    assert [item["name"] for item, _ in report.moved] == ["info@a.nl"] and report.failed == []
    assert report.moved[0][1]["messages"] == 1234 and "--delete" not in rsync.calls[0]
    again = migrate.do_again(connection, [_box()], notes, lambda question: "yes", [], copy=copy)
    assert [item["name"] for item, _ in again.moved] == ["info@a.nl"]
    assert "--delete" in rsync.calls[1]


def test_without_rsync_no_mail_is_copied_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: None)
    report = migrate.run(NewServer(tmp_path), [_box()], lambda text: "yes", copy=lambda item: 1 / 0)
    assert report.moved == [] and "rsync" in report.skipped[0][1]


def test_what_a_mailbox_copy_does_not_do_is_said_in_words_about_mail(tmp_path):
    extra = []
    _copy(tmp_path, extra_notes=extra)
    assert extra == [migrate.MAIL_NO_DELETE_NOTE] and "message that is gone here" in extra[0]
    _copy(tmp_path, extra_notes=extra)
    assert len(extra) == 1  # said once, not once per mailbox
