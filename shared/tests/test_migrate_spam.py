"""Moving what SpamAssassin learned: the old server's dump goes in beside the new server's own Bayes, and is
added to it in one step once it is all there."""

import pytest

from migrate import migrate, migratectl


# ---- The new server's half ----------------------------------------------------

class MariaDB(object):
    """Stands in for the mariadb client on the new server: answers by a part of the SQL it is sent."""

    def __init__(self, incoming=migratectl.BAYES_TABLES, column="binary(5)", users="vmail\n"):
        self.sent = []
        self.answers = [  # the first part found answers, so the column query comes before the table lists
            ("column_type", column + "\n"),
            ("table_schema = 'bayes_incoming'", "\n".join(incoming) + "\n"),
            ("table_schema = 'bayes'", "\n".join(migratectl.BAYES_TABLES) + "\n"),
            ("DISTINCT username", users),
            ("SUM(spam_count)", "157776\t195939\n137777\n"),
        ]

    def __call__(self, command, what, stdin=None, env=None, timeout=None):
        assert command[0] == "mariadb"
        self.sent.append(stdin)
        return next((answer for part, answer in self.answers if part in stdin), "")

    def everything(self):
        return "".join(self.sent)


@pytest.fixture
def mariadb(monkeypatch):
    fake = MariaDB()
    monkeypatch.setattr(migratectl, "_run", fake)
    return fake


def test_the_new_server_says_how_much_it_has_learned(mariadb):
    assert migratectl.answer({"request": "bayes"}) == {"ok": True, "spam": 157776, "ham": 195939,
                                                       "tokens": 137777}


def test_a_server_without_a_bayes_database_says_so(monkeypatch):
    fake = MariaDB()
    fake.answers[2] = ("table_schema = 'bayes'", "")
    monkeypatch.setattr(migratectl, "_run", fake)
    answer = migratectl.answer({"request": "bayes"})
    assert answer["ok"] is False and "mail role" in answer["error"]


def test_the_dump_gets_an_empty_database_of_its_own(mariadb):
    assert migratectl.answer({"request": "bayes_prepare"}) == {"ok": True, "database": "bayes_incoming"}
    assert "DROP DATABASE IF EXISTS `bayes_incoming`;\nCREATE DATABASE `bayes_incoming`;" in mariadb.everything()


def test_the_learning_that_came_in_is_added_to_this_servers_own(mariadb):
    """Neither server's learning is thrown away: a token both know keeps both their counts, added up."""
    answer = migratectl.answer({"request": "bayes_swap"})

    assert answer["ok"] is True and answer["spam"] == 157776
    merge = next(sql for sql in mariadb.sent if "ON DUPLICATE KEY UPDATE" in sql)
    assert merge.count("START TRANSACTION;") == 1 and merge.rstrip().endswith("COMMIT;")
    assert "`bayes`.`bayes_token`.spam_count + VALUES(spam_count)" in merge
    assert "`bayes`.`bayes_token`.ham_count + VALUES(ham_count)" in merge
    assert "GREATEST(`bayes`.`bayes_token`.atime, VALUES(atime))" in merge
    assert "INSERT IGNORE INTO `bayes`.bayes_seen" in merge  # a message either of them learned stays learned
    assert "mine.spam_count + incoming.spam_count" in merge
    assert "RENAME TABLE" not in mariadb.everything()  # nothing of this server's own is moved aside any more
    assert "DROP DATABASE `bayes_incoming`;" in mariadb.everything()
    assert "DROP DATABASE `bayes`" not in mariadb.everything()


def test_what_came_in_without_every_table_leaves_the_own_learning_alone(monkeypatch):
    fake = MariaDB(incoming=("bayes_token", "bayes_vars"))
    monkeypatch.setattr(migratectl, "_run", fake)
    answer = migratectl.answer({"request": "bayes_swap"})
    assert answer["ok"] is False and "bayes_seen" in answer["error"] and "left as it was" in answer["error"]
    assert "ON DUPLICATE KEY UPDATE" not in fake.everything()


def test_tokens_stored_as_characters_are_stored_as_bytes_before_the_swap(monkeypatch):
    fake = MariaDB(column="char(5)")
    monkeypatch.setattr(migratectl, "_run", fake)
    assert migratectl.answer({"request": "bayes_swap"})["ok"] is True
    alter = next(i for i, sql in enumerate(fake.sent) if "ALTER TABLE `bayes_incoming`.bayes_token" in sql)
    assert alter < next(i for i, sql in enumerate(fake.sent) if "ON DUPLICATE KEY UPDATE" in sql)


