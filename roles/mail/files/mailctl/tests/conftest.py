import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import NoReturn

import pymysql
import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ROLE_FILES = PACKAGE_ROOT.parent

sys.path.insert(0, str(PACKAGE_ROOT))
# Rich reads the width when mailctl's console is created; a wide one keeps table cells on one line.
os.environ["COLUMNS"] = "200"

from typer.testing import CliRunner  # noqa: E402

from mailctl import cli  # noqa: E402
from mailctl.core import db, system  # noqa: E402
from mailctl.core.config import Config, SendLimit  # noqa: E402

SCHEMAS = {
    "mailserver": ROLE_FILES / "setup_mailserver_tables.sql",
    "spamassassin": ROLE_FILES / "setup_spamassassin_tables.sql",
}
PASSWORD = "correct horse"

# Makes a real private key where opendkim-genkey puts it, but a short one, which is quicker.
FAKE_OPENDKIM_GENKEY = r"""
while getopts b:d:D:s: option; do
  case $option in D) dir=$OPTARG;; s) selector=$OPTARG;; esac
done
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:1024 -out "$dir/$selector.private" 2>/dev/null
"""
# How the public half of those keys starts in a DKIM record.
FAKE_KEY_RECORD_START = "v=DKIM1; h=sha256; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQ"


@pytest.fixture
def config(tmp_path) -> Config:
    """A configuration pointing every path into the test's temp directory, owned by the current user."""
    user = pwd.getpwuid(os.geteuid()).pw_name
    return Config(
        hostname="mail.example.nl",
        send_limits=(SendLimit(recipients=300, seconds=3600), SendLimit(recipients=1000, seconds=86400)),
        db_socket="",
        vmail_root=tmp_path / "vmail",
        vmail_user=user,
        dkim_keys=tmp_path / "opendkim" / "keys",
        dkim_key_table=tmp_path / "opendkim" / "KeyTable",
        dkim_signing_table=tmp_path / "opendkim" / "SigningTable",
        dkim_user=user,
        mail_logs=(tmp_path / "mail.log.1", tmp_path / "mail.log"),
        sieve_after=tmp_path / "sieve-after",
    )


class FakeCommand:
    """A fake program first on PATH that records the arguments of every call. Installing it again starts a new record."""

    def __init__(self, directory: Path, name: str, script: str) -> None:
        self._log = directory / f"{name}.calls"
        self._log.unlink(missing_ok=True)
        program = directory / name
        program.write_text(f"#!/bin/sh\nprintf '[%s]' \"$@\" >> '{self._log}'\necho >> '{self._log}'\n{script}\n")
        program.chmod(0o755)

    @property
    def calls(self) -> list[list[str]]:
        if not self._log.exists():
            return []
        return [re.findall(r"\[([^]]*)\]", line) for line in self._log.read_text().splitlines()]


