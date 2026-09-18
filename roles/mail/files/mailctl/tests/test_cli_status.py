import re
from datetime import datetime, timedelta
from ipaddress import ip_address

import pytest
from conftest import FAKE_KEY_RECORD_START, PASSWORD

from mailctl.core import dns_check, system
from mailctl.core.errors import MailctlError

DOVEADM = r"""
case "$1 $2" in
  "-f tab") printf 'mailbox\tmessages\tvsize\nINBOX\t12\t34567\nSent\t3\t4000\nSent Items\t3\t4000\n' ;;
  "sieve list") echo "roundcube ACTIVE" ;;
  "sieve get") echo 'require ["fileinto"];' ;;
esac
"""


@pytest.fixture
def account(mailctl, fake_command):
    fake_command("doveadm", DOVEADM)
    mailctl.ok("domain", "add", "example.nl")
    mailctl.ok("address", "add", "info@example.nl", "--password-stdin", stdin=f"{PASSWORD}\n")
    return "info@example.nl"


def log_line(minutes_ago, program, message):
    when = datetime.now().astimezone() - timedelta(minutes=minutes_ago)
    return f"{when.isoformat()} mail {program}[1]: {message}\n"


class FakeDns:
    """example.nl's mail goes to this server and its SPF allows it, but it has no DKIM record yet."""

    def txt(self, name):
        return {"example.nl": ["v=spf1 mx ~all"], "_dmarc.example.nl": ["v=DMARC1; p=reject"]}.get(name, [])

    def mx(self, name):
        return ["mail.example.nl"] if name == "example.nl" else []

    def addresses(self, name):
        return {ip_address("203.0.113.5")} if name == "mail.example.nl" else set()

    def srv(self, name):
        return []

    def ptr(self, address):
        return []


def test_status_of_an_account(mailctl, account, db_config, database):
    db_config.mail_logs[1].write_text(
        log_line(30, "postfix/submission/smtpd",
                 "A1: client=laptop[198.51.100.3], sasl_method=PLAIN, sasl_username=info@example.nl")
        + log_line(30, "postfix/qmgr", "A1: from=<info@example.nl>, size=100, nrcpt=2 (queue active)")
        + log_line(29, "postfix/smtp",
                   "A1: to=<gone@gmail.com>, relay=mx[192.0.2.1]:25, delay=1, delays=0/0/0/1, dsn=5.1.1,"
                   " status=bounced (no such user)")
    )
    database.execute("INSERT INTO last_login VALUES (%s, 'imap', UNIX_TIMESTAMP() - 7200, '198.51.100.3')", account)

    output = mailctl.ok("status", account)

    for expected in ("INBOX", "33.8 KB", "Sent Items → Sent", "imap", "198.51.100.3", "2 hours ago",
                     "2 of 300", "2 of 1000", "gone@gmail.com", "no such user"):
        assert expected in output
    # The total leaves out Sent Items, which is the Sent folder under another name.
    assert re.search(r"Total\s+15\s+37\.7 KB", output)


def test_status_of_an_account_says_when_there_is_no_mail_log(mailctl, account):
    assert "There is no mail log to count from" in mailctl.ok("status", account)


def test_status_of_an_account_shows_the_other_parts_when_dovecot_fails(mailctl, account, fake_command):
    fake_command("doveadm", "echo 'Error: Connection refused' >&2; exit 75")

    output = mailctl.ok("status", account)

    assert "Connection refused" in output
    assert "Last login" in output
    assert "Bounces in the last week" in output


def test_status_of_an_account_warns_about_an_unreadable_log(mailctl, account, db_config):
    log = db_config.mail_logs[1]
    log.write_text("")
    log.chmod(0)

    assert f"Can't read {log}" in mailctl.ok("status", account)


def test_status_of_an_unknown_account(mailctl, account):
    assert "isn't an account on this server" in mailctl.fails("status", "sales@example.nl")


def test_status_of_a_domain_checks_its_dns(mailctl, account, monkeypatch):
    monkeypatch.setattr(dns_check, "SystemResolver", FakeDns)
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address("203.0.113.5")})

    output = mailctl.ok("status", "example.nl")

    assert "Mail is delivered to mail.example.nl" in output
    assert "There is no DKIM record at mail._domainkey.example.nl" in output
    assert FAKE_KEY_RECORD_START in output
    assert "Policy: reject" in output
    assert "1 address" in output


