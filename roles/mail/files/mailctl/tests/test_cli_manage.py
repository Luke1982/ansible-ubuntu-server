import base64
import re
import subprocess
from pathlib import Path

import pytest
from conftest import FAKE_KEY_RECORD_START, PASSWORD, Mailctl

from mailctl import ui
from mailctl.core import system
from mailctl.core.errors import MailctlError

COMMANDS = [
    ("domain", "add"), ("domain", "list"), ("domain", "delete"),
    ("address", "add"), ("address", "list"), ("address", "password"), ("address", "delete"),
    ("forward", "add"), ("forward", "list"), ("forward", "delete"),
    ("dkim", "show"), ("dkim", "create"),
    ("dns", "show"), ("dns", "publish"), ("dns", "credentials"), ("autodiscover", "publish"),
    ("status",), ("doctor",),
    ("spam", "show"), ("spam", "set"), ("spam", "unset"),
    ("filters", "show"),
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
    assert re.search(r"MX\s+example\.nl\n\s+10 mail\.example\.nl\n", output)
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
