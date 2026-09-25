import base64
import io
import json
import re
import tarfile
import subprocess
from pathlib import Path

import pytest
from conftest import FAKE_KEY_RECORD_START, PASSWORD, Mailctl

from mailctl import ui
from mailctl.core import mailbox, sogofilters, system
from mailctl.core.errors import MailctlError

ROUNDCUBE = 'require ["fileinto"];\nif header :contains "subject" "invoice" { fileinto "Invoices"; }\n'

COMMANDS = [
    ("domain", "add"), ("domain", "list"), ("domain", "delete"),
    ("address", "add"), ("address", "import"), ("address", "list"), ("address", "password"), ("address", "delete"),
    ("forward", "add"), ("forward", "list"), ("forward", "delete"),
    ("dkim", "show"), ("dkim", "create"),
    ("dns", "show"), ("dns", "publish"), ("dns", "credentials"), ("autodiscover", "publish"),
    ("certificate", "sync"),
    ("status",), ("doctor",), ("repair",),
    ("spam", "show"), ("spam", "set"), ("spam", "unset"), ("spam", "learn"),
    ("filters", "show"), ("filters", "import"), ("filters", "run"),
]


@pytest.fixture
def terminal(monkeypatch):
    """Makes mailctl ask for missing values, as it does in a terminal."""
    monkeypatch.setattr(ui, "interactive", lambda: True)


@pytest.fixture
def typed_passwords(monkeypatch):
    """typed_passwords(...) gives the answers to the hidden password prompts, in order."""
    def answer(*passwords):
        answers = iter(passwords)
        monkeypatch.setattr(ui, "ask_secret", lambda prompt: next(answers))
    return answer


@pytest.fixture
def example_domain(mailctl):
    mailctl.ok("domain", "add", "example.nl")


def add_account(mailctl, address):
    mailctl.ok("address", "add", address, "--password-stdin", stdin=f"{PASSWORD}\n")


def ansible_change_messages():
    """The output that the Ansible task running 'mailctl dkim create' counts as a change."""
    task = Path(__file__).resolve().parents[3] / "tasks" / "configure-opendkim.yml"
    return re.findall(r"'([^']+)' in dkim_create\.stdout", task.read_text())


def test_help_lists_the_command_groups(mailctl):
    output = mailctl.ok("--help")

    for group in ("domain", "address", "forward", "dkim", "dns", "status", "doctor", "spam", "filters"):
        assert re.search(rf"^\W*{group}\s{{2,}}\S", output, re.MULTILINE), group


def test_help_works_for_users_other_than_root(monkeypatch):
    def refuse():
        raise MailctlError("mailctl must run as root.")

    monkeypatch.setattr(system, "require_root", refuse)

    assert "Usage" in Mailctl().ok("address", "add", "--help")


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_explains_itself_with_an_example(mailctl, command):
    output = mailctl.ok(*command, "--help")

    assert "Usage" in output
    assert "Example" in output


def test_arguments_that_are_asked_for_show_as_optional(mailctl):
    assert "[DOMAIN]" in mailctl.ok("domain", "delete", "--help")


def test_domain_add_signs_the_domain_and_prints_the_records_to_publish(mailctl, db_config):
    output = mailctl.ok("domain", "add", "Example.NL")

    assert "Added example.nl" in output
    assert re.search(r"MX\s+example\.nl\n\s+10 mail\.example\.nl\.\n", output)
    assert re.search(r"A\s+mail\.example\.nl\n\s+203\.0\.113\.5\n", output)
    for expected in ("v=spf1 mx ~all", "mail._domainkey.example.nl", FAKE_KEY_RECORD_START, "v=DMARC1; p=quarantine"):
        assert expected in output
    assert db_config.dkim_signing_table.read_text() == "*@example.nl mail._domainkey.example.nl\n"


def test_domain_add_leaves_out_the_address_records_when_the_servers_addresses_cant_be_read(mailctl, monkeypatch):
    def no_addresses():
        raise MailctlError("ip isn't installed.")

    monkeypatch.setattr(system, "server_ips", no_addresses)

    output = mailctl.ok("domain", "add", "example.nl")

    assert "10 mail.example.nl" in output
    assert not re.search(r"^\s*A+\s+mail\.example\.nl$", output, re.MULTILINE)
    assert "Couldn't read this server's addresses" in output
    assert "ip isn't installed" in output


def test_domain_add_keeps_the_domain_when_the_key_cant_be_created(mailctl, fake_command):
    fake_command("opendkim-genkey", "echo 'key generation failed' >&2; exit 1")

    output = mailctl.ok("domain", "add", "example.nl")

    assert "Added example.nl" in output
    assert "Couldn't create the DKIM key" in output
    assert "Create the DKIM key later with: mailctl dkim create example.nl" in output
    assert "example.nl" in mailctl.ok("domain", "list")


def test_domain_add_keeps_a_key_that_was_copied_over_without_its_record_file(mailctl, db_config, fake_opendkim_genkey):
    key = db_config.dkim_keys / "example.nl" / "mail.private"
    key.parent.mkdir(parents=True)
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024", "-out", str(key)],
        check=True, capture_output=True,
    )
    public_key = subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"], check=True, capture_output=True
    ).stdout

    output = mailctl.ok("domain", "add", "example.nl")

    assert f"p={base64.b64encode(public_key).decode()}" in output
    assert fake_opendkim_genkey.calls == []
    assert "*@example.nl" in db_config.dkim_signing_table.read_text()


