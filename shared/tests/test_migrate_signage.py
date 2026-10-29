"""Moving piSignage servers: found by the program that listens behind Apache's ProxyPass, and brought across with
domainctl signage on the new server."""

import json
import os

import pytest

from migrate import migrate

APACHE_SITE = """<VirtualHost *:443>
    ServerName andre.milocast.nl
    ProxyPass / http://127.0.0.1:3001/
    ProxyPassReverse / http://127.0.0.1:3001/
    RewriteEngine On
    RewriteCond %{HTTP:Upgrade} websocket [NC]
    RewriteRule /(.*) ws://127.0.0.1:3001/$1 [P,L]
</VirtualHost>
<VirtualHost *:443>
    ServerName www.example.nl
    DocumentRoot /var/www/example
</VirtualHost>
"""
SS = """LISTEN 0      511          0.0.0.0:80        0.0.0.0:*    users:(("apache2",pid=900,fd=4))
LISTEN 0      511                *:3001            *:*    users:(("node",pid=1234,fd=20))
LISTEN 0      511                *:3002            *:*    users:(("node",pid=1240,fd=20))
LISTEN 0      4096       127.0.0.1:27017     0.0.0.0:*    users:(("mongod",pid=800,fd=11))
"""


def _pisignage(directory, settings="uri: process.env.MONGOLAB_URI || 'mongodb://127.0.0.1:27017/pisignage-dev'"):
    (directory / "config" / "env").mkdir(parents=True)
    (directory / "config" / "env" / "development.js").write_text(settings)
    (directory / "package.json").write_text(json.dumps({"name": "pisignage-server"}))
    (directory / "data").mkdir()
    return directory


def _process(proc, pid, cwd, environment=b""):
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cwd").symlink_to(cwd)
    (proc / str(pid) / "environ").write_bytes(environment)


@pytest.fixture
def machine(tmp_path):
    """An old server with Apache in front of one piSignage server, a second one with no name, and other programs."""
    sites = tmp_path / "sites-enabled"
    sites.mkdir()
    (sites / "andre.conf").write_text(APACHE_SITE)
    proc = tmp_path / "proc"
    andre = _pisignage(tmp_path / "andre" / "pisignage-server")
    (tmp_path / "andre" / "media").mkdir()
    _process(proc, 1234, andre)
    _process(proc, 1240, _pisignage(tmp_path / "loose" / "pisignage-server"))
    _process(proc, 900, tmp_path)
    _process(proc, 800, tmp_path)
    return {"sites": str(sites), "proc": str(proc), "andre": andre}


def _found(machine, notes=None):
    return migrate.signage_servers(lambda *args: SS, sites_dir=machine["sites"], proc=machine["proc"], notes=notes)


def test_a_piSignage_server_is_found_by_the_name_apache_serves_it_under(machine):
    notes = []
    found = _found(machine, notes)
    assert found == [{"name": "andre.milocast.nl", "port": 3001, "code": str(machine["andre"]),
                      "media": str(machine["andre"].parent / "media"), "data": str(machine["andre"] / "data"),
                      "uri": "mongodb://127.0.0.1:27017/pisignage-dev", "database": "pisignage-dev"}]
    assert len(notes) == 1 and "port 3002" in notes[0] and "isn't behind any site" in notes[0]


def test_the_database_in_its_environment_comes_before_its_settings(machine, tmp_path):
    proc = tmp_path / "proc" / "1234" / "environ"
    proc.write_bytes(b"NODE_ENV=production\0MONGOLAB_URI=mongodb://127.0.0.1/andre-signage?retryWrites=true\0")
    assert _found(machine)[0]["database"] == "andre-signage"


def test_a_websocket_rewrite_counts_as_a_proxy_too(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / "bert.conf").write_text("<VirtualHost *:443>\n ServerName bert.milocast.nl\n"
                                     " RewriteRule /(.*) ws://localhost:3005/$1 [P,L]\n</VirtualHost>\n")
    assert migrate._proxied_ports(str(sites)) == {3005: "bert.milocast.nl"}


