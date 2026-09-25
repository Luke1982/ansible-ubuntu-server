import json
import os
import shutil
import subprocess

import pytest
from conftest import PASSWORD, ROLE_FILES

from mailctl import ui
from mailctl.core import addresses, sogofilters, transfer
from mailctl.core.errors import MailctlError

HASH = "{SHA512-CRYPT}$6$salt$" + "x" * 86
ROUNDCUBE = 'require ["fileinto"];\nif header :contains "subject" "invoice" { fileinto "Invoices"; }\n'
VACATION = 'require ["vacation"];\nvacation :days 1 "Away";\n'
SERVER_FILE = {
    "version": 1,
    "domains": ["example.nl", "shop.nl"],
    "addresses": [
        {"address": "info@example.nl", "password_hash": HASH},
        {"address": "Sales@Example.nl", "password": "correct horse battery"},
        {"address": "info@shop.nl", "password_hash": "$6$salt$" + "y" * 86},
    ],
    "forwards": [
        {"source": "sales@example.nl", "destination": "info@example.nl", "send_as": True},
        {"source": "sales@example.nl", "destination": "sales@example.nl"},
        {"source": "orders@shop.nl", "destination": "someone@elsewhere.nl"},
    ],
    "spam": [
        {"target": "server", "setting": "required_score", "value": "6"},
        {"target": "example.nl", "setting": "whitelist_from", "value": "*@partner.nl *@supplier.nl"},
        {"target": "info@example.nl", "setting": "required_score", "value": "4"},
        {"target": "info@shop.nl", "setting": "use_bayes", "value": "0"},
    ],
    "sieve": [
        {"address": "info@example.nl", "name": "roundcube", "active": True, "content": ROUNDCUBE},
        {"address": "info@example.nl", "name": "vacation", "active": False, "content": VACATION},
    ],
}
# Keeps each account's filters in a folder, as Dovecot would, so what's put can be listed and read back.
FAKE_DOVEADM = r"""
if [ "$1 $2" = "pw -l" ]; then echo "SHA1 MD5-CRYPT PLAIN SHA256-CRYPT SHA512-CRYPT BLF-CRYPT CRYPT"; exit 0; fi
[ "$1" = sieve ] || exit 0
dir="$SIEVE_STORE/$4"; mkdir -p "$dir"
case "$2" in
  put) cat > "$dir/$5" ;;
  activate) echo "$5" > "$dir/.active" ;;
  get) echo "sieve script:"; cat "$dir/$5" ;;
  list) for f in "$dir"/*; do [ -f "$f" ] || continue; n=$(basename "$f")
        if [ "$n" = "$(cat "$dir/.active" 2>/dev/null)" ]; then echo "$n ACTIVE"; else echo "$n"; fi; done ;;
esac
"""


def write(tmp_path, content, name="mailserver.json"):
    path = tmp_path / name
    path.write_text(json.dumps(content))
    path.chmod(0o600)
    return path


@pytest.fixture
def doveadm(fake_command, tmp_path, monkeypatch):
    monkeypatch.setenv("SIEVE_STORE", str(tmp_path / "sieve-store"))
    return fake_command("doveadm", FAKE_DOVEADM)


@pytest.fixture
def filled_server(mailctl, doveadm, tmp_path):
    """A server with what SERVER_FILE holds, imported."""
    mailctl.ok("import", str(write(tmp_path, SERVER_FILE)), "--yes")


def rows(database, sql, *params):
    return [tuple(row.values()) for row in database.rows(sql, *params)]


# Reading the file

def test_read_takes_the_file_apart(tmp_path):
    contents = transfer.read(write(tmp_path, SERVER_FILE))

    assert contents.domains == ["example.nl", "shop.nl"]
    assert contents.accounts[1] == transfer.Account("sales@example.nl", password="correct horse battery")
    assert contents.forwards == [transfer.Forward("sales@example.nl", "info@example.nl", True),
                                 transfer.Forward("orders@shop.nl", "someone@elsewhere.nl", False)], \
        "a forward to itself is how a copy was kept, which adding the forwards does here"
    assert transfer.SpamValue("example.nl", "welcomelist_from", "*@supplier.nl") in contents.spam, \
        "the old name of the setting, with two senders in one value"
    assert transfer.SpamValue("info@shop.nl", "use_bayes", "0") in contents.spam
    assert [script.name for script in contents.filters] == ["roundcube", "vacation"]


def test_read_accepts_a_file_with_only_some_parts(tmp_path):
    contents = transfer.read(write(tmp_path, {"domains": ["example.nl"]}))

    assert contents.domains == ["example.nl"] and contents.accounts == []