def test_learning_kept_under_another_name_is_given_to_vmail(monkeypatch):
    fake = MariaDB(users="spamd\n")
    monkeypatch.setattr(migratectl, "_run", fake)
    assert migratectl.answer({"request": "bayes_swap"})["ok"] is True
    assert "UPDATE `bayes_incoming`.bayes_vars SET username = 'vmail';" in fake.everything()
    assert "WHERE username = 'vmail'" in fake.everything() and "'spamd'" not in fake.everything()


# ---- The old server's half ----------------------------------------------------

def _old(database="bayes", counts="157776\t195939\t137777\n"):
    def run(*args, stdin=None):
        sql = args[-1]
        if "information_schema" in sql:
            return database + "\n" if database else ""
        if "SUM(spam_count)" in sql:
            return counts
        raise AssertionError(sql)
    return run


class NewServer(migrate.Connection):
    def __init__(self, tmp_path, learned=0):
        super().__init__("web03.example.nl", key=str(tmp_path / "migrate_key"))
        self.requests = []
        self.replies = {"bayes": {"ok": True, "spam": learned, "ham": learned, "tokens": learned},
                        "bayes_prepare": {"ok": True, "database": "bayes_incoming"},
                        "bayes_swap": {"ok": True, "spam": 157776, "ham": 195939, "tokens": 137777}}

    def ask(self, request, timeout=None):
        self.requests.append(request["request"])
        return self.replies[request["request"]]


class Pipe(object):
    def __init__(self, codes=(0, 0)):
        self.codes, self.pipes = codes, []

    def __call__(self, first, second, env=None):
        self.pipes.append((first, second))
        return self.codes


def _no_question(text):
    raise AssertionError("asked: " + text)


def test_the_learning_is_dumped_into_the_new_servers_place_for_it(tmp_path):
    connection, pipe = NewServer(tmp_path), Pipe()
    result = migrate.move_spam_learning(connection, _no_question, run=_old(), piper=pipe)
    dump, target = pipe.pipes[0]
    assert dump == ["mysqldump", "--single-transaction", "--quick", "--hex-blob", "--add-drop-table", "--",
                    "bayes"]
    assert target[0] == "ssh" and target[-4:] == ["sudo", "-n", "mariadb", "bayes_incoming"]
    assert connection.requests == ["bayes", "bayes_prepare", "bayes_swap"]
    assert result["ok"] is True and "157,776 spam and 195,939 good messages" in result["steps"][0]


def test_learning_the_new_server_did_itself_is_only_replaced_after_a_yes(tmp_path):
    connection, pipe, asked = NewServer(tmp_path, learned=40), Pipe(), []
    result = migrate.move_spam_learning(connection, lambda text: asked.append(text) or "no", run=_old(),
                                        piper=pipe)
    assert result["skipped"] and "keeps its own" in result["message"]
    assert "40 spam and 40 good messages" in asked[0] and "157,776 spam" in asked[0]
    assert connection.requests == ["bayes"] and pipe.pipes == []


def test_a_connection_lost_halfway_never_swaps(tmp_path):
    connection = NewServer(tmp_path)
    with pytest.raises(migrate.MoveFailed) as failed:
        migrate.move_spam_learning(connection, _no_question, run=_old(), piper=Pipe((-13, 255)))
    assert "connection" in failed.value.message and "as it was" in failed.value.message
    assert "bayes_swap" not in connection.requests


def test_a_server_that_keeps_no_learning_in_mysql_says_so(tmp_path):
    with pytest.raises(migrate.MoveFailed) as failed:
        migrate.move_spam_learning(NewServer(tmp_path), _no_question, run=_old(database=None), piper=Pipe())
    assert "no spam learning" in failed.value.message


def test_nothing_learned_here_asks_nothing_of_the_new_server(tmp_path):
    connection = NewServer(tmp_path)
    result = migrate.move_spam_learning(connection, _no_question, run=_old(counts="0\t0\t0\n"), piper=Pipe())
    assert result["skipped"] and connection.requests == []


def test_spam_is_a_command_and_s_a_key(monkeypatch, capsys):
    assert migrate._arguments(["spam"]).command == "spam"
    moved = []
    monkeypatch.setattr(migrate, "_move_spam", lambda connection, doing=None: moved.append(connection) or 0)
    monkeypatch.setattr(migrate, "own_key", lambda *a: "ssh-ed25519 AAAA migrate")
    monkeypatch.setattr(migrate, "Connection", lambda host: "the new server")
    monkeypatch.setattr(migrate, "check", lambda connection, show_key=True: {"version": migrate.PROTOCOL_VERSION})
    monkeypatch.setattr(migrate, "State", lambda: type("State", (), {"server": "web03.example.nl"})())
    assert migrate.main(["spam"], geteuid=lambda: 0) == 0 and moved == ["the new server"]
    assert "s to move the spam learning" in migrate.LIVE_PROMPT