def test_opendkim_gets_updated_on_the_next_run_after_a_failed_reload(mailctl, fake_command):
    fake_command("systemctl", "exit 1")
    assert "Couldn't update OpenDKIM" in mailctl.ok("domain", "add", "example.nl")

    systemctl = fake_command("systemctl")

    assert "Updated the OpenDKIM tables" in mailctl.ok("dkim", "create", "example.nl")
    assert systemctl.calls == [["reload-or-restart", "opendkim"]]


def test_domain_add_keeps_the_domain_when_opendkim_cant_be_updated(mailctl, fake_command):
    fake_command("systemctl", "echo 'Failed to reload opendkim.service' >&2; exit 1")

    output = mailctl.ok("domain", "add", "example.nl")

    assert "Added example.nl" in output
    assert "Couldn't update OpenDKIM" in output
    assert "example.nl" in mailctl.ok("domain", "list")


def test_a_missing_argument_without_a_terminal_is_named(mailctl):
    assert "DOMAIN is missing" in mailctl.fails("domain", "add")


def test_errors_show_a_message_and_a_hint(mailctl):
    output = mailctl.fails("address", "add", "info@example.nl", "--password-stdin", stdin=f"{PASSWORD}\n")

    assert "example.nl isn't a domain on this server" in output
    assert "mailctl domain add example.nl" in output


def test_invalid_names_are_refused(mailctl):
    assert "isn't a valid e-mail address" in mailctl.fails("address", "add", "info", "--password-stdin", stdin=f"{PASSWORD}\n")


def test_missing_values_are_asked_for_in_a_terminal(mailctl, terminal):
    assert "Added example.nl" in mailctl.ok("domain", "add", stdin="example.nl\n")


def test_domain_list_shows_each_domain(mailctl, example_domain):
    add_account(mailctl, "info@example.nl")

    output = mailctl.ok("domain", "list")

    assert re.search(r"example\.nl\s+1\s+0\s+0 B\s+✓", output)


def test_domain_list_without_domains(mailctl):
    assert "no domains yet" in mailctl.ok("domain", "list")


def test_domain_delete_removes_its_addresses_key_and_mail(mailctl, example_domain, db_config):
    add_account(mailctl, "info@example.nl")

    output = mailctl.ok("domain", "delete", "example.nl", "--yes", "--delete-mail")

    assert "This also deletes 1 address of example.nl" in output
    assert "Deleted example.nl" in output
    assert not (db_config.dkim_keys / "example.nl").exists()
    assert not (db_config.vmail_root / "example.nl").exists()
    assert db_config.dkim_signing_table.read_text() == ""
    assert "no domains yet" in mailctl.ok("domain", "list")


def test_domain_delete_finishes_and_warns_when_opendkim_cant_be_updated(mailctl, example_domain, db_config, fake_command):
    add_account(mailctl, "info@example.nl")
    fake_command("systemctl", "exit 1")

    output = mailctl.ok("domain", "delete", "example.nl", "--yes", "--delete-mail")

    assert "Deleted example.nl" in output
    assert "Couldn't update OpenDKIM" in output
    assert not (db_config.vmail_root / "example.nl").exists()


def test_domain_delete_without_a_terminal_needs_yes(mailctl, example_domain):
    assert "--yes" in mailctl.fails("domain", "delete", "example.nl")


def test_domain_delete_lists_at_most_ten_of_the_addresses_it_deletes(mailctl, example_domain):
    for number in range(12):
        add_account(mailctl, f"user{number:02}@example.nl")

    output = mailctl.ok("domain", "delete", "example.nl", "--yes", "--delete-mail")

    assert "This also deletes 12 addresses of example.nl" in output
    assert "user09@example.nl" in output
    assert "user10@example.nl" not in output
    assert "and 2 more" in output


def test_domain_delete_in_a_terminal_wants_the_name_typed(mailctl, example_domain, terminal):
    refused = mailctl.fails("domain", "delete", "example.nl", stdin="other.nl\n")

    assert "Delete example.nl? Type example.nl to confirm" in refused
    assert "Nothing was changed" in refused
    assert "Deleted example.nl" in mailctl.ok("domain", "delete", "example.nl", stdin="example.nl\n")


def test_deleting_logs_out_the_sessions_of_the_deleted_accounts(mailctl, example_domain, fake_command):
    add_account(mailctl, "info@example.nl")
    add_account(mailctl, "sales@example.nl")
    doveadm = fake_command("doveadm")

    mailctl.ok("address", "delete", "info@example.nl", "--yes", "--keep-mail")
    mailctl.ok("domain", "delete", "example.nl", "--yes", "--keep-mail")

    assert doveadm.calls == [["kick", "info@example.nl"], ["kick", "sales@example.nl"]]


