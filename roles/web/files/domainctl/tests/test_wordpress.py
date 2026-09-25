"""domainctl wp, against a pretend wp-cli and MariaDB: nothing here runs as root, downloads or touches a database."""

import json

import pytest

from domainctl.core import acl, sites, wordpress
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