@pytest.fixture
def fake_command(tmp_path, monkeypatch):
    """fake_command(name, shell script) installs a FakeCommand."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda name, script="": FakeCommand(bin_dir, name, script)


@pytest.fixture
def fake_opendkim_genkey(fake_command) -> FakeCommand:
    return fake_command("opendkim-genkey", FAKE_OPENDKIM_GENKEY)


@pytest.fixture(scope="session")
def database_socket(tmp_path_factory):
    """A throwaway MariaDB or MySQL server for the whole session, reachable only through its socket.

    MAILCTL_TEST_DB_PREFIX names the installation to use, like an unpacked mariadb-server-core package;
    by default it's the one on this machine. Without a server the database tests are skipped, or fail
    when MAILCTL_REQUIRE_DB is set.
    """
    if os.geteuid() == 0:
        _unavailable("database servers refuse to run as root")
    prefix = Path(os.environ.get("MAILCTL_TEST_DB_PREFIX") or "/")
    workdir = tmp_path_factory.mktemp("database")
    # Socket paths are limited to about 100 characters, too short for pytest's temp directories.
    socket_dir = Path(tempfile.mkdtemp(prefix="mailctl-", dir="/tmp"))
    socket = socket_dir / "db.sock"
    try:
        commands = _server_commands(prefix, workdir, socket)
        if commands is None:
            _unavailable(f"no MariaDB or MySQL server under {prefix}")
        initialise, run = commands
        with (workdir / "initialise.out").open("w") as output:
            subprocess.run(initialise, stdout=output, stderr=subprocess.STDOUT, check=True)
        server = subprocess.Popen(run)
        try:
            _wait_for_server(socket, server)
            yield str(socket)
        finally:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("MAILCTL_REQUIRE_DB"):
        pytest.fail(reason)
    pytest.skip(reason)


def _server_commands(prefix: Path, workdir: Path, socket: Path) -> tuple[list[str], list[str]] | None:
    """The commands that create a data directory and run a server on it, for the installation under prefix."""
    data = f"--datadir={workdir / 'data'}"
    run_options = [f"--socket={socket}", "--skip-networking",
                   f"--pid-file={workdir / 'server.pid'}", f"--log-error={workdir / 'server.log'}"]
    mariadbd = prefix / "usr/sbin/mariadbd"
    if mariadbd.exists():
        base = ["--no-defaults", f"--basedir={prefix / 'usr'}"]
        return (
            [str(prefix / "usr/bin/mariadb-install-db"), *base, data,
             "--auth-root-authentication-method=normal", "--skip-test-db"],
            [str(mariadbd), *base, f"--lc-messages-dir={prefix / 'usr/share/mariadb'}", data, *run_options],
        )
    mysqld = prefix / "usr/sbin/mysqld"
    if mysqld.exists():
        # AppArmor confines /usr/sbin/mysqld to the system's data directories; a copy elsewhere isn't confined.
        base = [str(shutil.copy2(mysqld, workdir / "mysqld")), "--no-defaults", f"--basedir={prefix / 'usr'}", data]
        return [*base, "--initialize-insecure"], [*base, "--mysqlx=OFF", *run_options]
    return None


def _wait_for_server(socket: Path, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if server.poll() is not None:
            pytest.fail("the database server stopped during startup")
        try:
            pymysql.connect(unix_socket=str(socket), user="root").close()
            return
        except pymysql.err.OperationalError:
            time.sleep(0.2)
    pytest.fail("the database server didn't start within 60 seconds")


@pytest.fixture
def db_config(config, database_socket) -> Config:
    """The test configuration, connected to freshly created mail databases."""
    connection = pymysql.connect(unix_socket=database_socket, user="root", autocommit=True)
    with connection.cursor() as cursor:
        for name, schema in SCHEMAS.items():
            cursor.execute(f"DROP DATABASE IF EXISTS {name}")
            cursor.execute(f"CREATE DATABASE {name}")
            cursor.execute(f"USE {name}")
            for statement in re.sub(r"--[^\n]*", "", schema.read_text()).split(";"):
                if statement.strip():
                    cursor.execute(statement)
    connection.close()
    return replace(config, db_socket=database_socket)


@pytest.fixture
def database(db_config):
    with db.connect(db_config) as connected:
        yield connected


class Mailctl:
    """Runs mailctl commands as a user would, without a terminal, and checks how they end."""

    def __init__(self) -> None:
        self._runner = CliRunner(mix_stderr=False)

    def run(self, *args: str, stdin: str | None = None):
        return self._runner.invoke(cli.app, list(args), input=stdin, catch_exceptions=False)

    def ok(self, *args: str, stdin: str | None = None) -> str:
        """The standard output of a command that succeeds; what Ansible reads."""
        result = self.run(*args, stdin=stdin)
        assert result.exit_code == 0, result.stdout + result.stderr
        return result.stdout

    def fails(self, *args: str, stdin: str | None = None) -> str:
        """Everything a failing command printed."""
        result = self.run(*args, stdin=stdin)
        assert result.exit_code == 1, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        return result.stdout + result.stderr


@pytest.fixture
def mailctl(db_config, tmp_path, monkeypatch, fake_command, fake_opendkim_genkey) -> Mailctl:
    """mailctl working on the test database and the test's temp directory."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(asdict(db_config), default=str))
    monkeypatch.setenv("MAILCTL_CONFIG", str(config_file))
    monkeypatch.setattr(system, "require_root", lambda: None)
    db_config.vmail_root.mkdir()
    fake_command("systemctl")
    fake_command("doveadm")
    return Mailctl()