def test_domain_delete_warns_about_forwards_from_other_domains(mailctl, example_domain):
    mailctl.ok("domain", "add", "other.nl")
    add_account(mailctl, "info@example.nl")
    mailctl.ok("forward", "add", "sales@other.nl", "info@example.nl", "--no-send-as")
    mailctl.ok("forward", "add", "sales@example.nl", "info@example.nl", "--no-send-as")

    output = mailctl.ok("domain", "delete", "example.nl", "--yes", "--keep-mail")

    assert "sales@other.nl still forwards to info@example.nl" in output
    assert "sales@example.nl still forwards" not in output


def test_address_add_creates_the_account_and_its_maildir(mailctl, example_domain, db_config):
    output = mailctl.ok("address", "add", "info@example.nl", "--password-stdin", stdin=f"{PASSWORD}\n")

    assert "Created info@example.nl" in output
    assert (db_config.vmail_root / "example.nl" / "info" / "Maildir" / ".Sent").is_dir()
    assert "info@example.nl" in mailctl.ok("address", "list")


def test_address_add_without_a_terminal_needs_the_password_on_standard_input(mailctl, example_domain):
    assert "--password-stdin" in mailctl.fails("address", "add", "info@example.nl")


def test_address_add_refuses_a_short_password_from_standard_input(mailctl, example_domain):
    output = mailctl.fails("address", "add", "info@example.nl", "--password-stdin", stdin="short\n")

    assert "at least 8 characters" in output
    assert "info@example.nl" not in mailctl.ok("address", "list")


def test_address_add_asks_for_the_password_twice(mailctl, example_domain, terminal, typed_passwords):
    typed_passwords("short", "correct horse", "different one", "correct horse", "correct horse")

    output = mailctl.ok("address", "add", "info@example.nl")

    assert "at least 8 characters" in output
    assert "don't match" in output
    assert "Created info@example.nl" in output


def test_address_add_in_a_terminal_offers_to_add_a_missing_domain(mailctl, terminal, typed_passwords):
    typed_passwords("correct horse", "correct horse")

    output = mailctl.ok("address", "add", "info@example.nl", stdin="y\n")

    assert "Added example.nl" in output
    assert "Created info@example.nl" in output


def test_address_add_changes_nothing_when_stopped_at_the_password(mailctl, terminal, monkeypatch):
    def stop(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "ask_secret", stop)

    result = mailctl.run("address", "add", "info@example.nl", stdin="y\n")

    # Ctrl+C ends with exit status 1 on Typer 0.9 and 130 on newer versions.
    assert result.exit_code in (1, 130)
    assert "no domains yet" in mailctl.ok("domain", "list")


def test_address_list_of_one_domain(mailctl, example_domain):
    mailctl.ok("domain", "add", "other.nl")
    add_account(mailctl, "info@example.nl")
    add_account(mailctl, "info@other.nl")

    output = mailctl.ok("address", "list", "other.nl")

    assert "info@other.nl" in output
    assert "info@example.nl" not in output
    assert "never" in output


def test_listings_of_one_domain_say_when_it_has_nothing(mailctl, example_domain):
    assert "example.nl has no accounts yet" in mailctl.ok("address", "list", "example.nl")
    assert "example.nl has no forwards yet" in mailctl.ok("forward", "list", "example.nl")


def test_listings_show_stored_text_as_it_is(mailctl, example_domain, database):
    database.execute(
        "INSERT INTO virtual_aliases (domain_id, source, destination)"
        " SELECT id, 'sales@example.nl', 'odd[/b]@gmail.com' FROM virtual_domains"
    )

    assert "odd[/b]@gmail.com" in mailctl.ok("forward", "list")


def test_address_password_replaces_the_password(mailctl, example_domain, database):
    add_account(mailctl, "info@example.nl")
    before = database.value("SELECT password FROM virtual_users")

    output = mailctl.ok("address", "password", "info@example.nl", "--password-stdin", stdin="another password\n")

    assert "Changed the password of info@example.nl" in output
    assert database.value("SELECT password FROM virtual_users") != before


def test_address_delete_without_a_terminal_needs_yes(mailctl, example_domain):
    add_account(mailctl, "info@example.nl")

    assert "--yes" in mailctl.fails("address", "delete", "info@example.nl", "--keep-mail")


def test_address_delete_without_a_terminal_needs_a_decision_about_the_mail(mailctl, example_domain):
    add_account(mailctl, "info@example.nl")

    assert "--delete-mail" in mailctl.fails("address", "delete", "info@example.nl", "--yes")


@pytest.mark.parametrize("flag, mail_kept", [("--keep-mail", True), ("--delete-mail", False)])
def test_address_delete_keeps_or_deletes_the_mail(mailctl, example_domain, db_config, flag, mail_kept):
    add_account(mailctl, "info@example.nl")

    assert "Deleted info@example.nl" in mailctl.ok("address", "delete", "info@example.nl", "--yes", flag)

    assert (db_config.vmail_root / "example.nl" / "info").exists() is mail_kept
    assert "info@example.nl" not in mailctl.ok("address", "list")


def test_address_delete_wont_delete_mail_through_a_symbolic_link(mailctl, example_domain, db_config, tmp_path):
    add_account(mailctl, "info@example.nl")
    moved = tmp_path / "moved"
    (db_config.vmail_root / "example.nl").rename(moved)
    (db_config.vmail_root / "example.nl").symlink_to(moved)

    output = mailctl.ok("address", "delete", "info@example.nl", "--yes", "--delete-mail")

    assert "Deleted info@example.nl" in output
    assert "Couldn't delete the mail" in output
    assert "symbolic link" in output
    assert (moved / "info").is_dir()


