"""domainctl wp, against a pretend wp-cli and MariaDB: nothing here runs as root, downloads or touches a database."""

import json

import pytest
from typer.testing import CliRunner

from domainctl import cli
from domainctl.core import acl, sites, users, wordpress
from domainctl.core.sites import Site
from serverctl import system
from test_cli import run, server  # noqa: F401  (server is a fixture)


class _Server:
    """What wp-cli and MariaDB would do, as far as domainctl can tell."""

    def __init__(self, config):
        self.config = config
        self.databases: set[str] = set()
        self.installed: set[str] = set()
        self.plugins: dict[str, str] = {}  # site user: WP fail2ban's status
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[str | None] = []
        self.fail_on: str | None = None
        self.fed: dict[tuple[str, ...], bytes] = {}  # what a program was given to read, by command

    def run(self, *args, stdin=None):
        self.calls.append(args)
        self.stdin.append(stdin)
        if args[0] == "mariadb":
            return self._sql(stdin)
        if args[0] == "runuser" and args[4] == "mkdir":
            (self.config.home_root / args[-1]).mkdir(parents=True, exist_ok=True)
            return ""
        user, command = args[2], args[9:]
        if self.fail_on and self.fail_on in command:
            raise system.CtlError(f"wp {' '.join(command)} failed")
        docroot = self.config.docroot_of(user)
        match command:
            case ("core", "download", *_):
                (docroot / "wp-includes").mkdir(parents=True)
                (docroot / "wp-includes" / "version.php").write_text("<?php")
                (docroot / "index.php").write_text("<?php")
            case ("config", "create", *_):
                (docroot / "wp-config.php").write_text("<?php")
            case ("core", "is-installed"):
                if user not in self.installed:
                    raise system.CtlError("not installed")
            case ("core", "install", *_):
                self.installed.add(user)
            case ("plugin", "list", *_):
                found = [{"name": "akismet", "status": "inactive"}]
                if user in self.plugins:
                    found.append({"name": wordpress.PLUGIN, "status": self.plugins[user]})
                return json.dumps(found)
            case ("plugin", "install" | "activate", *_):
                self.plugins[user] = "active"
        return ""

    def run_fed(self, *args, source):
        self.calls.append(args)
        self.fed[args] = source.read()

    def _sql(self, statements):
        if statements.startswith("SELECT"):
            name = statements.split("'")[1]
            return f"{int(name in self.databases)}\n0\n"
        if statements.startswith("CREATE DATABASE"):
            self.databases.add(statements.split("`")[1])
        if statements.startswith("DROP DATABASE"):
            self.databases.discard(statements.split("`")[1])
        return ""


@pytest.fixture
def fake(server, monkeypatch):  # noqa: F811
    pretend = _Server(server)
    monkeypatch.setattr(system, "run", pretend.run)
    monkeypatch.setattr(system, "run_fed", pretend.run_fed)
    monkeypatch.setattr(acl, "apply", lambda path, entries: True)
    return pretend


