import os

from migrate import migrate

VHOST = """<VirtualHost *:80>
    ServerName {name}
{extra}    DocumentRoot {root}
</VirtualHost>
"""

APACHE_S = """VirtualHost configuration:
*:80                   is a NameVirtualHost
         default server shop.example.nl ({conf}:{a})
         port 80 namevhost shop.example.nl ({conf}:{a})
                 alias www.shop.example.nl
                 alias winkel.example.nl
         port 80 namevhost plain.example.nl ({conf}:{b})
*:443                  is a NameVirtualHost
         default server shop.example.nl ({conf}:{c})
         port 443 namevhost shop.example.nl ({conf}:{c})
                 alias www.shop.example.nl
                 alias winkel.example.nl
ServerRoot: "/etc/apache2"
Main DocumentRoot: "/var/www/html"
"""


class Server:
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, *args, stdin=None):
        text = " ".join(args)
        for part, answer in self.answers.items():
            if part in text:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise migrate.CommandFailed("no answer for " + text)


def _write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w") as handle:
        handle.write(text)
    return str(path)


def _conf(tmp_path, shop_root, plain_root):
    conf = "".join([
        VHOST.format(name="shop.example.nl", root=shop_root,
                     extra="    ServerAlias www.shop.example.nl winkel.example.nl\n"),
        "\n\n\n\n",
        VHOST.format(name="plain.example.nl", root=plain_root, extra=""),
        "\n\n\n\n",
        VHOST.format(name="shop.example.nl", root=shop_root, extra="    # a comment\n"),
    ])
    starts = [number for number, line in enumerate(conf.split("\n"), 1) if line.startswith("<VirtualHost")]
    return _write(tmp_path / "sites-enabled" / "shop.conf", conf), starts


def _wp_config(root, name="shopdb", user="shopuser", password="s3cret", host="localhost", prefix="wp_"):
    _write(root / "wp-config.php", """<?php
define( 'DB_NAME', '%s' );
define('DB_USER', '%s');
define("DB_PASSWORD", "%s");
define('DB_HOST', '%s');
$table_prefix = '%s';
""" % (name, user, password, host, prefix))


def _server(made, **extra):
    conf, (a, b, c) = made
    answers = {"apache2ctl": APACHE_S.format(conf=conf, a=a, b=b, c=c),
               "information_schema": "shopdb\t1048576\nmysql\t4096\n"}
    answers.update(extra)
    return Server(answers)