def test_address_delete_mentions_forwards_that_stay_and_kept_mail(mailctl, example_domain, db_config):
    add_account(mailctl, "info@example.nl")
    mailctl.ok("forward", "add", "info@example.nl", "info@gmail.com")
    mailctl.ok("forward", "add", "sales@example.nl", "info@example.nl", "--no-send-as")

    output = mailctl.ok("address", "delete", "info@example.nl", "--yes", "--keep-mail")

    assert "info@example.nl still forwards to info@gmail.com" in output
    # Mail forwarded to info@ still reaches info@gmail.com, so there is nothing to warn about.
    assert "sales@example.nl still forwards" not in output
    assert f"Its mail is kept in {db_config.vmail_root / 'example.nl' / 'info'}" in output
    assert "is in the account again" in mailctl.ok("address", "add", "info@example.nl", "--password-stdin", stdin=f"{PASSWORD}\n")


def test_address_delete_warns_about_forwards_to_it(mailctl, example_domain):
    add_account(mailctl, "info@example.nl")
    mailctl.ok("forward", "add", "sales@example.nl", "info@example.nl", "--no-send-as")

    output = mailctl.ok("address", "delete", "info@example.nl", "--yes", "--keep-mail")

    assert "sales@example.nl still forwards to info@example.nl, which is no longer on this server" in output


def test_forward_add_list_and_delete(mailctl, example_domain):
    add_account(mailctl, "piet@example.nl")

    output = mailctl.ok("forward", "add", "sales@example.nl", "piet@example.nl", "--send-as")
    assert "sales@example.nl now forwards to piet@example.nl" in output
    assert "piet@example.nl may send as sales@example.nl" in output

    listing = mailctl.ok("forward", "list")
    assert "sales@example.nl" in listing
    assert "piet@example.nl" in listing

    output = mailctl.ok("forward", "delete", "sales@example.nl", "piet@example.nl")
    assert "sales@example.nl no longer forwards to piet@example.nl" in output
    assert "piet@example.nl may no longer send as sales@example.nl" in output
    assert "no forwards" in mailctl.ok("forward", "list")


def test_forward_delete_notices_a_send_as_permission_written_in_capitals(mailctl, example_domain, database):
    add_account(mailctl, "piet@example.nl")
    database.execute(
        "INSERT INTO virtual_aliases (domain_id, source, destination)"
        " SELECT id, 'sales@example.nl', 'Piet@example.nl' FROM virtual_domains"
    )
    database.execute(
        "INSERT INTO virtual_sender_aliases (domain_id, users, alias)"
        " SELECT id, 'Piet@example.nl', 'sales@example.nl' FROM virtual_domains"
    )

    output = mailctl.ok("forward", "delete", "sales@example.nl", "piet@example.nl")

    assert "piet@example.nl may no longer send as sales@example.nl" in output


def test_forward_add_checks_the_forward_before_asking_about_sending(mailctl, example_domain):
    add_account(mailctl, "piet@example.nl")

    assert "piet@example.nl can't forward to itself" in mailctl.fails("forward", "add", "piet@example.nl", "piet@example.nl")


def test_forward_to_an_account_without_a_terminal_needs_a_send_as_decision(mailctl, example_domain):
    add_account(mailctl, "piet@example.nl")

    assert "--send-as" in mailctl.fails("forward", "add", "sales@example.nl", "piet@example.nl")


def test_forward_to_an_outside_address_doesnt_ask_about_sending(mailctl, example_domain):
    assert "now forwards to someone@gmail.com" in mailctl.ok("forward", "add", "sales@example.nl", "someone@gmail.com")


def test_forward_add_says_that_an_outside_address_cant_send_as_the_source(mailctl, example_domain):
    output = mailctl.ok("forward", "add", "sales@example.nl", "someone@gmail.com", "--send-as")

    assert "someone@gmail.com isn't an account on this server, so it can't send as sales@example.nl" in output


def test_forwarding_an_account_keeps_a_copy_in_its_mailbox(mailctl, example_domain):
    add_account(mailctl, "info@example.nl")

    assert "keeps a copy" in mailctl.ok("forward", "add", "info@example.nl", "info@gmail.com")


def test_dkim_create_tells_ansible_whether_anything_changed(mailctl, db_config):
    created, updated = ansible_change_messages()
    assert created in mailctl.ok("dkim", "create", "a.nl")
    mailctl.ok("dkim", "create", "b.nl")

    unchanged = mailctl.ok("dkim", "create", "a.nl")
    assert "a.nl already has a DKIM key" in unchanged
    assert created not in unchanged
    assert updated not in unchanged

    # The tables the old playbook left behind listed only the server's own domain.
    db_config.dkim_signing_table.write_text("*@a.nl mail._domainkey.a.nl\n")
    assert updated in mailctl.ok("dkim", "create", "a.nl")
    assert "*@b.nl" in db_config.dkim_signing_table.read_text()


