import base64
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
from ipaddress import ip_address
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
from mailctl.core import db, openlitespeed, reach, system, transip  # noqa: E402
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
# spamc as a working SpamAssassin answers: the message back, with the headers it scored it with.
FAKE_SPAMC = r"""
cat > /dev/null
printf 'X-Spam-Checker-Version: SpamAssassin 4.0.0\nX-Spam-Flag: YES\n'
printf 'X-Spam-Status: Yes, score=1000.0 required=5.0 tests=GTUBE\n\n%s\n' "the test message"
"""
# postconf as a server whose Postfix hands mail to spamass-milter.
FAKE_POSTCONF = "echo unix:/var/spool/postfix/spamass/spamass.sock\n"

# How the public half of those keys starts in a DKIM record.
FAKE_KEY_RECORD_START = "v=DKIM1; h=sha256; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQ"


@pytest.fixture
def config(tmp_path) -> Config:
    """A configuration pointing every path into the test's temp directory, owned by the current user."""
    user = pwd.getpwuid(os.geteuid()).pw_name
    return Config(
        hostname="server.hosting.example",
        send_limits=(SendLimit(recipients=300, seconds=3600), SendLimit(recipients=1000, seconds=86400)),
        db_socket="",
        vmail_root=tmp_path / "vmail",
        vmail_user=user,
        ols_user=user,
        dkim_keys=tmp_path / "opendkim" / "keys",
        dkim_key_table=tmp_path / "opendkim" / "KeyTable",
        dkim_signing_table=tmp_path / "opendkim" / "SigningTable",
        dkim_user=user,
        mail_logs=(tmp_path / "mail.log.1", tmp_path / "mail.log"),
        sieve_after=tmp_path / "sieve-after",
        certificate_file=tmp_path / "fullchain.pem",
        transip_settings=tmp_path / "mailctl" / "transip.json",
        transip_key=tmp_path / "mailctl" / "transip.key",
        ols_root=tmp_path / "lsws",
        autodiscover_root=tmp_path / "mailautodiscover",
        letsencrypt_dir=tmp_path / "letsencrypt",
    )


def make_certificate(path: Path, *names: str, days: int = 90) -> Path:
    """A self-signed certificate like Let's Encrypt's: the names are its subject alternative names."""
    subject_alt_names = ",".join(f"DNS:{name}" for name in names)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(path.with_suffix(".key")), "-out", str(path), "-days", str(days),
         "-subj", f"/CN={names[0]}", "-addext", f"subjectAltName={subject_alt_names}"],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture(scope="session")
def transip_key_pair(tmp_path_factory) -> tuple[Path, Path]:
    """A private key like the ones TransIP's control panel makes, and its public half."""
    directory = tmp_path_factory.mktemp("transip")
    private, public = directory / "transip.key", directory / "transip.pub"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True, capture_output=True)
    return private, public


class FakeTransip:
    """TransIP's API with the domain example.nl. It checks login signatures with the public key, and records every
    request as (method, path, body, headers). With whitelisted False, this server isn't on the API whitelist, so only
    tokens that work anywhere work: others are refused when they're asked for, or with refused_on_use when they're
    used."""

    TOKEN = "fake-token"
    WHITELIST_ONLY_TOKEN = "fake-whitelist-only-token"

    def __init__(self, public_key: Path, workdir: Path) -> None:
        self._public_key = public_key
        self._workdir = workdir
        self.whitelisted = True
        self.refused_on_use = False
        self.requests: list[tuple[str, str, dict | None, dict[str, str]]] = []
        self.zones: dict[str, list[dict]] = {"example.nl": []}
        self.nameservers = {"example.nl": ["ns0.transip.net", "ns1.transip.nl", "ns2.transip.eu"]}
        self.answers: dict[tuple[str, str], list[tuple[int, dict]]] = {}  # answers to give first, per request

    def entries(self, domain: str = "example.nl") -> list[tuple[str, int, str, str]]:
        return [(entry["name"], entry["expire"], entry["type"], entry["content"]) for entry in self.zones[domain]]

    def paths(self, method: str | None = None) -> list[str]:
        return [path for requested, path, _, _ in self.requests if method in (None, requested)]

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        path = url.removeprefix(transip.API)
        self.requests.append((method, path, json.loads(body) if body else None, headers))
        if self.answers.get((method, path)):
            status, answer = self.answers[(method, path)].pop(0)
            return status, json.dumps(answer).encode()
        if path == "/auth":
            if not self._signed(body, headers.get("Signature", "")):
                return 401, b'{"error": "Signature invalid"}'
            if not self.whitelisted and not json.loads(body)["global_key"]:
                if not self.refused_on_use:
                    return 403, b'{"error": "Remote IP 203.0.113.5 is not authorized for this request"}'
                return 201, json.dumps({"token": self.WHITELIST_ONLY_TOKEN}).encode()
            return 201, json.dumps({"token": self.TOKEN}).encode()
        if headers.get("Authorization") == f"Bearer {self.WHITELIST_ONLY_TOKEN}":
            return 403, b'{"error": "Remote IP 203.0.113.5 is not whitelisted"}'
        if headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return 401, b'{"error": "Invalid token"}'
        if path == "/api-test":
            return 200, b'{"ping": "pong"}'
        domain, resource = path.removeprefix("/domains/").split("/")
        if domain not in self.zones:
            return 404, json.dumps({"error": f"Domain with name '{domain}' not found"}).encode()
        if (method, resource) == ("GET", "dns"):
            return 200, json.dumps({"dnsEntries": self.zones[domain]}).encode()
        if (method, resource) == ("PUT", "dns"):
            self.zones[domain] = json.loads(body)["dnsEntries"]
            return 204, b""
        if (method, resource) == ("GET", "nameservers"):
            names = [{"hostname": name, "ipv4": "", "ipv6": ""} for name in self.nameservers[domain]]
            return 200, json.dumps({"nameservers": names}).encode()
        return 405, b'{"error": "Method not allowed"}'

    def _signed(self, body: bytes, signature: str) -> bool:
        signed, signature_file = self._workdir / "signed", self._workdir / "signature"
        signed.write_bytes(body)
        signature_file.write_bytes(base64.b64decode(signature))
        verify = ["openssl", "dgst", "-sha512", "-verify", str(self._public_key), "-signature", str(signature_file)]
        return subprocess.run([*verify, str(signed)], capture_output=True).returncode == 0