def test_the_sites_come_from_apaches_own_view_of_itself(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert [site["name"] for site in found] == ["shop.example.nl", "plain.example.nl"]
    assert found[0]["aliases"] == ["www.shop.example.nl", "winkel.example.nl"]
    assert found[0]["docroot"] == str(shop)
    assert found[1]["aliases"] == []
    assert found[1]["docroot"] == str(plain)


def test_a_site_with_wp_config_is_wordpress_with_its_database(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    _wp_config(shop)
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    site = found[0]
    assert site["wordpress"] is True
    assert (site["database"], site["db_user"], site["db_password"], site["prefix"]) == (
        "shopdb", "shopuser", "s3cret", "wp_")
    assert site["size"] == 1048576
    assert site["problem"] is None


def test_wp_config_is_read_whatever_the_quoting(tmp_path):
    path = _write(tmp_path / "wp-config.php", r"""<?php
// define('DB_NAME', 'commented');
define(   'DB_NAME'  ,  "my db"  );
define('DB_USER', 'it\'s');
define( "DB_PASSWORD" , 'pa"ss\\word' ) ; // the password
define('DB_HOST', 'db.example.nl:3307');
$table_prefix  =  "wp7_";
""")
    assert migrate.wp_settings(path) == {"DB_NAME": "my db", "DB_USER": "it's", "DB_PASSWORD": 'pa"ss\\word',
                                         "DB_HOST": "db.example.nl:3307", "prefix": "wp7_"}
    sock = _write(tmp_path / "other" / "wp-config.php", "define('DB_HOST', 'localhost:/run/mysqld/mysqld.sock');\n")
    assert migrate.wp_settings(sock)["DB_HOST"] == "localhost:/run/mysqld/mysqld.sock"
    assert migrate.wp_settings(sock)["DB_NAME"] is None


def test_a_wp_config_it_cannot_read_marks_the_site_as_a_problem(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    _wp_config(shop)
    os.mkdir(str(plain))
    os.mkdir(str(plain / "wp-config.php"))  # exists, but can't be read as a file
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert found[1]["wordpress"] is True
    assert "can't be read" in found[1]["problem"]
    assert found[1]["database"] is None


def test_a_wp_config_with_empty_credentials_is_a_problem(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    _wp_config(shop, user="", password="")
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert found[0]["wordpress"] is True
    assert "DB_USER, DB_PASSWORD" in found[0]["problem"]


def test_a_site_without_wp_config_is_reported_and_not_wordpress(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    os.mkdir(str(plain))
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert found[1]["wordpress"] is False
    assert found[1]["problem"] is None and found[1]["database"] is None


def test_without_apache2ctl_the_conf_files_are_read(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    _wp_config(shop)
    conf = _conf(tmp_path, shop, plain)[0]
    server = Server({"apache2ctl": migrate.CommandFailed("apache2ctl isn't installed", missing=True),
                     "information_schema": "shopdb\t10\n"})
    notes = []
    found = migrate.sites(server, notes, sites_dir=os.path.dirname(conf))
    assert [site["name"] for site in found] == ["shop.example.nl", "plain.example.nl"]
    assert found[0]["aliases"] == ["www.shop.example.nl", "winkel.example.nl"]
    assert found[0]["database"] == "shopdb" and found[0]["size"] == 10
    assert any("apache2ctl" in note for note in notes)


def test_database_sizes_that_cannot_be_read_give_a_note_not_an_exception():
    notes = []
    server = Server({"information_schema": migrate.CommandFailed("mariadb failed: denied")})
    assert migrate.database_sizes(server, notes) == {}
    assert "denied" in notes[0]


def test_a_wp_config_above_the_web_directory_is_a_problem_not_plain_files(tmp_path):
    shop, plain = tmp_path / "site" / "public", tmp_path / "plain"
    os.makedirs(str(shop))
    _wp_config(tmp_path / "site")
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert found[0]["wordpress"] is True
    assert "outside its web directory" in found[0]["problem"]


def test_a_directory_with_wordpress_files_but_no_wp_config_is_a_problem(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    _write(shop / "wp-includes" / "version.php", "<?php\n")
    found = migrate.sites(_server(_conf(tmp_path, shop, plain)))
    assert found[0]["wordpress"] is True
    assert "no wp-config.php" in found[0]["problem"]


def test_a_port_with_one_virtual_host_is_not_dropped(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    conf, (a, b, c) = _conf(tmp_path, shop, plain)
    output = APACHE_S.format(conf=conf, a=a, b=b, c=c) + "*:8443                 lone.example.nl (%s:%d)\n" % (conf, b)
    found = migrate.sites(Server({"apache2ctl": output, "information_schema": ""}))
    assert [site["name"] for site in found] == ["shop.example.nl", "plain.example.nl", "lone.example.nl"]


# ---- Task 12: moving a site's files and database ---------------------------

import json  # noqa: E402
import pytest  # noqa: E402
import sys  # noqa: E402

ROOT = "/home/shopexample/public_html/www"


class Fakes(object):
    """Stand in for rsync and for 'mysqldump | ssh ... adopt'."""

    def __init__(self, rsync_code=0, dump_code=0, adopt_code=0):
        self.rsync, self.pipes = [], []
        self.codes = (rsync_code, dump_code, adopt_code)

    def runner(self, *args):
        self.rsync.append(list(args))
        return self.codes[0]

    def piper(self, first, second, env=None):
        self.pipes.append((list(first), list(second), env))
        return self.codes[1], self.codes[2]


class NewServer(migrate.Connection):
    def __init__(self, tmp_path, replies=None):
        super().__init__("new.example.nl", key=str(tmp_path / "migrate_key"))
        self.requests, self.replies = [], replies or {}

    def ask(self, request, timeout=None):
        self.requests.append(request)
        return self.replies.get(request["request"],
                                {"ok": True, "steps": ["Updated."]})


WP = {"DB_NAME": "oldshop", "DB_USER": "oldshop", "DB_PASSWORD": "s3cret'pw",
      "DB_HOST": "localhost"}
ANSWER = {"ok": True, "docroot": ROOT, "database": "shopexample"}


def _shop(**more):
    return dict({"id": "site:shop.example.nl", "kind": "site",
                 "name": "shop.example.nl", "user": "shopexample",
                 "docroot": "/var/www/shop", "database": "shopexample"},
                **more)


def _notes(tmp_path):
    return migrate.State(path=str(tmp_path / "var" / "state.json"),
                         say=lambda text: None)


def _move(tmp_path, fakes, notes=None, answer=ANSWER, site=None, **more):
    connection = more.pop("connection", None) or NewServer(tmp_path)
    result = migrate.move_site(
        connection, site or _shop(), answer, notes=notes or _notes(tmp_path),
        runner=fakes.runner, piper=fakes.piper,
        settings_of=lambda path: dict(WP), **more)
    return result, connection


def test_the_files_go_with_rsync_as_root_on_the_other_side(tmp_path):
    fakes = Fakes()
    _move(tmp_path, fakes)
    rsync = fakes.rsync[0]
    assert rsync[:2] == ["rsync", "-az"]
    for flag in ("-s", "--omit-dir-times", "--no-owner", "--no-group"):
        assert flag in rsync
    assert "--delete" not in rsync
    assert rsync[rsync.index("--rsync-path") + 1] == "sudo -n rsync"
    assert "-i " + str(tmp_path / "migrate_key") in rsync[rsync.index("-e") + 1]
    # Slashes on both ends: the files land IN the web root.
    assert rsync[-2:] == ["/var/www/shop/", "ansible@new.example.nl:" + ROOT + "/"]


def test_a_path_with_a_space_is_not_quoted_for_rsync(tmp_path):
    fakes = Fakes()
    _move(tmp_path, fakes, answer={"docroot": "/home/a b/www"},
          site=_shop(docroot="/var/www/my shop/"))
    assert fakes.rsync[0][-2:] == ["/var/www/my shop/",
                                   "ansible@new.example.nl:/home/a b/www/"]


def test_the_dump_goes_straight_into_the_adopt_with_the_password_in_the_environment(tmp_path):
    fakes = Fakes()
    _move(tmp_path, fakes)
    dump, adopt, env = fakes.pipes[0]
    assert dump[:3] == ["mysqldump", "--single-transaction", "--quick"]
    assert dump[-2:] == ["--", "oldshop"]
    assert env == {"MYSQL_PWD": "s3cret'pw"}
    everything = " ".join(dump + adopt + sum(fakes.rsync, []))
    assert "s3cret" not in everything
    assert adopt[0] == "ssh"
    assert adopt[-12:] == ["sudo", "-n", "domainctl", "wp", "adopt",
                           "shopexample", "--database", "shopexample",
                           "--dump", "-", "--replace", "--yes"]


def test_run_piped_streams_one_command_into_the_other(tmp_path):
    out = tmp_path / "out"
    codes = migrate.run_piped(
        [sys.executable, "-c", "print('x' * 100000, end='')"],
        [sys.executable, "-c",
         "import sys; open(%r, 'w').write(sys.stdin.read())" % str(out)])
    assert codes == (0, 0) and len(out.read_text()) == 100000
    assert migrate.run_piped([sys.executable, "-c", "import sys; sys.exit(3)"],
                             ["cat"], env={"A": "1"})[0] == 3


def test_run_command_adds_the_environment_to_its_own_for_that_command_only(monkeypatch):
    monkeypatch.delenv("MIGRATE_X", raising=False)
    out = migrate.run_command("sh", "-c", "echo $MIGRATE_X:$HOME",
                              env={"MIGRATE_X": "1"})
    assert out.startswith("1:") and out.strip() != "1:"  # HOME kept
    assert "MIGRATE_X" not in migrate.os.environ


def test_a_second_run_offers_a_site_whose_files_did_not_arrive(tmp_path):
    site = _shop()
    notes = migrate.State(path=str(tmp_path / "state.json"), say=print)
    exists = {site["id"]: {"state": "exists", "detail": "already here"}}
    assert migrate.offered([site], exists, notes) == []

    connection = NewServer(tmp_path, {"apply": ANSWER})
    broken = Fakes(rsync_code=23)
    report = migrate.run(
        connection, [site], lambda text: None, notes=notes,
        move=migrate.mover(connection, notes, None, runner=broken.runner,
                           piper=broken.piper, settings_of=lambda p: dict(WP)))
    assert report.files_to_copy == [site] and report.moved == []
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["items"][site["id"]]["status"] == "made"
    assert "s3cret" not in json.dumps(saved)

    again = migrate.State(path=str(tmp_path / "state.json"), say=print)
    assert migrate.offered([site], exists, again) == [site]
    assert "files still to come" in migrate.dashboard([site], exists, again)
    ok = Fakes()
    connection.requests.clear()
    report = migrate.run(
        connection, [site], lambda text: None, notes=again, status=exists,
        move=migrate.mover(connection, again, None, runner=ok.runner,
                           piper=ok.piper, settings_of=lambda p: dict(WP)))
    assert [r["request"] for r in connection.requests] == ["finish_site"]
    assert ok.rsync[0][-1] == "ansible@new.example.nl:" + ROOT + "/"
    assert [i for i, _ in report.moved] == [site]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["items"][site["id"]]["status"] == "moved"


def test_a_site_remembered_as_made_but_new_on_the_server_is_made_again(tmp_path):
    site = _shop()
    notes = _notes(tmp_path)
    notes.made(site, ANSWER)
    connection = NewServer(tmp_path, {"apply": ANSWER})
    seen = []
    new = {site["id"]: {"state": "new", "detail": ""}}
    migrate.run(connection, [site], lambda text: None, notes=notes,
                status=new, move=lambda item, answer: seen.append(answer))
    assert [r["request"] for r in connection.requests] == ["apply"]
    assert seen == [ANSWER]


def test_nothing_is_moved_when_the_notes_cannot_be_kept(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    notes = migrate.State(path=str(blocked / "state.json"), say=print)
    connection = NewServer(tmp_path)
    report = migrate.run(connection, [_shop()], lambda text: None, notes=notes)
    assert connection.requests == [] and report.stopped
    assert "Nothing was moved" in report.stopped
    assert report.not_tried == [_shop()]
    # Looking at the dashboard needs no notes at all.
    assert "shop.example.nl" in migrate.dashboard([_shop()], {}, notes)


def test_doing_it_again_replaces_the_database_only_when_told(tmp_path):
    asked = []
    fakes = Fakes()
    result, _ = _move(tmp_path, fakes, again=True,
                      ask=lambda text: asked.append(text) or "no")
    assert result["skipped"] and "Nothing was done" in result["message"]
    assert fakes.rsync == [] and fakes.pipes == []
    assert "shopexample" in asked[0] and "shop.example.nl" in asked[0]
    assert "emptied" in asked[0]
    _move(tmp_path, Fakes(), again=True)  # no way to ask: nothing happens
    result, _ = _move(tmp_path, fakes, again=True, ask=lambda text: "yes")
    assert result["ok"] and len(fakes.rsync) == 1 and len(fakes.pipes) == 1


def test_again_without_a_database_asks_nothing(tmp_path):
    fakes = Fakes()
    result, _ = _move(tmp_path, fakes, again=True, site=_shop(database=None))
    assert result["ok"] and fakes.pipes == []


@pytest.mark.parametrize("codes, word", [
    ((0, 2, 0), "mysqldump"), ((0, 0, 1), "read the database in")])
def test_a_failed_dump_or_adopt_leaves_the_site_outstanding_and_says_why(
        tmp_path, codes, word):
    notes = _notes(tmp_path)
    notes.moved(_shop())  # moved once before: now doing it again
    fakes = Fakes(*codes)
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(tmp_path, fakes, notes=notes, again=True, ask=lambda t: "yes")
    assert word in str(failed.value) and "OLD server's database" in str(failed.value)
    assert notes.entry(_shop())["status"] == "made"


def test_a_failed_finish_leaves_the_site_outstanding(tmp_path):
    notes = _notes(tmp_path)
    connection = NewServer(tmp_path, {"finish_site": {"ok": False, "error": "no"}})
    with pytest.raises(migrate.MoveFailed):
        _move(tmp_path, Fakes(), notes=notes, connection=connection)
    assert notes.entry(_shop())["status"] == "made"


def test_a_missing_destination_stops_before_anything_is_copied(tmp_path):
    fakes = Fakes()
    with pytest.raises(migrate.MoveFailed):
        _move(tmp_path, fakes, answer={"ok": True})
    assert fakes.rsync == []


def test_files_that_vanish_while_copying_are_not_a_failure(tmp_path):
    result, _ = _move(tmp_path, Fakes(rsync_code=24))
    assert result["ok"] and migrate.VANISHED_NOTE in result["steps"]
    with pytest.raises(migrate.MoveFailed):
        _move(tmp_path, Fakes(rsync_code=23))


def test_the_no_delete_note_is_given_once(tmp_path):
    extra = []
    _move(tmp_path, Fakes(), extra_notes=extra)
    _move(tmp_path, Fakes(), extra_notes=extra)
    assert extra == [migrate.NO_DELETE_NOTE]


def test_a_write_that_breaks_halfway_leaves_the_old_file_whole(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    notes = migrate.State(path=str(path), say=print)
    notes.moved(_shop())
    before = path.read_text()

    def broken(data, handle, **kw):
        handle.write("{ half")
        raise OSError("disk full")
    monkeypatch.setattr(migrate.json, "dump", broken)
    with pytest.raises(migrate.MoveFailed):
        notes.moved(_shop(id="site:b"))
    assert path.read_text() == before and os.listdir(str(tmp_path)) == ["state.json"]


@pytest.mark.parametrize("text", ["{ not json", "[1, 2]", '{"items": 5}'])
def test_notes_that_cannot_be_read_are_said_and_start_empty(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)
    said = []
    notes = migrate.State(path=str(path), say=said.append)
    assert notes.items == {} and len(said) == 1 and "never filled" in said[0]


def test_notes_of_an_older_form_keep_what_is_usable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"items": {"site:a": {"status": "made"}, "x": 5}, "old": 1}')
    notes = migrate.State(path=str(path), say=print)
    assert notes.items == {"site:a": {"status": "made"}}


def test_item_ids_are_stable_and_the_user_comes_from_the_domain():
    found = [{"name": "Shop.Example.nl", "aliases": [], "docroot": "/nowhere",
              "database": "d", "db_user": "u", "db_host": "localhost",
              "size": 5, "problem": None}]
    first = migrate.site_items(found, owner_of=lambda p: "www")
    second = migrate.site_items(found)
    assert first[0]["id"] == second[0]["id"] == "site:shop.example.nl"
    assert first[0]["user"] == "shopexample" and first[0]["old_owner"] == "www"
    assert "password" not in json.dumps(first)


def test_cron_ids_are_stable_and_a_jobs_user_is_its_sites_new_user():
    job = {"user": "wwwdata", "line": "* * * * * /bin/x", "site": "shop.example.nl",
           "interpreter": "php", "settings": [], "problem": None}
    first = migrate.cron_items([job, job])
    assert first[0]["user"] == "shopexample" and first[0]["old_user"] == "wwwdata"
    assert [i["id"] for i in first] == [i["id"] for i in migrate.cron_items([job, job])]
    assert len({i["id"] for i in first}) == 2
    other = migrate.cron_items([dict(job, line="* * * * * /bin/y")])[0]
    assert other["id"] not in {i["id"] for i in first}
    loose = migrate.cron_items([dict(job, site=None)])[0]
    assert loose["id"] == first[0]["id"] and "belongs to no site" in loose["problem"]
    assert migrate.offered([loose], {loose["id"]: {"state": "new"}}) == []