def test_dkim_create_shows_a_new_key_even_when_opendkim_cant_be_reloaded(mailctl, fake_command):
    fake_command("systemctl", "echo 'Job for opendkim.service failed' >&2; exit 1")

    output = mailctl.fails("dkim", "create", "example.nl")

    assert "Created a DKIM key for example.nl" in output
    assert FAKE_KEY_RECORD_START in output
    assert "opendkim.service failed" in output


def test_dkim_show_prints_the_record(mailctl):
    mailctl.ok("dkim", "create", "example.nl")

    output = mailctl.ok("dkim", "show", "example.nl")

    assert "mail._domainkey.example.nl" in output
    assert FAKE_KEY_RECORD_START in output


def test_dkim_show_without_a_key_explains_how_to_create_one(mailctl):
    assert "mailctl dkim create example.nl" in mailctl.fails("dkim", "show", "example.nl")


# mailctl address import: the accounts of a server being moved here, with the passwords their owners already have.

ACCOUNTS = {"info@example.nl": "correct horse battery", "sales@example.nl": "another one entirely"}


def stored_hash(database, address):
    return database.value("SELECT password FROM virtual_users WHERE email = %s", address)


def crypt_like_dovecot(database, address, password):
    """The hash the account should have: the same salt, so only the password decides whether they match."""
    salt = stored_hash(database, address).split("$")[2]
    hashed = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
                            input=password + "\n", capture_output=True, text=True, check=True).stdout.strip()
    return "{SHA512-CRYPT}" + hashed


def accounts_file(tmp_path, accounts=None, name="accounts.json"):
    path = tmp_path / name
    path.write_text(json.dumps(ACCOUNTS if accounts is None else accounts))
    path.chmod(0o600)
    return path


def test_import_creates_every_account_in_the_file(mailctl, example_domain, tmp_path, database, db_config):
    path = accounts_file(tmp_path)

    output = mailctl.ok("address", "import", str(path), "--yes")

    assert "Created 2 accounts" in output
    listed = mailctl.ok("address", "list")
    for address, password in ACCOUNTS.items():
        assert address in listed
        assert mailbox.home_dir(db_config, address).is_dir(), "the account got no mailbox"
        assert stored_hash(database, address) == crypt_like_dovecot(database, address, password), \
            "the password in the file isn't the one the account got"


def test_import_leaves_an_account_that_is_already_there(mailctl, example_domain, tmp_path, terminal, typed_passwords):
    typed_passwords("the password it has now", "the password it has now")
    mailctl.ok("address", "add", "info@example.nl")
    path = accounts_file(tmp_path)

    output = mailctl.ok("address", "import", str(path), "--yes")

    assert "1 account in" in output and "is already here and left as they are." in output
    assert "Created 1 account" in output


def test_import_checks_the_whole_file_before_creating_anything(mailctl, example_domain, tmp_path):
    path = accounts_file(tmp_path, {"info@example.nl": "correct horse battery", "sales@example.nl": "short"})

    output = mailctl.fails("address", "import", str(path), "--yes")

    assert "The password needs at least" in output
    assert "There are no accounts yet." in mailctl.ok("address", "list")


def test_import_needs_the_domains_to_be_on_this_server(mailctl, example_domain, tmp_path):
    path = accounts_file(tmp_path, {"info@example.nl": "correct horse battery",
                                    "info@elsewhere.nl": "correct horse battery"})

    output = mailctl.fails("address", "import", str(path), "--yes")

    assert "elsewhere.nl is not on this server" in output
    assert "There are no accounts yet." in mailctl.ok("address", "list")


def test_import_can_check_a_file_without_creating_anything(mailctl, example_domain, tmp_path):
    path = accounts_file(tmp_path)

    output = mailctl.ok("address", "import", str(path), "--dry-run")

    assert "2 accounts to create" in output and "Nothing was created (--dry-run)." in output
    assert "There are no accounts yet." in mailctl.ok("address", "list")


def test_import_says_to_delete_a_file_of_passwords_others_can_read(mailctl, example_domain, tmp_path):
    path = accounts_file(tmp_path)
    path.chmod(0o644)

    output = mailctl.ok("address", "import", str(path), "--yes")

    assert "can be read by others, and it holds passwords" in output
    assert f"Delete {path} now" in output


SA_LEARN = 'echo "Learned tokens from $# message(s) ($# message(s) examined)"'


def junk_and_inbox(db_config, address="info@example.nl", junk=1, inbox=1):
    """Mail in the account's Junk folder and inbox, as Dovecot stores it."""
    maildir = db_config.vmail_root / "example.nl" / address.split("@")[0] / "Maildir"
    for count, folder in ((junk, maildir / ".Junk" / "cur"), (inbox, maildir / "cur")):
        folder.mkdir(parents=True, exist_ok=True)
        for number in range(count):
            (folder / f"{number}.mail:2,S").write_text("Subject: one\n\nbody\n")


def test_spam_learn_teaches_from_junk_and_from_what_people_kept(mailctl, example_domain, db_config, fake_command):
    add_account(mailctl, "info@example.nl")
    junk_and_inbox(db_config, junk=2, inbox=1)
    sa_learn = fake_command("sa-learn", SA_LEARN)

    output = mailctl.ok("spam", "learn")

    assert "Learned from" in output
    spam_call = next(call for call in sa_learn.calls if "--spam" in call)
    ham_call = next(call for call in sa_learn.calls if "--ham" in call)
    assert len(spam_call) == 4 and len(ham_call) == 3  # --no-sync, what to learn, and the messages
    assert ["--sync"] in sa_learn.calls  # what was learned is written at the end, once