@pytest.mark.parametrize("change, message", [
    ({"addresses": [{"address": "info@example.nl"}]}, 'should have either a "password" or a "password_hash"'),
    ({"addresses": [{"address": "info@example.nl", "password": "short"}]}, "The password needs at least"),
    ({"addresses": [{"address": "info@example.nl", "password_hash": "plain text"}]}, "isn't one Dovecot reads"),
    ({"addresses": [{"address": "info@", "password_hash": HASH}]}, "isn't a valid e-mail address"),
    ({"forwards": [{"source": "a@example.nl", "destination": "b@example.nl", "send_as": "yes"}]}, "true or false"),
    ({"spam": [{"target": "server", "setting": "required_score", "value": "a lot"}]}, "isn't a number"),
    ({"sieve": [{"address": "nobody@example.nl", "name": "x", "content": ""}]}, "which isn't an account in the file"),
    ({"sieve": [{"address": "info@example.nl", "name": "../x", "content": ""}]}, "isn't a filter name"),
    ({"accounts": []}, "parts mailctl doesn't know: accounts"),
    ({"version": 2}, "version 2 of the file"),
])
def test_read_says_what_is_wrong_and_where(tmp_path, change, message):
    with pytest.raises(MailctlError) as problem:
        transfer.read(write(tmp_path, {**SERVER_FILE, **change}))

    assert message in problem.value.message


def test_read_refuses_two_active_filters_for_an_account(tmp_path):
    scripts = [{**script, "active": True} for script in SERVER_FILE["sieve"]]

    with pytest.raises(MailctlError, match="more than one active filter for info@example.nl"):
        transfer.read(write(tmp_path, {**SERVER_FILE, "sieve": scripts}))


# Importing

def test_import_creates_everything_in_the_file(mailctl, doveadm, tmp_path, database, db_config):
    output = mailctl.ok("import", str(write(tmp_path, SERVER_FILE)), "--yes")

    assert "Imported 2 domains, 3 accounts, 2 forwards, 5 spam settings, 2 filters" in output
    assert rows(database, "SELECT email, password FROM virtual_users WHERE email LIKE 'info@%%' ORDER BY email") == \
        [("info@example.nl", HASH), ("info@shop.nl", "{SHA512-CRYPT}$6$salt$" + "y" * 86)], \
        "the hashes are kept, with the scheme webmail needs in front"
    assert database.value("SELECT password FROM virtual_users WHERE email = 'sales@example.nl'") \
        .startswith("{SHA512-CRYPT}$6$"), "a password in plain text is hashed"
    listed = [line.split() for line in mailctl.ok("forward", "list").splitlines()]
    assert ["sales@example.nl", "info@example.nl", "✓"] in listed
    assert ["orders@shop.nl", "someone@elsewhere.nl", "–"] in listed
    assert ("sales@example.nl", "sales@example.nl") in rows(database, "SELECT source, destination FROM virtual_aliases"), \
        "sales@ keeps a copy in its own mailbox, as mailctl forward add does"
    assert sorted(rows(database, "SELECT username, preference, value FROM spamassassin.userpref")) == [
        ("$GLOBAL", "required_score", "6"), ("%example.nl", "welcomelist_from", "*@partner.nl"),
        ("%example.nl", "welcomelist_from", "*@supplier.nl"), ("info@example.nl", "required_score", "4"),
        ("info@shop.nl", "use_bayes", "0")]
    assert "1 rule of them is in webmail's filters now" in output, "the rules go where filters are edited"
    assert "Not in webmail: " in output, "an away message isn't something webmail's filters have"
    assert [rule["name"] for rule in sogofilters.read(database, "info@example.nl")] == ["roundcube 1"]
    assert ["sieve", "activate", "-u", "info@example.nl", sogofilters.SCRIPT] in doveadm.calls
    assert (tmp_path / "sieve-store" / "info@example.nl" / "vacation").read_text() == VACATION
    assert (db_config.vmail_root / "shop.nl" / "info" / "Maildir").is_dir()
    assert (db_config.dkim_keys / "shop.nl").is_dir()
    assert "mailctl dns show shop.nl" in output


