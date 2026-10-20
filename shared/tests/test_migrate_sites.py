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


# ---- Task 12: moving a site's files and database ----------------------------------------------------------

import json  # noqa: E402


class FakeRun(object):
    """Stands in for run_command: records every command, its input and its environment."""

    def __init__(self, dump="-- a dump\n", fail=None):
        self.calls, self.dump, self.fail = [], dump, fail

    def __call__(self, *args, stdin=None, env=None):
        self.calls.append({"args": list(args), "stdin": stdin, "env": env})
        if self.fail and args[0] == self.fail:
            raise migrate.CommandFailed("%s failed: it broke" % args[0])
        return self.dump if args[0] == "mysqldump" else ""


class NewServer(migrate.Connection):
    def __init__(self, tmp_path, replies=None):
        super().__init__("new.example.nl", key=str(tmp_path / "migrate_key"))
        self.requests, self.replies = [], replies or {}

    def ask(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.get(request["request"], {"ok": True, "steps": ["Updated the web server."]})
        return reply


WP = {"DB_NAME": "oldshop", "DB_USER": "oldshop", "DB_PASSWORD": "s3cret'pw", "DB_HOST": "localhost"}


def _shop():
    return {"id": "site:shop.example.nl", "kind": "site", "name": "shop.example.nl", "user": "shopexample",
            "docroot": "/var/www/shop", "database": "shopexample"}


def _state(tmp_path):
    state = migrate.State(path=str(tmp_path / "var" / "state.json"), say=lambda text: None)
    state.made(_shop(), {"docroot": "/home/shopexample/public_html/www", "database": "shopexample"})
    return state


def _move(tmp_path, run, **more):
    notes = more.pop("notes", None) or _state(tmp_path)
    connection = more.pop("connection", None) or NewServer(tmp_path)
    return migrate.move_site(connection, _shop(), notes=notes, run=run, settings_of=lambda path: dict(WP),
                             **more), connection


def test_the_files_go_with_rsync_as_root_on_the_other_side(tmp_path):
    run = FakeRun()
    _move(tmp_path, run)
    rsync = run.calls[0]["args"]
    assert rsync[:4] == ["rsync", "-az", "--omit-dir-times", "--no-owner"] and "--no-group" in rsync
    assert "--delete" not in rsync
    assert rsync[rsync.index("--rsync-path") + 1] == "sudo -n rsync"
    assert "-i " + str(tmp_path / "migrate_key") in rsync[rsync.index("-e") + 1]
    # Both ends have a slash: the files land IN the web root, not in a folder inside it.
    assert rsync[-2:] == ["/var/www/shop/", "ansible@new.example.nl:/home/shopexample/public_html/www/"]


def test_the_dump_is_piped_and_never_written_to_disk(tmp_path):
    run = FakeRun(dump="CREATE TABLE x;\n")
    _move(tmp_path, run)
    dump, adopt = run.calls[1], run.calls[2]
    assert dump["args"][0] == "mysqldump" and dump["args"][-1] == "oldshop"
    assert "s3cret'pw" not in " ".join(" ".join(call["args"]) for call in run.calls)
    assert dump["env"] == {"MYSQL_PWD": "s3cret'pw"}
    assert adopt["stdin"] == "CREATE TABLE x;\n" and adopt["env"] is None
    words = adopt["args"]
    assert words[0] == "ssh"
    assert words[-11:] == ["sudo", "-n", "domainctl", "wp", "adopt", "shopexample", "--database", "shopexample",
                           "--dump", "-", "--replace", "--yes"][-11:]
    assert "--replace" in words and "--yes" in words
    assert not list(tmp_path.glob("**/*.sql"))


def test_a_second_run_offers_a_site_whose_files_did_not_arrive(tmp_path):
    site = _shop()
    state = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    exists = {site["id"]: {"state": "exists", "detail": "already here"}}
    assert migrate.offered([site], exists, state) == []

    failing = FakeRun(fail="rsync")
    connection = NewServer(tmp_path, {"apply": {"ok": True, "docroot": "/home/shopexample/public_html/www"}})
    report = migrate.run(connection, [site], lambda text: None, notes=state,
                         move=migrate.mover(connection, state, None, run=failing, settings_of=lambda p: dict(WP)))
    assert report.files_to_copy == [site] and report.moved == []
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["items"][site["id"]]["status"] == "made" and "s3cret" not in json.dumps(saved)

    again = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)  # a new run
    assert migrate.offered([site], exists, again) == [site]
    assert "files still to come" in migrate.dashboard([site], exists, again)
    ok = FakeRun()
    connection.requests.clear()
    report = migrate.run(connection, [site], lambda text: None, notes=again,
                         move=migrate.mover(connection, again, None, run=ok, settings_of=lambda p: dict(WP)))
    assert [r["request"] for r in connection.requests] == ["finish_site"]  # not made a second time
    assert [i for i, _ in report.moved] == [site]
    assert json.loads((tmp_path / "state.json").read_text())["items"][site["id"]]["status"] == "moved"


def test_doing_it_again_replaces_the_database_only_when_told(tmp_path):
    asked = []
    run = FakeRun()
    result, _ = _move(tmp_path, run, again=True, ask=lambda text: asked.append(text) or "no")
    assert result["skipped"] and "Nothing was done" in result["message"] and run.calls == []
    assert "shopexample" in asked[0] and "shop.example.nl" in asked[0] and "emptied" in asked[0]
    result, _ = _move(tmp_path, run, again=True, ask=lambda text: "yes")
    assert result["ok"] and [c["args"][0] for c in run.calls][:2] == ["rsync", "mysqldump"]


def test_the_state_file_is_atomic_and_an_unwritable_one_is_said_once(tmp_path):
    said = []
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    state = migrate.State(path=str(blocked / "state.json"), say=said.append)
    state.made(_shop(), {"docroot": "/x"})
    state.moved(_shop())
    assert len(said) == 1 and state.entry(_shop())["status"] == "moved"
    good = migrate.State(path=str(tmp_path / "d" / "state.json"), say=said.append)
    good.moved(_shop())
    assert sorted(os.listdir(str(tmp_path / "d"))) == ["state.json"]


def test_item_ids_are_the_same_between_runs_and_the_user_comes_from_the_domain():
    found = [{"name": "Shop.Example.nl", "aliases": [], "docroot": "/nonexistent", "database": "d", "db_user": "u",
              "db_host": "localhost", "size": 5, "problem": None}]
    first, second = migrate.site_items(found, owner_of=lambda p: "www"), migrate.site_items(found)
    assert first[0]["id"] == second[0]["id"] == "site:shop.example.nl"
    assert first[0]["user"] == "shopexample" and first[0]["old_owner"] == "www"
    assert "password" not in json.dumps(first)
    job = {"user": "root", "line": "* * * * * /bin/x", "site": None, "interpreter": None, "settings": [],
           "problem": None}
    ids = [i["id"] for i in migrate.cron_items([job, job])]
    assert ids == [i["id"] for i in migrate.cron_items([job, job])] and len(set(ids)) == 2
    assert migrate.cron_items([dict(job, line="* * * * * /bin/y")])[0]["id"] not in ids
