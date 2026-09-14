import base64
import subprocess

import pytest

from mailctl.core import dkim
from mailctl.core.errors import MailctlError


@pytest.fixture
def systemctl(fake_command):
    return fake_command("systemctl")


def test_create_key_makes_a_private_2048_bit_key_once(config, fake_opendkim_genkey):
    assert dkim.create_key(config, "example.nl")

    directory = config.dkim_keys / "example.nl"
    assert (directory / "mail.private").stat().st_mode & 0o777 == 0o600
    assert fake_opendkim_genkey.calls == [["-b", "2048", "-s", "mail", "-d", "example.nl", "-D", str(directory)]]

    assert not dkim.create_key(config, "example.nl")
    assert len(fake_opendkim_genkey.calls) == 1


def test_key_domains_lists_the_domains_that_have_a_key(config, fake_opendkim_genkey):
    dkim.create_key(config, "b.nl")
    dkim.create_key(config, "a.nl")
    (config.dkim_keys / "c.nl").mkdir()
    (config.dkim_keys / "old keys").mkdir()
    (config.dkim_keys / "old keys" / "mail.private").write_text("key")

    assert dkim.key_domains(config) == ["a.nl", "b.nl"]
    assert dkim.has_key(config, "a.nl")
    assert not dkim.has_key(config, "c.nl")


def test_key_domains_without_a_key_directory(config):
    assert dkim.key_domains(config) == []


def test_a_key_folder_with_capitals_from_the_old_script_is_used(config, fake_opendkim_genkey):
    folder = config.dkim_keys / "Example.nl"
    folder.mkdir(parents=True)
    (folder / "mail.private").write_text("key")

    assert dkim.key_domains(config) == ["example.nl"]
    assert not dkim.create_key(config, "example.nl")
    dkim.write_tables(config)
    assert config.dkim_key_table.read_text() == f"mail._domainkey.example.nl example.nl:mail:{folder}/mail.private\n"


def test_write_tables_lists_every_domain_that_has_a_key_and_says_whether_they_changed(config, fake_opendkim_genkey):
    dkim.create_key(config, "b.nl")
    dkim.create_key(config, "a.nl")

    assert dkim.write_tables(config)
    assert not dkim.write_tables(config)

    keys = config.dkim_keys
    assert config.dkim_key_table.read_text() == (
        f"mail._domainkey.a.nl a.nl:mail:{keys}/a.nl/mail.private\n"
        f"mail._domainkey.b.nl b.nl:mail:{keys}/b.nl/mail.private\n"
    )
    assert config.dkim_signing_table.read_text() == "*@a.nl mail._domainkey.a.nl\n*@b.nl mail._domainkey.b.nl\n"
    assert config.dkim_key_table.stat().st_mode & 0o777 == 0o644


def test_write_tables_without_keys_empties_the_tables(config):
    config.dkim_key_table.parent.mkdir(parents=True)
    config.dkim_key_table.write_text("mail._domainkey.old.nl old.nl:mail:/gone\n")

    assert dkim.write_tables(config)

    assert config.dkim_key_table.read_text() == ""
    assert config.dkim_signing_table.read_text() == ""


def test_update_opendkim_restores_domains_the_old_playbook_wiped_and_reloads_only_on_change(
    config, fake_opendkim_genkey, systemctl
):
    dkim.create_key(config, "a.nl")
    dkim.create_key(config, "b.nl")
    config.dkim_signing_table.write_text("*@a.nl mail._domainkey.a.nl\n")

    assert dkim.update_opendkim(config)
    assert not dkim.update_opendkim(config)

    assert config.dkim_signing_table.read_text() == "*@a.nl mail._domainkey.a.nl\n*@b.nl mail._domainkey.b.nl\n"
    assert systemctl.calls == [["reload-or-restart", "opendkim"]]


def test_update_opendkim_puts_the_old_tables_back_when_opendkim_cant_reload(
    config, fake_opendkim_genkey, fake_command, systemctl
):
    dkim.create_key(config, "a.nl")
    dkim.update_opendkim(config)
    dkim.create_key(config, "b.nl")
    fake_command("systemctl", "echo 'Job for opendkim.service failed' >&2; exit 1")

    with pytest.raises(MailctlError, match="opendkim.service failed"):
        dkim.update_opendkim(config)
    assert "b.nl" not in config.dkim_signing_table.read_text()

    fake_command("systemctl")
    assert dkim.update_opendkim(config)
    assert "*@b.nl" in config.dkim_signing_table.read_text()


def test_delete_key_removes_the_domains_key_directory(config, fake_opendkim_genkey):
    dkim.create_key(config, "example.nl")

    dkim.delete_key(config, "example.nl")
    dkim.delete_key(config, "example.nl")

    assert not (config.dkim_keys / "example.nl").exists()


def test_record_name():
    assert dkim.record_name("example.nl") == "mail._domainkey.example.nl"


def test_record_value_is_made_from_the_private_key_alone(config, fake_opendkim_genkey):
    dkim.create_key(config, "example.nl")
    private_key = config.dkim_keys / "example.nl" / "mail.private"
    public_key = subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout", "-outform", "DER"], capture_output=True, check=True
    ).stdout

    assert dkim.record_value(config, "example.nl") == f"v=DKIM1; h=sha256; k=rsa; p={base64.b64encode(public_key).decode()}"


def test_record_value_reports_a_domain_without_a_key(config):
    with pytest.raises(MailctlError, match="example.nl has no DKIM key"):
        dkim.record_value(config, "example.nl")