def test_import_skips_what_is_already_here_with_a_friendly_note(mailctl, doveadm, tmp_path, database):
    mailctl.ok("domain", "add", "example.nl")
    mailctl.ok("address", "add", "info@example.nl", "--password-stdin", stdin=f"{PASSWORD}\n")
    mailctl.ok("spam", "set", "server", "required_score", "5")
    before = database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'")

    result = mailctl.run("import", str(write(tmp_path, SERVER_FILE)), "--yes")

    assert result.exit_code == 0, result.stdout + result.stderr
    output = result.stdout
    assert "✓ Already on this server, left as they are: 1 domain, 1 account and 1 server spam setting." in output
    assert "info@example.nl" not in output.split("Already on this server")[1].split("\n")[1], \
        "the names are only listed with --verbose"
    assert "!" not in output and not result.stderr, "what's here already is fine, not a warning"
    assert "Imported 1 domain, 2 accounts, 2 forwards, 1 spam setting" in output
    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'") == before
    assert not [call for call in doveadm.calls if call[:2] == ["sieve", "put"]], "the filters of an account that's here stay"
    assert "welcomelist_from" not in mailctl.ok("spam", "show", "example.nl"), "a domain that's here keeps its settings"


def test_import_names_what_is_skipped_with_verbose(mailctl, doveadm, tmp_path):
    mailctl.ok("domain", "add", "example.nl")

    output = mailctl.ok("import", str(write(tmp_path, SERVER_FILE)), "--dry-run", "--verbose")

    assert "Already on this server, left as they are: 1 domain." in output
    assert "\n  example.nl\n" in output


def test_import_again_finds_everything_there(mailctl, filled_server, tmp_path):
    output = mailctl.ok("import", str(write(tmp_path, SERVER_FILE)), "--yes")

    assert "Everything in" in output and "is already on this server" in output
    assert "Already on this server, left as they are" not in output, "one line says it all"


def test_import_needs_every_domain_on_this_server_or_in_the_file(mailctl, doveadm, tmp_path):
    output = mailctl.fails("import", str(write(tmp_path, {**SERVER_FILE, "domains": ["example.nl"]})), "--yes")

    assert "shop.nl is neither on this server nor in the file." in output
    assert "There are no domains yet" in mailctl.ok("domain", "list")


def test_import_dry_run_changes_nothing(mailctl, doveadm, tmp_path):
    output = mailctl.ok("import", str(write(tmp_path, SERVER_FILE)), "--dry-run")

    assert "To create from" in output and "Nothing was changed (--dry-run)." in output
    assert "Would put 1 rule of roundcube in webmail's filters." in output
    assert "There are no domains yet" in mailctl.ok("domain", "list")


def test_import_creates_nothing_when_a_filter_doesnt_compile(mailctl, doveadm, fake_command, tmp_path):
    fake_command("sievec", 'case "$1" in *vacation.sieve) echo "line 2: error: unknown command" >&2; exit 1;; esac')

    output = mailctl.fails("import", str(write(tmp_path, SERVER_FILE)), "--yes")

    assert "1 filter in" in output and "nothing was imported" in output
    assert "There are no domains yet" in mailctl.ok("domain", "list")


# Password hashes of another scheme

MD5_HASH = "$1$salt$" + "z" * 22
ARGON_HASH = "{ARGON2ID}$argon2id$v=19$m=65536,t=3,p=1$" + "a" * 60
OLD_SCHEMES = {"domains": ["example.nl"], "addresses": [
    {"address": "info@example.nl", "password_hash": HASH},
    {"address": "old@example.nl", "password_hash": MD5_HASH},
    {"address": "argon@example.nl", "password_hash": ARGON_HASH},
], "sieve": [{"address": "argon@example.nl", "name": "roundcube", "active": True, "content": ROUNDCUBE}]}


@pytest.fixture
def typed_passwords(monkeypatch):
    """typed_passwords(...) makes mailctl ask, as in a terminal, and gives the answers to its password prompts."""
    def answer(*passwords):
        answers, prompts = iter(passwords), []
        monkeypatch.setattr(ui, "interactive", lambda: True)
        monkeypatch.setattr(ui, "ask_secret", lambda prompt: prompts.append(prompt) or next(answers))
        return prompts
    return answer


@pytest.mark.parametrize("stored, expected", [
    (HASH, HASH),
    ("$6$salt$abc", "{SHA512-CRYPT}$6$salt$abc"),
    ("$5$salt$abc", "{SHA256-CRYPT}$5$salt$abc"),
    ("$2y$10$abc", "{BLF-CRYPT}$2y$10$abc"),
    ("$y$j9T$abc", "{CRYPT}$y$j9T$abc"),
])
def test_a_hash_gets_its_scheme_in_front(stored, expected):
    assert addresses.labelled(stored) == expected