class Connection(object):
    user, host = "ansible", "web03.example.nl"

    def options(self):
        return ["-i", "key"]

    def command(self, *words):
        return ["ssh", "new", "sudo", "-n"] + list(words)


SERVER = {"name": "andre.milocast.nl", "port": 3001, "code": "/srv/andre/pisignage-server", "media": "MEDIA",
          "data": "DATA", "uri": "mongodb://127.0.0.1:27017/pisignage-dev", "database": "pisignage-dev"}


def _move(monkeypatch, answer="no", codes=(0, 0), visible=0):
    ran, piped = [], []
    monkeypatch.setattr(migrate.os.path, "isdir", lambda path: path in ("MEDIA", "DATA"))
    run = lambda *args: "andremilocast:x:1001:1001::/home/andremilocast:/bin/bash\n"
    steps = migrate.move_signage(Connection(), SERVER, lambda question: answer, run=run,
                                 runner=lambda *args: ran.append(args) or visible,
                                 piper=lambda first, second: piped.append((first, second)) or codes)
    return steps, ran, piped


def test_a_move_sets_it_up_there_copies_its_files_and_reads_its_database_in(monkeypatch):
    steps, ran, piped = _move(monkeypatch)

    assert ran[0][-8:] == ("domainctl", "signage", "add", "andre.milocast.nl", "--user", "andremilocast", "--no-dns",
                           "--yes")
    assert ran[1][0] == "rsync" and ran[1][-2:] == ("MEDIA/", "ansible@web03.example.nl:/home/andremilocast/media/")
    assert ran[2][-2:] == ("DATA/", "ansible@web03.example.nl:/home/andremilocast/pisignage-server/data/")
    (dump, restore), = piped
    assert dump[0] == "mongodump" and "--archive" in dump and "--gzip" in dump
    assert restore[-8:] == ["signage", "restore", "andremilocast", "--from-database", "pisignage-dev", "--dump",
                            "-", "--yes"]
    assert len(ran) == 3  # no DNS move without a yes
    assert "Its DNS still points here" in steps[-1]


def test_a_yes_moves_its_dns_and_gets_its_certificate(monkeypatch):
    steps, ran, _ = _move(monkeypatch, answer="yes")
    assert ran[-1][-6:] == ("signage", "add", "andre.milocast.nl", "--user", "andremilocast", "--yes")
    assert "served there over HTTPS" in steps[-1]


def test_a_lost_connection_while_the_database_is_sent_is_said(monkeypatch):
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(monkeypatch, codes=(-13, 255))
    assert "connection to the new server was lost" in failed.value.message


def test_nothing_is_copied_when_the_new_server_did_not_set_it_up(monkeypatch):
    with pytest.raises(migrate.MoveFailed) as failed:
        _move(monkeypatch, visible=1)
    assert "didn't set up the piSignage server" in failed.value.message


def test_a_server_whose_database_is_unknown_is_not_moved():
    with pytest.raises(migrate.MoveFailed) as failed:
        migrate.move_signage(Connection(), dict(SERVER, database=None), lambda question: "no")
    assert "which database" in failed.value.message


# ---- What the sites and the screens make of a piSignage server --------------

ANDRE = {"name": "andre.milocast.nl", "port": 3001, "code": "/srv/andre/pisignage-server",
         "media": "/srv/andre/media", "data": "/srv/andre/pisignage-server/data",
         "uri": "mongodb://127.0.0.1:27017/pisignage-dev", "database": "pisignage-dev"}


def _apache_view(conf):
    return ("VirtualHost configuration:\n"
            "*:443                  is a NameVirtualHost\n"
            "         default server andre.milocast.nl (%s:1)\n"
            "         port 443 namevhost andre.milocast.nl (%s:1)\n" % (conf, conf))


@pytest.fixture
def one_site(tmp_path):
    """A server with nothing on it but the one name Apache hands on to a piSignage server."""
    sites = tmp_path / "sites-enabled"
    sites.mkdir()
    (sites / "andre.conf").write_text(APACHE_SITE.split("<VirtualHost *:443>\n    ServerName www")[0])
    return str(sites)