def test_spam_learn_only_reads_what_came_in_since_the_last_run(mailctl, example_domain, db_config, fake_command):
    add_account(mailctl, "info@example.nl")
    junk_and_inbox(db_config)
    fake_command("sa-learn", SA_LEARN)
    mailctl.ok("spam", "learn")

    output = mailctl.ok("spam", "learn")

    assert "Nothing new to learn from." in output
    assert db_config.spam_learn_state.exists()


def test_spam_learn_can_read_everything_again(mailctl, example_domain, db_config, fake_command):
    add_account(mailctl, "info@example.nl")
    junk_and_inbox(db_config)
    fake_command("sa-learn", SA_LEARN)
    mailctl.ok("spam", "learn")

    assert "Learned from" in mailctl.ok("spam", "learn", "--all")


def test_spam_learn_can_show_what_it_would_read(mailctl, example_domain, db_config, fake_command):
    add_account(mailctl, "info@example.nl")
    junk_and_inbox(db_config, junk=2, inbox=3)
    sa_learn = fake_command("sa-learn", SA_LEARN)

    output = mailctl.ok("spam", "learn", "--dry-run")

    assert "Would learn from 2 spam messages and 3 other messages" in output
    assert "Nothing was learned (--dry-run)." in output
    assert sa_learn.calls == []


def test_spam_learn_says_when_there_is_no_mail_to_learn_from(mailctl, example_domain, fake_command):
    add_account(mailctl, "info@example.nl")
    fake_command("sa-learn", SA_LEARN)

    assert "There is no mail to learn from." in mailctl.ok("spam", "learn")


def test_the_playbook_teaches_spamassassin_every_night():
    """Bayes only helps when it is fed, and nobody feeds it by hand."""
    tasks = (Path(__file__).resolve().parents[3] / "tasks" / "configure-spamassassin.yml").read_text()

    assert "mailctl-spam-learn.timer" in tasks
    timer = (Path(__file__).resolve().parents[2] / "mailctl-spam-learn.timer").read_text()
    service = (Path(__file__).resolve().parents[2] / "mailctl-spam-learn.service").read_text()
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "mailctl spam learn" in service


def test_filters_show_says_that_only_the_active_one_runs(mailctl, example_domain, fake_command):
    """An account can have several filters, from webmail and from the import, and Dovecot runs one."""
    scripts = {"sogo": "# webmail's own", "roundcube": "# imported"}
    listing = "; ".join(f'echo "{name}{" ACTIVE" if name == "sogo" else ""}"' for name in scripts)
    fake_command("doveadm", f'if [ "$2" = "list" ]; then {listing}; elif [ "$2" = "get" ]; then echo "# filter"; fi')
    add_account(mailctl, "info@example.nl")

    output = mailctl.ok("filters", "show", "info@example.nl")

    assert "Only the active filter runs. The others are kept and do nothing." in output


def test_filters_adopt_puts_the_filters_an_account_has_in_webmail(mailctl, example_domain, fake_command, database):
    """For accounts whose filters were imported before mailctl put them in webmail's list."""
    script = 'require ["fileinto"];\n# rule:[Invoices]\nif header :contains "subject" "invoice" { fileinto "Bills"; }'
    doveadm = fake_command("doveadm", f'if [ "$2" = "list" ]; then echo "roundcube ACTIVE"; '
                                      f'elif [ "$2" = "get" ]; then printf \'{script}\'; fi')
    add_account(mailctl, "info@example.nl")

    output = mailctl.ok("filters", "adopt", "info@example.nl")

    assert "1 rule in webmail's filters" in output
    filters = sogofilters.read(database, "info@example.nl")
    assert [one["name"] for one in filters] == ["Invoices"]
    assert ["sieve", "activate", "-u", "info@example.nl", "sogo"] in [call[:5] for call in doveadm.calls]


def test_filters_adopt_can_show_what_it_would_do(mailctl, example_domain, fake_command, database):
    script = 'if header :contains "subject" "invoice" { fileinto "Bills"; }'
    fake_command("doveadm", f'if [ "$2" = "list" ]; then echo "roundcube ACTIVE"; '
                            f'elif [ "$2" = "get" ]; then printf \'{script}\'; fi')
    add_account(mailctl, "info@example.nl")

    output = mailctl.ok("filters", "adopt", "info@example.nl", "--dry-run")

    assert "would put 1 rule in webmail's filters" in output
    assert "Nothing was changed (--dry-run)." in output
    assert sogofilters.read(database, "info@example.nl") == []


IMPORTED_FILTER = ('require ["fileinto"];\n# rule:[Invoices]\n'
                   'if header :contains "subject" "invoice" { fileinto "Bills"; }\n')


def server_file(tmp_path, filters, accounts=("info@example.nl",), name="mailserver.json"):
    """A file as 'mailctl export' writes it: accounts and forwards as well as filters."""
    path = tmp_path / name
    path.write_text(json.dumps({
        "domains": ["example.nl"],
        "addresses": [{"address": address, "password": PASSWORD} for address in accounts],
        "forwards": [{"source": "sales@example.nl", "destination": "info@example.nl"}],
        "sieve": filters,
    }))
    path.chmod(0o600)
    return path


