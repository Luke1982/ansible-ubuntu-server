import json

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
    assert migrate.file_part(inventory) == {
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
    json.dumps(migrate.file_part(inventory))


def test_one_domain_leaves_the_others_out():
    server = _server(**{
        "virtual_domains": "example.nl\n",
        "virtual_users": "info@example.nl\t%s\n" % HASH,
        "virtual_sender_aliases": "",
        "virtual_aliases": "sales@example.nl\tme@other.nl\n",
    })
    inventory = migrate.mail_inventory(server, "mailserver", domain="Example.nl", mail_root="/nonexistent")
    assert inventory["domains"] == ["example.nl"]
    assert [account["address"] for account in inventory["addresses"]] == ["info@example.nl"]
    assert inventory["forwards"][0]["destination"] == "me@other.nl"  # a forward belongs to its source's domain
    assert [value["target"] for value in inventory["spam"]] == ["example.nl"]  # not the server's settings
    assert {script["address"] for script in inventory["sieve"]} == {"info@example.nl"}
    queries = " ".join(" ".join(args) for args in server.asked)
    assert "= 'example.nl'" in queries


def test_a_catch_all_address_is_left_out_and_reported():
    server = _server(**{
        "virtual_aliases": "@example.nl\tinfo@example.nl\nsales@example.nl\tinfo@example.nl\n",
        "virtual_users": "info@example.nl\t%s\n@example.nl\t%s\n" % (HASH, HASH),
    })
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert [forward["source"] for forward in inventory["forwards"]] == ["sales@example.nl"]
    assert [account["address"] for account in inventory["addresses"]] == ["info@example.nl"]
    assert len(inventory["left_out"]) == 2
    assert all("catch-all" in reason for reason in inventory["left_out"])
    assert "left_out" not in migrate.file_part(inventory)


def test_values_with_escaped_tabs_newlines_and_backslashes_come_back_as_they_were():
    server = _server(userpref="$GLOBAL\tadd_header\ta\\tb\\nc\\\\d\n")
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert inventory["spam"][0]["value"] == "a\tb\nc\\d"


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
    assert inventory["spam"] == [] and "spamassassin" in inventory["notes"][0]
    assert len(inventory["addresses"]) == 2


def test_a_second_active_filter_is_kept_but_switched_off():
    server = _server(**{"doveadm sieve list -u info@example.nl": "a ACTIVE\nb ACTIVE\n",
                        "doveadm sieve get -u info@example.nl": "keep;\n"})
    inventory = migrate.mail_inventory(server, "mailserver", mail_root="/nonexistent")
    assert [script["active"] for script in inventory["sieve"]] == [True, False]
    assert "one active filter" in inventory["left_out"][0]
