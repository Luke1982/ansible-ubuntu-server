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