def a_filter(address="info@example.nl", name="roundcube", active=True, content=IMPORTED_FILTER):
    return {"address": address, "name": name, "active": active, "content": content}


SIEVE_FILTER_OUTPUT = """>> Filtering message:

  ID:      <1@example.nl>
  Subject: Invoice 7

Performed actions:

 * store message in folder: Bills
        + create mailbox if it does not exist

Implicit keep:

  (none)

>> Filtering message:

  ID:      <2@example.nl>
  Subject: Hello

Performed actions:

  (none)

Implicit keep:

 * store message in folder: INBOX

"""
NOTHING_MOVED = """>> Filtering message:

  ID:      <1@example.nl>
  Subject: Hello

Performed actions:

  (none)

Implicit keep:

 * store message in folder: INBOX

"""


def with_an_active_filter(mailctl, fake_command, script="# filter"):
    """An account whose active filter is webmail's, as doveadm reports it."""
    fake_command("doveadm", f'if [ "$2" = "list" ]; then echo "sogo ACTIVE"; '
                            f'elif [ "$2" = "get" ]; then echo "{script}"; fi')
    add_account(mailctl, "info@example.nl")


def test_filters_run_moves_the_mail_that_is_already_there(mailctl, example_domain, fake_command):
    """Filters only sort mail as it arrives; this is for the mail that came in before them."""
    with_an_active_filter(mailctl, fake_command)
    sieve_filter = fake_command("sieve-filter", f"cat <<'REPORT'\n{SIEVE_FILTER_OUTPUT}REPORT")

    output = mailctl.ok("filters", "run", "info@example.nl", "--yes")

    assert "sogo looked at 2 messages in INBOX:" in output
    assert "1 → Bills" in output
    assert "Moved 1 of 2 messages out of INBOX" in output
    assert "-e" in sieve_filter.calls[0]  # without it sieve-filter changes nothing
    assert sieve_filter.calls[0][-1] == "INBOX"


def test_filters_run_can_show_what_it_would_move(mailctl, example_domain, fake_command):
    with_an_active_filter(mailctl, fake_command)
    sieve_filter = fake_command("sieve-filter", f"cat <<'REPORT'\n{SIEVE_FILTER_OUTPUT}REPORT")

    output = mailctl.ok("filters", "run", "info@example.nl", "--dry-run")

    assert "1 → Bills" in output
    assert "1 of 2 would move." in output
    assert "Nothing was moved (--dry-run)." in output
    assert "-e" not in sieve_filter.calls[0]


def test_filters_run_takes_another_folder(mailctl, example_domain, fake_command):
    with_an_active_filter(mailctl, fake_command)
    sieve_filter = fake_command("sieve-filter", f"cat <<'REPORT'\n{SIEVE_FILTER_OUTPUT}REPORT")

    output = mailctl.ok("filters", "run", "info@example.nl", "--folder", "Archive", "--yes")

    assert "messages in Archive:" in output
    assert sieve_filter.calls[0][-1] == "Archive"


def test_filters_run_says_when_nothing_matches(mailctl, example_domain, fake_command):
    with_an_active_filter(mailctl, fake_command)
    fake_command("sieve-filter", f"cat <<'REPORT'\n{NOTHING_MOVED}REPORT")

    output = mailctl.ok("filters", "run", "info@example.nl", "--yes")

    assert "sogo looked at 1 message in INBOX:" in output
    assert "Nothing in INBOX moves: the filters leave it all where it is." in output


KEPT_AND_FILED = """>> Filtering message:

  ID:      <3@example.nl>
  Subject: Out of stock

Performed actions:

 * store message in folder: INBOX
 * store message in folder: Stock

Implicit keep:

  (none)

"""


def test_filters_run_counts_a_rule_that_keeps_the_message_apart(mailctl, example_domain, fake_command):
    """A rule with a keep files the message and leaves it where it is, so it comes by again on the next run."""
    with_an_active_filter(mailctl, fake_command)
    fake_command("sieve-filter", f"cat <<'REPORT'\n{KEPT_AND_FILED}REPORT")

    output = mailctl.ok("filters", "run", "info@example.nl", "--yes")

    assert "1 → Stock" in output
    assert "1 filed back into INBOX, so they stay where they are" in output
    assert "Moved 1 of 1 message out of INBOX" in output


def test_filters_run_needs_a_filter_that_is_active(mailctl, example_domain, fake_command):
    fake_command("doveadm", 'if [ "$2" = "list" ]; then echo "roundcube"; elif [ "$2" = "get" ]; then echo "# f"; fi')
    add_account(mailctl, "info@example.nl")

    output = mailctl.fails("filters", "run", "info@example.nl", "--yes")

    assert "has no active filter, so there is nothing to run." in output
    assert "mailctl filters show info@example.nl" in output


def test_filters_import_takes_only_the_filters_from_a_mail_server_file(mailctl, example_domain, tmp_path, database):
    """The accounts and forwards in the file are 'mailctl import' business; this command leaves them alone."""
    add_account(mailctl, "info@example.nl")
    path = server_file(tmp_path, [a_filter()])

    output = mailctl.ok("filters", "import", str(path), "--yes")

    assert "1 filter of 1 account" in output
    assert [one["name"] for one in sogofilters.read(database, "info@example.nl")] == ["Invoices"]
    assert "in webmail's filters now" in output
    assert "sales@example.nl" not in mailctl.ok("forward", "list")