def site(config, user="example", domain="example.nl", placeholder=True):
    sites.save(config, Site(user, domain))
    docroot = config.docroot_of(user)
    (docroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    config.logs_of(user).mkdir(parents=True)
    if placeholder:
        (docroot / "index.html").write_text("This site is set up and waiting for its files.")
    return docroot


def install(*extra):
    return run("wp", "install", "example", "--admin-user", "jan", "--admin-email", "jan@example.nl", *extra)


def test_install_sets_wordpress_up_with_its_own_database_and_wp_fail2ban(server, fake):  # noqa: F811
    docroot = site(server)

    result = install()

    assert result.exit_code == 0, result.output
    assert fake.databases == {"example"}
    assert fake.installed == {"example"} and fake.plugins == {"example": "active"}
    assert not (docroot / "index.html").exists(), "the placeholder would be served before index.php"
    assert "WP fail2ban is active on example" in result.output
    assert "Log in at https://example.nl/wp-login.php" in result.output


def test_install_runs_wp_cli_as_the_sites_user_with_secrets_on_stdin(server, fake):  # noqa: F811
    site(server)
    install()
    wp_calls = [call for call in fake.calls if call[0] == "runuser" and wordpress.WP in call]
    assert wp_calls and all(call[:3] == ("runuser", "-u", "example") for call in wp_calls)
    create = next(call for call in wp_calls if "create" in call)
    assert "--prompt=dbpass" in create and not any("dbpass=" in arg for arg in create)
    database_password = fake.stdin[fake.calls.index(create)].strip()
    created = next(stdin for call, stdin in zip(fake.calls, fake.stdin) if stdin and "CREATE USER" in stdin)
    assert f"IDENTIFIED BY '{database_password}'" in created
    install_call = next(call for call in wp_calls if call[9:11] == ("core", "install"))
    admin_password = fake.stdin[fake.calls.index(install_call)].strip()
    assert "--url=https://example.nl" in install_call and admin_password not in " ".join(install_call)


def test_install_refuses_a_web_root_with_files_of_another_site(server, fake):  # noqa: F811
    docroot = site(server)
    (docroot / "shop.php").write_text("<?php")

    result = install()

    assert result.exit_code == 1
    assert "already holds files that aren't WordPress: shop.php" in result.output
    assert fake.databases == set() and not (docroot / "wp-includes").exists()


def test_install_leaves_a_database_it_did_not_make_alone(server, fake):  # noqa: F811
    site(server)
    fake.databases.add("example")

    result = install()

    assert result.exit_code == 1
    assert "There is already a database example in MariaDB" in result.output
    assert not any(stdin and "DROP" in stdin for stdin in fake.stdin)


def test_install_takes_the_database_away_again_when_wp_config_fails(server, fake):  # noqa: F811
    site(server)
    fake.fail_on = "create"

    result = install()

    assert result.exit_code == 1
    assert fake.databases == set(), "a database whose password nobody knows is no use"


def test_install_picks_up_where_it_stopped(server, fake):  # noqa: F811
    site(server)
    fake.fail_on = "install"
    assert install().exit_code == 1
    fake.fail_on = None

    result = install()

    assert result.exit_code == 0, result.output
    assert fake.installed == {"example"} and fake.plugins == {"example": "active"}
    assert sum(1 for call in fake.calls if call[9:11] == ("core", "download")) == 1


def test_install_on_an_installed_site_only_adds_wp_fail2ban(server, fake):  # noqa: F811
    site(server)
    install()
    fake.plugins.clear()

    result = run("wp", "install", "example")

    assert result.exit_code == 0, result.output
    assert fake.plugins == {"example": "active"}
    assert "Log in at" not in result.output


def test_check_says_which_sites_lack_wp_fail2ban(server, fake):  # noqa: F811
    for user, status in (("shop", "active"), ("blog", "inactive"), ("news", None)):
        docroot = site(server, user, f"{user}.nl", placeholder=False)
        (docroot / "wp-includes").mkdir()
        (docroot / "wp-includes" / "version.php").write_text("<?php")
        if status:
            fake.plugins[user] = status
    site(server, "static", "static.nl")

    result = run("wp", "check")

    assert result.exit_code == 0, result.output
    assert "installed, not active" in result.output and "not installed" in result.output
    assert "not WordPress" in result.output
    assert "2 sites can't be protected by fail2ban" in result.output
    assert fake.plugins == {"shop": "active", "blog": "inactive"}, "nothing changes without --fix"


def test_check_fix_puts_wp_fail2ban_on_every_wordpress_site(server, fake):  # noqa: F811
    for user in ("blog", "news"):
        docroot = site(server, user, f"{user}.nl", placeholder=False)
        (docroot / "wp-includes").mkdir()
        (docroot / "wp-includes" / "version.php").write_text("<?php")
    fake.plugins["blog"] = "inactive"

    result = run("wp", "check", "--fix")

    assert result.exit_code == 0, result.output
    assert fake.plugins == {"blog": "active", "news": "active"}
    activated = [call[9:11] for call in fake.calls if call[0] == "runuser" and call[9:10] == ("plugin",)]
    assert ("plugin", "activate") in activated and ("plugin", "install") in activated


WP_CONFIG = """<?php
define( 'DB_NAME', 'old_name' );
define("DB_USER","old_user");
  define(   'DB_PASSWORD'  ,  'old\\'pass'  );   // from the old server
define( 'DB_HOST', 'db.old.example' );
define( 'AUTH_KEY', 'put your unique phrase here' );
define( 'WP_HOME', 'https://example.nl' );
$table_prefix = 'xyz_';
"""


def with_config(config, text=WP_CONFIG):
    docroot = site(config)
    (docroot / "wp-config.php").write_text(text)
    return docroot / "wp-config.php"


def adopt(*extra):
    return run("wp", "adopt", "example", *extra)


def test_adopt_keeps_everything_in_wp_config_but_the_database_lines(server):  # noqa: F811
    """Salts, table prefix and a site's own constants are what makes it that site."""
    path = with_config(server)

    wordpress.adopt_config(server, "example", "example_db", "example_db", "se'cret")

    text = path.read_text()
    assert "define( 'DB_NAME', 'example_db' );" in text
    assert "define( 'DB_USER', 'example_db' );" in text
    assert "define( 'DB_PASSWORD', 'se\\'cret' );" in text
    assert f"define( 'DB_HOST', '{wordpress.DB_HOST}' );" in text
    assert "old" not in text.replace("put your unique", "")
    assert "$table_prefix = 'xyz_';" in text and "WP_HOME" in text and "AUTH_KEY" in text


def test_adopt_names_the_wp_config_that_has_no_database_lines(server):  # noqa: F811
    path = with_config(server, "<?php\n$table_prefix = 'wp_';\n")

    with pytest.raises(system.CtlError, match=f"{path} has no line for DB_NAME"):
        wordpress.adopt_config(server, "example", "x", "x", "y")
    assert path.read_text() == "<?php\n$table_prefix = 'wp_';\n"


def test_adopt_makes_the_database_reads_the_dump_and_points_wp_config_at_it(server, fake, tmp_path):  # noqa: F811
    path = with_config(server)
    dump = tmp_path / "example.sql"
    dump.write_text("CREATE TABLE xyz_posts (id int);")

    result = adopt("--dump", str(dump))

    assert result.exit_code == 0, result.output
    assert fake.databases == {"example"}
    assert ("mariadb", "example") in fake.calls
    assert fake.fed[("mariadb", "example")] == b"CREATE TABLE xyz_posts (id int);"
    assert "define( 'DB_NAME', 'example' );" in path.read_text()
    assert "now points at it" in result.output


def test_adopt_refuses_a_database_that_is_already_there(server, fake):  # noqa: F811
    path = with_config(server)
    fake.databases.add("example")

    result = adopt()

    assert result.exit_code == 1
    assert "already" in result.output
    assert path.read_text() == WP_CONFIG and not any(stdin and "DROP" in stdin for stdin in fake.stdin)


def test_adopt_replaces_a_database_when_told_to(server, fake, tmp_path):  # noqa: F811
    with_config(server)
    fake.databases.add("example")
    dump = tmp_path / "example.sql"
    dump.write_text("INSERT INTO t VALUES (1);")

    result = adopt("--replace", "--yes", "--dump", str(dump))

    assert result.exit_code == 0, result.output
    assert "Replaced" in result.output
    assert any(stdin and stdin.startswith("DROP DATABASE") for stdin in fake.stdin)
    assert fake.databases == {"example"}


def test_adopt_does_not_empty_a_database_without_asking(server, fake):  # noqa: F811
    with_config(server)
    fake.databases.add("example")

    result = adopt("--replace")

    assert result.exit_code == 1
    assert not any(stdin and "DROP" in stdin for stdin in fake.stdin)


def test_adopt_reads_the_dump_from_standard_input(server, fake):  # noqa: F811
    """So the old server can pipe mysqldump straight into it over SSH."""
    with_config(server)

    result = CliRunner().invoke(cli.app, ["wp", "adopt", "example", "--dump", "-"], input=b"INSERT INTO t VALUES (1);")

    assert result.exit_code == 0, result.output
    assert fake.fed[("mariadb", "example")] == b"INSERT INTO t VALUES (1);"


def test_adopt_takes_the_database_away_again_when_the_dump_fails(server, fake, tmp_path, monkeypatch):  # noqa: F811
    path = with_config(server)
    dump = tmp_path / "example.sql"
    dump.write_text("broken")

    def failing(*args, source):
        raise system.CtlError("mariadb example failed: syntax error")

    monkeypatch.setattr(system, "run_fed", failing)
    result = adopt("--dump", str(dump))

    assert result.exit_code == 1
    assert fake.databases == set() and path.read_text() == WP_CONFIG


def test_adopt_needs_the_sites_wp_config(server, fake):  # noqa: F811
    site(server)

    result = adopt()

    assert result.exit_code == 1 and "no wp-config.php" in result.output
    assert fake.databases == set()


def test_adopt_hands_the_dump_over_byte_for_byte(server, fake, tmp_path):  # noqa: F811
    """A latin1 dump must not turn into replacement characters, nor depend on the server's locale."""
    with_config(server)
    dump = tmp_path / "latin1.sql"
    content = "INSERT INTO t VALUES ('caf\xe9');".encode("latin-1") + bytes([0xff, 0x00, 0xfe])
    dump.write_bytes(content)

    result = adopt("--dump", str(dump))

    assert result.exit_code == 0, result.output
    assert fake.fed[("mariadb", "example")] == content


def test_adopt_checks_wp_config_before_it_touches_the_old_database(server, fake, tmp_path):  # noqa: F811
    path = with_config(server, "<?php\ndefine( 'DB_NAME', 'x' );\n")
    fake.databases.add("example")
    dump = tmp_path / "example.sql"
    dump.write_text("INSERT INTO t VALUES (1);")

    result = adopt("--replace", "--yes", "--dump", str(dump))

    assert result.exit_code == 1 and "no line for DB_USER, DB_PASSWORD" in result.output
    assert fake.databases == {"example"} and not any(stdin and "DROP" in stdin for stdin in fake.stdin)
    assert not fake.fed


def test_adopt_says_the_old_database_is_gone_when_it_fails_after_replacing_it(server, fake, tmp_path,
                                                                           monkeypatch):  # noqa: F811
    with_config(server)
    fake.databases.add("example")
    dump = tmp_path / "example.sql"
    dump.write_text("broken")

    def failing(*args, source):
        raise system.CtlError("mariadb example failed: syntax error")

    monkeypatch.setattr(system, "run_fed", failing)
    result = adopt("--replace", "--yes", "--dump", str(dump))

    assert result.exit_code == 1
    assert "old database example is gone" in result.output.replace("\n", " ")


def test_adopt_works_for_a_site_whose_virtual_host_waits_for_the_dns(server, fake, monkeypatch):  # noqa: F811
    """domainctl add --no-dns leaves a user and directories only; the files and database come over before the DNS."""
    docroot = server.docroot_of("example")
    docroot.mkdir(parents=True)
    server.logs_of("example").mkdir(parents=True)
    (docroot / "wp-config.php").write_text(WP_CONFIG)
    monkeypatch.setattr(users, "exists", lambda name: name == "example")
    assert sites.list_sites(server) == []

    result = adopt()

    assert result.exit_code == 0, result.output
    assert "waiting for the DNS" in result.output
    assert fake.databases == {"example"}


def test_adopt_still_refuses_a_user_that_domainctl_did_not_make(server, fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(users, "exists", lambda name: True)
    result = adopt()
    assert result.exit_code == 1 and "There is no site example" in result.output