def test_import_asks_for_new_passwords_where_the_hash_is_weak_or_unreadable(mailctl, doveadm, tmp_path, database,
                                                                           typed_passwords):
    prompts = typed_passwords("", "a new password", "a new password")

    output = mailctl.ok("import", str(write(tmp_path, OLD_SCHEMES)), "--yes")

    assert "! 1 account has a password hash Dovecot here can't read (ARGON2ID)" in output
    assert "1 account has a password hash of another scheme (MD5-CRYPT): mailctl asks for new passwords (one of " \
           "them weak), and Enter keeps a hash." in output
    assert prompts[0] == "New password for argon@example.nl (ARGON2ID now; Enter skips the account)"
    assert prompts[1] == "New password for old@example.nl (MD5-CRYPT now; Enter keeps the hash)"
    assert "argon@example.nl is skipped, with its spam settings and filters." in output
    assert "Imported 1 domain, 2 accounts from" in output
    new_hash = database.value("SELECT password FROM virtual_users WHERE email = 'old@example.nl'")
    assert new_hash.startswith("{SHA512-CRYPT}$6$") and new_hash != MD5_HASH
    assert not addresses.exists(database, "argon@example.nl")
    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'") == HASH


def test_import_keeps_a_weak_hash_when_enter_is_pressed(mailctl, doveadm, tmp_path, database, typed_passwords):
    typed_passwords("a new password", "a new password", "")

    mailctl.ok("import", str(write(tmp_path, OLD_SCHEMES)), "--yes")

    assert database.value("SELECT password FROM virtual_users WHERE email = 'old@example.nl'") == "{MD5-CRYPT}" + MD5_HASH
    assert addresses.exists(database, "argon@example.nl")


def test_import_asks_for_a_password_where_the_hash_is_of_another_scheme(mailctl, doveadm, tmp_path, database,
                                                                       typed_passwords):
    """This server hashes passwords its own way, and the only way to a hash that fits is the password itself."""
    prompts = typed_passwords("a new password", "a new password")
    file = write(tmp_path, {"domains": ["example.nl"], "addresses": [
        {"address": "info@example.nl", "password_hash": "$5$salt$" + "s" * 43}]})

    output = mailctl.ok("import", str(file), "--yes")

    assert "1 account has a password hash of another scheme (SHA256-CRYPT)" in output
    assert prompts[0] == "New password for info@example.nl (SHA256-CRYPT now; Enter keeps the hash)"
    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'").startswith("{SHA512")


def test_import_keeps_a_hash_of_another_scheme_when_enter_is_pressed(mailctl, doveadm, tmp_path, database,
                                                                     typed_passwords):
    typed_passwords("")
    file = write(tmp_path, {"domains": ["example.nl"], "addresses": [
        {"address": "info@example.nl", "password_hash": "$5$salt$" + "s" * 43}]})

    mailctl.ok("import", str(file), "--yes")

    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'") \
        == "{SHA256-CRYPT}$5$salt$" + "s" * 43, "Dovecot reads it as well as its own"


def test_import_with_keep_hashes_asks_nothing_and_keeps_them(mailctl, doveadm, tmp_path, database, typed_passwords):
    prompts = typed_passwords()
    file = write(tmp_path, {"domains": ["example.nl"], "addresses": [
        {"address": "info@example.nl", "password_hash": "$5$salt$" + "s" * 43}]})

    mailctl.ok("import", str(file), "--yes", "--keep-hashes")

    assert prompts == []
    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'") \
        == "{SHA256-CRYPT}$5$salt$" + "s" * 43


def test_import_asks_for_every_other_scheme_with_new_passwords(mailctl, doveadm, tmp_path, database, typed_passwords):
    prompts = typed_passwords("a new password", "a new password")
    file = write(tmp_path, {"domains": ["example.nl"], "addresses": [
        {"address": "info@example.nl", "password_hash": "$5$salt$" + "s" * 43},
        {"address": "sales@example.nl", "password_hash": HASH}]})

    mailctl.ok("import", str(file), "--yes", "--new-passwords")

    assert prompts == ["New password for info@example.nl (SHA256-CRYPT now; Enter keeps the hash)", "Password again"]
    assert database.value("SELECT password FROM virtual_users WHERE email = 'info@example.nl'").startswith("{SHA512")


def test_import_without_a_terminal_keeps_weak_hashes_and_says_so(mailctl, doveadm, tmp_path, database):
    file = write(tmp_path, {"domains": ["example.nl"], "addresses": [{"address": "old@example.nl",
                                                                       "password_hash": MD5_HASH}]})

    output = mailctl.ok("import", str(file), "--yes")

    assert "1 account has a password hash of another scheme (MD5-CRYPT): they're kept (one of them weak); " \
           "set new ones with: mailctl address password ADDRESS." in output
    assert database.value("SELECT password FROM virtual_users WHERE email = 'old@example.nl'") == "{MD5-CRYPT}" + MD5_HASH


