"""Sites that are neither WordPress, nor a redirect, nor a piSignage server: files and one database, copied,
with the settings left for a person because nothing here can read where that site keeps them."""

import os

import pytest

from migrate import migrate


def _site(**more):
    return dict({"name": "boersma-advocaten.nl", "docroot": "/var/www/boersma", "wordpress": False,
                 "problem": None, "aliases": [], "server": "apache", "size": None, "database": None,
                 "db_user": None, "db_host": None, "redirect": None, "signage": None}, **more)


def _item(**more):
    item, = migrate.site_items([_site(**more)])
    return item


def _row(item, status=None):
    return migrate.customers([item], status or {})[0]


class Server(object):
    """The new server: it makes the site, and makes the database when it is asked to."""

    def __init__(self, docroot="/home/boersmaadvocaten/public_html/www", refuse=None, question=None):
        self.requests, self.docroot, self.refuse, self.question = [], docroot, refuse, question
        self.user, self.host = "ansible", "web03.example.nl"

    def options(self):
        return ["-i", "key"]

    def ask(self, request, timeout=None):
        self.requests.append(dict(request))
        kind = request["request"]
        if kind == "apply":
            return {"ok": True, "docroot": self.docroot, "steps": []}
        if kind == "adopt_database":
            if self.question and not request.get("replace"):
                return {"ok": False, "ask": {"about": "database", "question": self.question}}
            if self.refuse:
                return {"ok": False, "error": self.refuse}
            return {"ok": True, "database": "boersma", "user": "boersma", "password": "s3cret-but-new",
                    "host": "localhost", "steps": ["Made the database boersma and read the dump in."]}
        return {"ok": True}

    def asked_for(self, kind):
        return [one for one in self.requests if one["request"] == kind]


@pytest.fixture
def here(tmp_path, monkeypatch):
    """A web root with the site's files in it."""
    docroot = tmp_path / "boersma"
    docroot.mkdir()
    (docroot / "index.php").write_text("<?php // the site")
    monkeypatch.setattr(migrate, "run_into", lambda path, *args: open(path, "wb").write(b"-- dump\n") and 0)
    return str(docroot)


def _copy(server, here, databases=None, typed="1", answer="yes", secret="", **more):
    """Types 'typed' at the database question and at the user name after it, and 'secret' at the password."""
    said, copied = [], []
    replies = [typed, ""]
    steps = migrate.copy_other_site(
        server, _item(docroot=here), databases if databases is not None else {"boersma": 4096},
        ask=lambda question: answer, choose=lambda prompt: replies.pop(0) if replies else "",
        secret=lambda prompt: secret, say=said.append,
        runner=lambda *args: copied.append(args) or 0, **more)
    return steps, " ".join(" ".join(said).split()), copied


def test_the_databases_of_this_server_are_offered_and_the_one_picked_is_dumped_into_the_site(here):
    server = Server()
    steps, said, copied = _copy(server, here, {"boersma": 4096, "wordpressdb": 99})

    assert "Which database does boersma-advocaten.nl use?" in said
    assert os.path.exists(os.path.join(here, "boersma.sql"))
    assert os.stat(os.path.join(here, "boersma.sql")).st_mode & 0o777 == 0o600
    assert "Dumped the database boersma" in " ".join(steps)
    # The dump goes over with the files, in one copy, and the new server reads it from where it lands.
    assert copied[0][0] == "rsync" and copied[0][-2] == here + "/"
    assert server.asked_for("adopt_database")[0]["dump"] == "boersma.sql"


def test_the_new_server_says_the_password_and_what_is_left_to_do(here):
    _steps, said, _copied = _copy(Server(), here)
    assert "password s3cret-but-new" in said and "database boersma" in said and "host localhost" in said
    assert "in the site's own settings" in said


def test_a_site_without_a_database_copies_only_its_files(here):
    server = Server()
    steps, said, copied = _copy(server, here, typed="")

    assert server.asked_for("adopt_database") == []
    assert not os.path.exists(os.path.join(here, "boersma.sql"))
    assert "No database picked" in said and copied and "Copied the files" in " ".join(steps)


def test_nothing_is_copied_without_a_yes(here):
    server = Server()
    steps, said, copied = _copy(server, here, answer="no")
    assert steps == [] and copied == [] and "was left alone" in said
    assert server.requests == []