def test_status_of_a_domain_shows_every_record_mail_delivery_needs(mailctl, account, monkeypatch):
    class FakeDnsWithoutMx(FakeDns):
        def mx(self, name):
            return []

    monkeypatch.setattr(dns_check, "SystemResolver", FakeDnsWithoutMx)
    monkeypatch.setattr(system, "server_ips", lambda: {ip_address("203.0.113.5")})

    output = mailctl.ok("status", "example.nl")

    assert "There is no MX record" in output
    assert re.search(r"MX\s+example\.nl\n\s+10 mail\.example\.nl\.\n", output)
    assert re.search(r"A\s+mail\.example\.nl\n\s+203\.0\.113\.5\n", output)


def test_status_of_a_domain_shows_the_rest_when_the_dns_checks_fail(mailctl, account, monkeypatch):
    def no_addresses():
        raise MailctlError("ip isn't installed.")

    monkeypatch.setattr(system, "server_ips", no_addresses)

    output = mailctl.ok("status", "example.nl")

    assert "ip isn't installed" in output
    assert "1 address" in output


def test_spam_settings_can_be_set_shown_and_unset(mailctl, account):
    assert "required_score is now 4 for domain example.nl" in mailctl.ok(
        "spam", "set", "example.nl", "required_score", "4"
    )
    assert "Added *@partner.nl to welcomelist_from for info@example.nl" in mailctl.ok(
        "spam", "set", account, "welcomelist_from", "*@Partner.nl"
    )

    output = mailctl.ok("spam", "show", account)
    assert "required_score" in output
    assert "domain example.nl" in output
    assert "*@partner.nl" in output

    assert "Removed *@partner.nl from welcomelist_from for info@example.nl" in mailctl.ok(
        "spam", "unset", account, "welcomelist_from", "*@partner.nl"
    )
    assert "*@partner.nl" not in mailctl.ok("spam", "show", account)


def test_spam_set_for_the_whole_server_and_setting_names_in_any_case(mailctl):
    assert "required_score is now 5 for the whole server" in mailctl.ok("spam", "set", "server", "Required_Score", "5")


def test_spam_set_says_when_nothing_changed(mailctl, account):
    mailctl.ok("spam", "set", "example.nl", "welcomelist_from", "*@partner.nl")

    output = mailctl.ok("spam", "set", "example.nl", "welcomelist_from", "*@Partner.nl")

    assert "*@partner.nl is already in welcomelist_from for domain example.nl" in output


def test_spam_show_without_settings(mailctl, account):
    assert "No spam settings" in mailctl.ok("spam", "show", account)


def test_spam_unset_of_a_setting_that_isnt_set(mailctl, account):
    assert "required_score isn't set for info@example.nl" in mailctl.ok("spam", "unset", account, "required_score")


def test_spam_unset_of_a_whole_list_needs_confirmation(mailctl, account):
    mailctl.ok("spam", "set", account, "blocklist_from", "*@spam.example")
    mailctl.ok("spam", "set", account, "blocklist_from", "*@junk.example")

    assert "--yes" in mailctl.fails("spam", "unset", account, "blocklist_from")
    assert "Removed blocklist_from for info@example.nl" in mailctl.ok("spam", "unset", account, "blocklist_from", "--yes")


def test_spam_set_refuses_an_unknown_setting_before_asking_for_a_value(mailctl, account):
    assert "Unknown spam setting" in mailctl.fails("spam", "set", "example.nl", "rewrite_header")


def test_spam_refuses_a_domain_that_isnt_on_this_server(mailctl, account):
    assert "other.nl isn't a domain on this server" in mailctl.fails("spam", "show", "other.nl")


def test_filters_show_the_accounts_scripts_and_the_server_scripts(mailctl, account, db_config):
    db_config.sieve_after.mkdir()
    (db_config.sieve_after / "spam-to-folder.sieve").write_text('if header :contains "X-Spam-Flag" "YES" { stop; }\n')

    output = mailctl.ok("filters", "show", account)

    for expected in ("roundcube", "active", 'require ["fileinto"];', "spam-to-folder", "X-Spam-Flag"):
        assert expected in output