def test_import_without_a_terminal_refuses_a_hash_dovecot_cant_read(mailctl, doveadm, tmp_path):
    output = mailctl.fails("import", str(write(tmp_path, OLD_SCHEMES)), "--yes", "--keep-hashes")

    assert "New passwords are needed for 1 account" in output
    assert "There are no domains yet" in mailctl.ok("domain", "list")


def test_import_reads_filters_exported_with_doveadms_header(tmp_path):
    scripts = [{**SERVER_FILE["sieve"][0], "content": "sieve script:\n" + ROUNDCUBE}]

    contents = transfer.read(write(tmp_path, {**SERVER_FILE, "sieve": scripts}))

    assert contents.filters[0].content == ROUNDCUBE


# Exporting

def test_export_writes_what_import_reads(mailctl, filled_server, tmp_path):
    path = tmp_path / "export.json"

    output = mailctl.ok("export", str(path))

    assert "Wrote 2 domains, 3 accounts, 2 forwards, 5 spam settings, 3 filters" in output, \
        "the two imported filters, and webmail's, which holds the rules of one of them"
    assert path.stat().st_mode & 0o777 == 0o600
    exported = json.loads(path.read_text())
    assert {"address": "info@example.nl", "password_hash": HASH} in exported["addresses"]
    assert {"source": "sales@example.nl", "destination": "info@example.nl", "send_as": True} in exported["forwards"]
    assert {"target": "server", "setting": "required_score", "value": "6"} in exported["spam"]
    assert {"address": "info@example.nl", "name": "roundcube", "active": False, "content": ROUNDCUBE} in exported["sieve"]
    assert {"address": "info@example.nl", "name": sogofilters.SCRIPT, "active": True} \
        .items() <= next(script for script in exported["sieve"] if script["name"] == sogofilters.SCRIPT).items(), \
        "webmail's filter is the one that runs"
    transfer.read(path)


def test_export_asks_before_overwriting_a_file(mailctl, filled_server, tmp_path):
    path = tmp_path / "export.json"
    path.write_text("keep me")

    assert "Overwrite it?" in mailctl.fails("export", str(path))
    assert path.read_text() == "keep me"
    mailctl.ok("export", str(path), "--yes")
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_server_moves_through_export_and_import(mailctl, filled_server, tmp_path):
    first = tmp_path / "first.json"
    mailctl.ok("export", str(first))
    for domain in ("example.nl", "shop.nl"):
        mailctl.ok("domain", "delete", domain, "--yes", "--delete-mail")
    shutil.rmtree(tmp_path / "sieve-store")
    mailctl.ok("spam", "unset", "server", "required_score")

    mailctl.ok("import", str(first), "--yes")

    second = tmp_path / "second.json"
    mailctl.ok("export", str(second))
    there, back = json.loads(first.read_text()), json.loads(second.read_text())
    # The rules also went into webmail's list, which webmail keeps in a filter of its own: that one is new.
    assert [script["name"] for script in back["sieve"] if script["name"] == sogofilters.SCRIPT]
    for server in (there, back):
        server["sieve"] = [script for script in server["sieve"] if script["name"] != sogofilters.SCRIPT]
    assert back == there


# export-mailserver.sh, for servers without mailctl

def test_the_script_writes_what_mailctl_export_writes(mailctl, filled_server, fake_command, db_config, tmp_path):
    client = shutil.which("mariadb") or shutil.which("mysql")
    if client is None or shutil.which("jq") is None:
        pytest.skip("the script needs jq and the mysql or mariadb client")
    fake_command("id", "echo 0")
    fake_command("mariadb", f'exec {client} --no-defaults --socket={db_config.db_socket} --user=root "$@"')
    by_mailctl, by_script = tmp_path / "mailctl.json", tmp_path / "script.json"
    mailctl.ok("export", str(by_mailctl))

    result = subprocess.run(["bash", str(ROLE_FILES / "export-mailserver.sh"), str(by_script)],
                            capture_output=True, text=True, env=os.environ)

    assert result.returncode == 0, result.stderr
    assert "Wrote 2 domains, 3 accounts, 2 forwards, 5 spam settings and 3 filters" in result.stderr
    assert by_script.stat().st_mode & 0o777 == 0o600
    exported, expected = json.loads(by_script.read_text()), json.loads(by_mailctl.read_text())
    for part in ("forwards", "sieve"):  # the order may differ
        exported[part].sort(key=json.dumps)
        expected[part].sort(key=json.dumps)
    assert exported == expected