def test_a_database_the_new_server_already_has_is_asked_about(here, monkeypatch):
    """The same question a WordPress site's database gets, with the same three answers."""
    monkeypatch.setattr(migrate, "_database_question", lambda text: "yes")
    server = Server(question="The database boersma is already on this server. Go on?")
    steps, _said, _copied = _copy(server, here)
    assert [one.get("replace") for one in server.asked_for("adopt_database")] == [None, True]
    assert "Made the database boersma" in " ".join(steps)


def test_a_site_whose_files_are_gone_is_not_copied(tmp_path):
    said = []
    steps = migrate.copy_other_site(Server(), _item(docroot=str(tmp_path / "nothing-here")), {},
                                    ask=lambda question: "yes", say=said.append)
    assert steps == [] and "no web root" in " ".join(" ".join(said).split())


def test_the_board_says_the_files_can_be_copied_and_which_key_does_it():
    """"by hand" said nobody here could move it. f moves everything of it but the one line a person writes."""
    item = _item()
    row = _row(item)
    assert row.word("site") == migrate.FILES == "copy it"
    assert migrate.next_step(row)[1] == "copy its files"
    keys = dict((key, what) for key, what, _note in migrate.customer_menu(row))
    assert "f" in keys and "2" not in keys
    assert "type f" in migrate.why_nothing(row, "2")
    assert migrate.customer_action(row, "f")[1] == "other"


def test_the_table_says_what_is_there_to_copy():
    screen = migrate.customer_screen(_row(_item()), {}, new_server="web03.majorlabel.nl")
    website = next(line for line in screen.splitlines() if line.strip().startswith("Website"))
    assert "files" in website and "not WordPress" not in website
    assert website.split()[-2:] == ["copy", "it"]


def test_once_it_is_across_it_is_done():
    item = _item()
    state = migrate.State(path="/nonexistent-dir-for-test/state.json", say=lambda text: None)
    state.save = lambda: None
    state.moved(item)
    row = migrate.customers([item], {item["id"]: {"state": "exists", "detail": ""}}, state)
    assert row[0].word("site") == migrate.DONE


def test_the_login_the_site_already_uses_is_made_on_the_new_server(here):
    """The site's own settings came over with its files. Make the same user and password there and it works
    without a line of it being changed."""
    server, said, _copied = Server(), [], []
    replies = ["1", "boersma_user"]
    migrate.copy_other_site(server, _item(docroot=here), {"boersma": 4096},
                            ask=lambda question: "yes",
                            choose=lambda prompt: replies.pop(0) if replies else "",
                            secret=lambda prompt: "the-one-in-its-config", say=said.append,
                            runner=lambda *args: 0)
    asked = server.asked_for("adopt_database")[0]
    assert asked["db_user"] == "boersma_user" and asked["password"] == "the-one-in-its-config"


def test_enter_at_the_password_leaves_the_new_server_to_make_one(here):
    server = Server()
    _steps, said, _copied = _copy(server, here)
    asked = server.asked_for("adopt_database")[0]
    assert "password" not in asked and asked["db_user"] == "boersma"
    assert "in the site's own settings" in said


def test_a_site_that_is_across_stops_being_called_not_wordpress():
    item = _item()
    state = migrate.State(path="/nonexistent-dir-for-test/state.json", say=lambda text: None)
    state.save = lambda: None
    state.moved(item)
    row = migrate.customers([item], {item["id"]: {"state": "exists", "detail": ""}}, state)[0]
    screen = " ".join(migrate.customer_screen(row, {}, state, new_server="web03.majorlabel.nl").split())
    assert "No wp-config.php" not in screen and "not WordPress" not in screen
    assert "on the new server" in screen and "f brings over what changed here since" in screen


def test_the_dns_of_a_site_that_is_across_is_moved_without_a_warning():
    item = _item()
    state = migrate.State(path="/nonexistent-dir-for-test/state.json", say=lambda text: None)
    state.save = lambda: None
    state.moved(item)
    there = {item["id"]: {"state": "exists", "detail": ""}}
    said = []
    migrate._point_dns(Server(), migrate.customers([item], there, state)[0],
                       ask=lambda question: "no", say=said.append)
    whole = " ".join(" ".join(said).split())
    assert "is not on the new server" not in whole and "Type f here first" not in whole


def test_the_dns_of_a_site_that_is_not_across_says_to_copy_it_first():
    said = []
    migrate._point_dns(Server(), _row(_item()), ask=lambda question: "no", say=said.append)
    assert "Type f here first" in " ".join(" ".join(said).split())