@pytest.fixture
def fake_transip(transip_key_pair, tmp_path, monkeypatch) -> FakeTransip:
    api = FakeTransip(transip_key_pair[1], tmp_path)
    monkeypatch.setattr(transip, "_send", api.send)
    monkeypatch.setattr(transip, "BUSY_WAIT", 0)
    return api


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


@pytest.fixture(autouse=True)
def everything_answers(monkeypatch):
    """No test reaches the network: every address answers, unless a test says otherwise."""
    monkeypatch.setattr(reach, "answers", lambda address, port, timeout=None: True)


@pytest.fixture(autouse=True)
def lock_file(tmp_path, monkeypatch):
    """The lock OpenLiteSpeed's config is changed under, in the test's own directory instead of /run.

    This has to patch the module the code calls: patching another one passes just as quietly and leaves the tests
    taking the real lock. tests/test_locking.py checks that it didn't.
    """
    monkeypatch.setattr(openlitespeed, "LOCK_FILE", tmp_path / "openlitespeed.lock")


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
        # SOGo makes its own tables; this is the one mailctl reads and writes, as SOGo creates it.
        cursor.execute("DROP DATABASE IF EXISTS sogo")
        cursor.execute("CREATE DATABASE sogo")
        cursor.execute("CREATE TABLE sogo.sogo_user_profile (c_uid varchar(255) NOT NULL PRIMARY KEY, "
                       "c_defaults text, c_settings text)")
        cursor.execute("CREATE TABLE sogo.sogo_folder_info (c_folder_id bigint unsigned NOT NULL AUTO_INCREMENT "
                       "PRIMARY KEY, c_path varchar(255) NOT NULL, c_path1 varchar(255) NOT NULL, "
                       "c_path2 varchar(255), c_foldername varchar(255) NOT NULL, c_location varchar(2048), "
                       "c_quick_location varchar(2048), c_acl_location varchar(2048), "
                       "c_folder_type varchar(255) NOT NULL)")
        cursor.execute("CREATE TABLE sogo.sogo_acl (c_folder_id bigint unsigned NOT NULL, "
                       "c_object varchar(255) NOT NULL, c_uid varchar(255) NOT NULL, c_role varchar(80) NOT NULL)")
        cursor.execute("CREATE TABLE sogo.sogo_store (c_folder_id bigint unsigned NOT NULL, "
                       "c_name varchar(255) NOT NULL, c_content longtext NOT NULL)")
        cursor.execute("CREATE TABLE sogo.sogo_cache_folder (c_uid varchar(255) NOT NULL, "
                       "c_path varchar(255) NOT NULL, c_content longtext)")
        cursor.execute("CREATE TABLE sogo.sogo_users (c_uid varchar(255) NOT NULL PRIMARY KEY, "
                       "c_name varchar(255), c_password varchar(255), c_cn varchar(255), mail varchar(255))")
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
    """mailctl working on the test database and the test's temp directory, on a server at 203.0.113.5."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(asdict(db_config), default=str))
    monkeypatch.setenv("MAILCTL_CONFIG", str(config_file))
    monkeypatch.setattr(system, "require_root", lambda: None)
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address("203.0.113.5")})
    db_config.vmail_root.mkdir()
    fake_command("systemctl")
    fake_command("doveadm")
    fake_command("sievec")
    fake_command("spamc", FAKE_SPAMC)
    fake_command("postconf", FAKE_POSTCONF)
    return Mailctl()