def _sites(one_site, running):
    class Server(object):
        def __call__(self, *args, **named):
            if args[0] == "apache2ctl":
                return _apache_view(os.path.join(one_site, "andre.conf"))
            return ""
    return migrate.sites(Server(), sites_dir=one_site, sizes={}, nginx_dir=str(one_site),
                         lsws_root=str(one_site), signage=running)


def test_a_name_that_is_only_a_piSignage_server_is_known_for_one(one_site):
    """Not a website with files that somebody forgot to copy: an application with a name in front of it."""
    site = _sites(one_site, {"andre.milocast.nl": ANDRE})[0]
    assert site["signage"] == ANDRE
    # The proxy to its own port is what makes it a piSignage server, so it is not named as work by hand.
    assert site["redirect"] is None
    assert site["problem"] is None


def test_without_piSignage_running_the_same_name_is_still_a_proxy_to_set_up_by_hand(one_site):
    site = _sites(one_site, {})[0]
    assert site["signage"] is None
    assert "set it up by hand" in " ".join((site["redirect"] or {}).get("by_hand") or [])


def test_the_item_of_a_piSignage_name_says_what_moves_it(one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    assert item["signage"] == 3001
    assert "piSignage server" in item["problem"] and "port 3001" in item["problem"]
    assert "migrate.py signage" in item["problem"] and "Type g" in item["problem"]
    assert "wp-config.php" not in item["problem"]


def _row(item):
    return migrate.customers([item], {})[0]


def test_the_customer_screen_offers_to_move_it(one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    keys = dict((key, what) for key, what, _note in migrate.customer_menu(_row(item)))
    assert "piSignage" in keys["g"]
    assert migrate.customer_action(_row(item), "g")[1] == "signage"


def test_a_customer_without_one_is_not_offered_it(one_site):
    item, = migrate.site_items(_sites(one_site, {}))
    assert "g" not in dict((key, what) for key, what, _note in migrate.customer_menu(_row(item)))
    assert migrate.customer_action(_row(item), "g") == (None, None, False)


def test_moving_it_from_the_screen_moves_this_customers_server_and_no_other(monkeypatch, one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    moved, said = [], []
    monkeypatch.setattr(migrate, "signage_servers",
                        lambda run, notes=None: [ANDRE, dict(ANDRE, name="other.milocast.nl")])
    monkeypatch.setattr(migrate, "move_signage",
                        lambda connection, server, ask, run=None: moved.append(server["name"]) or ["moved it"])

    migrate._signage_for(None, _row(item), ask=lambda question: "yes", say=said.append)

    assert moved == ["andre.milocast.nl"]
    assert "moved it" in " ".join(said)


def test_it_is_not_moved_without_a_yes(monkeypatch, one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    moved, said = [], []
    monkeypatch.setattr(migrate, "signage_servers", lambda run, notes=None: [ANDRE])
    monkeypatch.setattr(migrate, "move_signage",
                        lambda connection, server, ask, run=None: moved.append(server["name"]))

    migrate._signage_for(None, _row(item), ask=lambda question: "no", say=said.append)

    assert moved == [] and "was left alone" in " ".join(said)


def test_a_server_that_is_no_longer_running_says_so_instead_of_moving_nothing(monkeypatch, one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    said = []
    monkeypatch.setattr(migrate, "signage_servers", lambda run, notes=None: [])
    migrate._signage_for(None, _row(item), ask=lambda question: "yes", say=said.append)
    assert "no piSignage server is running here under that name" in " ".join(" ".join(said).split())


# ---- What the board and the table call it -----------------------------------

def _item(one_site):
    item, = migrate.site_items(_sites(one_site, {"andre.milocast.nl": ANDRE}))
    return item


def _there(item):
    return {item["id"]: {"state": "exists", "detail": "The piSignage server is already here."}}


def test_the_board_says_signage_and_not_by_hand(one_site):
    """'by hand' means nobody here can move it; this one moves with one key, so the row says what it is."""
    item = _item(one_site)
    row = migrate.customers([item], {})[0]
    assert row.word("site") == migrate.SIGNAGE == "signage"
    assert migrate.next_step(row)[1] == "move the piSignage server"
    assert migrate.customers([item], _there(item))[0].word("site") == migrate.DONE


def test_the_table_names_the_server_instead_of_calling_it_not_wordpress(one_site):
    item = _item(one_site)
    row = migrate.customers([item], {})[0]
    screen = migrate.customer_screen(row, {}, new_server="web03.majorlabel.nl")
    # The row is not called Website either: it is not one, which is the whole thing this screen got wrong.
    assert not any(line.strip().startswith("Website") for line in screen.splitlines())
    row = next(line for line in screen.splitlines() if line.strip().startswith("piSignage"))
    assert "port 3001" in row and "not WordPress" not in screen
    assert row.split()[-1] == "signage"


def test_the_menu_does_not_offer_to_copy_a_website_that_is_not_there(one_site):
    """Every name of this customer is in front of the app, so there is no website to copy with number 2."""
    item = _item(one_site)
    for status in ({}, _there(item)):
        row = migrate.customers([item], status)[0]
        keys = [key for key, _what, _note in migrate.customer_menu(row)]
        assert "2" not in keys and "g" in keys
        assert "type g to move it" in migrate.why_nothing(row, "2")


def test_once_it_is_across_the_screen_stops_telling_you_to_move_it(one_site):
    item = _item(one_site)
    screen = migrate.customer_screen(migrate.customers([item], _there(item))[0], _there(item),
                                     new_server="web03.majorlabel.nl")
    assert "is on the new server already" in " ".join(screen.split())
    assert "has no website files to copy" not in " ".join(screen.split())


def test_the_dns_is_not_moved_before_the_server_is(one_site):
    """Its screens talk to the app, so a name pointing at a server without it is worse than one still here."""
    item = _item(one_site)
    said = []
    migrate._point_dns(None, migrate.customers([item], {})[0], ask=lambda question: "no", say=said.append)
    assert "type g here first" in " ".join(" ".join(said).split())

    said = []
    migrate._point_dns(None, migrate.customers([item], _there(item))[0], ask=lambda question: "no",
                       say=said.append)
    assert "type g here first" not in " ".join(said)
    assert "not on the new server" not in " ".join(said)


def test_the_screen_asks_the_new_server_again_once_the_server_is_across(monkeypatch, one_site, capsys):
    """Without this the screen under the move keeps saying "not there yet" about the thing that just went
    across, and so does the board it goes back to."""
    item = _item(one_site)
    status = {item["id"]: {"state": "new", "detail": "not here yet"}}
    there = {"state": "exists", "detail": "The piSignage server of andre.milocast.nl is already here."}
    asked = []

    class Connection(object):
        host = "web03.example.nl"

        def ask(self, request, timeout=None):
            asked.append(request["request"])
            if request["request"] == "status":
                return {"ok": True, "items": dict((one["id"], dict(there)) for one in request["items"])}
            return {"ok": True}

    monkeypatch.setattr(migrate, "signage_servers", lambda run, notes=None: [ANDRE])
    monkeypatch.setattr(migrate, "move_signage",
                        lambda connection, server, ask, run=None: ["Moved it."])
    monkeypatch.setattr(migrate, "_ask_about", lambda *args, **named: None)
    monkeypatch.setattr(migrate, "_dns_for", lambda *args, **named: None)
    monkeypatch.setattr(migrate, "_mail_for", lambda *args, **named: None)
    monkeypatch.setattr(migrate, "quick_look", lambda *args, **named: {})
    monkeypatch.setattr(migrate, "_question", lambda question: "yes")
    typed = ["g", "b"]
    monkeypatch.setattr(migrate, "_typed_line", lambda prompt: typed.pop(0))
    looked = (None, None, None, None, [item], status)

    migrate._open_customer("andre.milocast.nl", None, Connection(), None, [], [""], {}, False, set(), looked)

    assert "status" in asked  # it asked again, instead of drawing the screen from what it knew before
    assert status[item["id"]]["state"] == "exists"
    screen = capsys.readouterr().out
    assert screen.count("not there yet") == 1  # the screen above the move, not the one under it
    assert "there already" in screen