def test_filters_import_leaves_out_an_account_this_server_doesnt_have(mailctl, example_domain, tmp_path):
    path = server_file(tmp_path, [a_filter("gone@example.nl")], accounts=("gone@example.nl",))

    output = mailctl.ok("filters", "import", str(path), "--yes")

    assert "gone@example.nl isn't an account on this server" in output
    assert "mailctl address add gone@example.nl" in output
    assert "There is nothing to import." in output


def test_filters_import_can_take_one_accounts_filters(mailctl, example_domain, tmp_path, database):
    add_account(mailctl, "info@example.nl")
    add_account(mailctl, "sales@example.nl")
    path = server_file(tmp_path, [a_filter(), a_filter("sales@example.nl")],
                       accounts=("info@example.nl", "sales@example.nl"))

    output = mailctl.ok("filters", "import", str(path), "info@example.nl", "--yes")

    assert "1 filter of 1 account" in output
    assert sogofilters.read(database, "sales@example.nl") == []


def test_filters_import_keeps_a_filter_the_account_already_has(mailctl, example_domain, tmp_path, fake_command):
    fake_command("doveadm", 'if [ "$2" = "list" ]; then echo "roundcube ACTIVE"; '
                            'elif [ "$2" = "get" ]; then echo "# filter"; fi')
    add_account(mailctl, "info@example.nl")
    path = server_file(tmp_path, [a_filter()])

    output = mailctl.ok("filters", "import", str(path), "--yes")

    assert "already has a filter called roundcube, left as it is" in output
    assert "mailctl filters import --replace" in output
    assert "There is nothing to import." in output


def test_filters_import_overwrites_what_is_there_when_asked(mailctl, example_domain, tmp_path, fake_command,
                                                            database):
    fake_command("doveadm", 'if [ "$2" = "list" ]; then echo "roundcube ACTIVE"; '
                            'elif [ "$2" = "get" ]; then echo "# filter"; fi')
    add_account(mailctl, "info@example.nl")
    path = server_file(tmp_path, [a_filter()])

    output = mailctl.ok("filters", "import", str(path), "--replace", "--yes")

    assert "Imported 1 filter of info@example.nl." in output
    assert [one["name"] for one in sogofilters.read(database, "info@example.nl")] == ["Invoices"]


def test_filters_import_can_show_what_it_would_do(mailctl, example_domain, tmp_path, database):
    add_account(mailctl, "info@example.nl")
    path = server_file(tmp_path, [a_filter()])

    output = mailctl.ok("filters", "import", str(path), "--dry-run")

    assert "Would put 1 rule of roundcube in webmail's filters." in output
    assert "Nothing was changed (--dry-run)." in output
    assert sogofilters.read(database, "info@example.nl") == []


def test_filters_import_says_when_the_file_holds_no_filters(mailctl, example_domain, tmp_path):
    path = server_file(tmp_path, [])

    assert "holds no filters" in mailctl.ok("filters", "import", str(path), "--yes")


def test_address_delete_takes_what_webmail_keeps_with_it(mailctl, example_domain, database, typed_passwords, terminal):
    """Calendars, address books and filters live in webmail's database, not in the mail directory."""
    typed_passwords("correct horse battery", "correct horse battery")
    mailctl.ok("address", "add", "info@example.nl")
    database.execute("INSERT INTO sogo.sogo_user_profile (c_uid, c_defaults) VALUES (%s, %s)",
                     "info@example.nl", '{"SOGoSieveFilters": []}')
    database.execute("INSERT INTO sogo.sogo_folder_info (c_path, c_path1, c_path2, c_foldername, c_folder_type) "
                     "VALUES (%s, 'Users', %s, 'personal', 'Appointment')",
                     "/Users/info@example.nl/Calendar/personal", "info@example.nl")

    output = mailctl.ok("address", "delete", "info@example.nl", "--yes", "--delete-mail")

    assert "Webmail lost 1 calendar or address book and its webmail settings and filters too." in output
    assert database.value("SELECT COUNT(*) FROM sogo.sogo_user_profile WHERE c_uid = %s", "info@example.nl") == 0
    assert database.value("SELECT COUNT(*) FROM sogo.sogo_folder_info WHERE c_path2 = %s", "info@example.nl") == 0


def test_address_delete_that_keeps_the_mail_keeps_webmails_data_too(mailctl, example_domain, database,
                                                                   typed_passwords, terminal):
    typed_passwords("correct horse battery", "correct horse battery")
    mailctl.ok("address", "add", "info@example.nl")
    database.execute("INSERT INTO sogo.sogo_user_profile (c_uid, c_defaults) VALUES (%s, %s)",
                     "info@example.nl", '{"SOGoSieveFilters": []}')

    mailctl.ok("address", "delete", "info@example.nl", "--yes", "--keep-mail")

    assert database.value("SELECT COUNT(*) FROM sogo.sogo_user_profile WHERE c_uid = %s", "info@example.nl") == 1
