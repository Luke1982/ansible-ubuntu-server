import getopt
import io
import json
import os
import pwd
import smtplib
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from mailctl import sitesendmail

# The password file's entries, as far as site-sendmail reads them. Not pwd.struct_passwd: Python 3.12 complains
# about hand-made ones when it exits.
User = namedtuple("User", "pw_name pw_uid pw_dir")
SITE_HOME = Path("/home/garagerenard")
USERS = [
    User("root", 0, "/root"),
    User("www-data", 33, "/var/www"),
    User("garagerenard", 5019, str(SITE_HOME)),
    User("shop", 5030, "/home/garagerenard/shop"),
    User("nobody", 65534, "/"),
]

# As PHP's mail() writes it: bare line ends, the To and Subject headers first.
PHP_MAIL = (
    b"To: Klant <klant@gmail.com>, other@example.nl\n"
    b"Subject: Your order\n"
    b"Cc: copy@example.nl\n"
    b"Bcc: hidden@example.nl,\n"
    b"  second-hidden@example.nl\n"
    b"From: WordPress <wordpress@garagerenard.nl>\n"
    b"\n"
    b"Thank you for your order.\n"
    b"Bcc: this line is the body\n"
)


class FakeSmtp:
    """Postfix as smtplib talks to it. refuse maps recipients to the reply they get."""

    refuse: dict[str, tuple[int, bytes]] = {}
    unreachable = False
    sent: list[dict] = []

    def __init__(self, host, port, local_hostname, timeout):
        if FakeSmtp.unreachable:
            raise ConnectionRefusedError(111, "Connection refused")
        self.connection = {"server": (host, port), "helo": local_hostname}

    def __enter__(self):
        return self

    def __exit__(self, *problem):
        return False

    def sendmail(self, sender, recipients, message):
        refused = {recipient: FakeSmtp.refuse[recipient] for recipient in recipients if recipient in FakeSmtp.refuse}
        if len(refused) == len(recipients):
            raise smtplib.SMTPRecipientsRefused(refused)
        FakeSmtp.sent.append({**self.connection, "sender": sender, "recipients": recipients, "message": message})
        return refused


@pytest.fixture
def postfix(monkeypatch, tmp_path):
    """site-sendmail on server.hosting.example, run by PHP for a script of garagerenard's site."""
    FakeSmtp.refuse, FakeSmtp.unreachable, FakeSmtp.sent = {}, False, []
    monkeypatch.setattr(sitesendmail.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(sitesendmail.pwd, "getpwall", lambda: USERS)
    monkeypatch.setattr(sitesendmail, "_working_directory", lambda: SITE_HOME / "public_html" / "www")
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"hostname": "server.hosting.example"}))
    monkeypatch.setenv("MAILCTL_CONFIG", str(config_file))
    return FakeSmtp


def run(monkeypatch, *args: str, message: bytes = PHP_MAIL) -> int:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(message)))
    return sitesendmail.main(list(args))


def test_php_mail_goes_to_postfix_in_the_sites_name(postfix, monkeypatch):
    assert run(monkeypatch, "-t", "-i") == 0

    [sent] = postfix.sent
    assert sent["server"] == ("127.0.0.1", 25)
    assert sent["helo"] == "garagerenard.server.hosting.example"
    assert sent["sender"] == "garagerenard@server.hosting.example"
    assert sent["recipients"] == [
        "klant@gmail.com", "other@example.nl", "copy@example.nl", "hidden@example.nl", "second-hidden@example.nl",
    ]


def test_the_message_goes_without_its_bcc_header_and_with_smtp_line_ends(postfix, monkeypatch):
    run(monkeypatch, "-t", "-i")

    message = postfix.sent[0]["message"]
    assert b"hidden@example.nl" not in message
    assert message.startswith(b"To: Klant <klant@gmail.com>, other@example.nl\r\nSubject: Your order\r\nCc: ")
    assert message.endswith(b"\r\n\r\nThank you for your order.\r\nBcc: this line is the body\r\n")
    assert b"\n" not in message.replace(b"\r\n", b"")


def test_the_sender_a_script_names_is_kept(postfix, monkeypatch):
    """PHPMailer passes it stuck to -f; others pass it as the next argument."""
    run(monkeypatch, "-oi", "-fbounces@garagerenard.nl", "-t")
    run(monkeypatch, "-r", "other@garagerenard.nl", "-t")

    assert [sent["sender"] for sent in postfix.sent] == ["bounces@garagerenard.nl", "other@garagerenard.nl"]
    assert {sent["helo"] for sent in postfix.sent} == {"garagerenard.server.hosting.example"}


def test_recipients_on_the_command_line_go_without_reading_the_headers(postfix, monkeypatch):
    run(monkeypatch, "-i", "--", "only@example.nl")

    assert postfix.sent[0]["recipients"] == ["only@example.nl"]
    assert b"Bcc: hidden@example.nl" in postfix.sent[0]["message"]


def test_a_refused_recipient_is_reported_and_php_hears_it_failed(postfix, monkeypatch, capsys):
    """Not EX_TEMPFAIL: PHP's mail() takes that for sent."""
    postfix.refuse = {"copy@example.nl": (450, b"4.7.1 Sending limit reached for this website")}

    status = run(monkeypatch, "-t", "-i")

    assert status == sitesendmail.EX_UNAVAILABLE != 75
    assert "copy@example.nl: 450 4.7.1 Sending limit reached for this website" in capsys.readouterr().err
    assert len(postfix.sent) == 1  # the others went


def test_mail_that_every_recipient_is_refused_for_fails(postfix, monkeypatch, capsys):
    postfix.refuse = {address: (450, b"4.7.1 Sending limit reached") for address in ("only@example.nl",)}

    assert run(monkeypatch, "only@example.nl") == sitesendmail.EX_UNAVAILABLE
    assert "only@example.nl: 450" in capsys.readouterr().err
    assert postfix.sent == []


def test_postfix_out_of_reach_fails(postfix, monkeypatch, capsys):
    postfix.unreachable = True

    assert run(monkeypatch, "-t") == sitesendmail.EX_UNAVAILABLE
    assert "can't hand the message to Postfix" in capsys.readouterr().err


def test_mail_without_recipients_fails(postfix, monkeypatch, capsys):
    assert run(monkeypatch, "-t", message=b"Subject: nobody to send to\n\nhello\n") == sitesendmail.EX_USAGE
    assert "no recipients" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["-bs"], ["-q"], ["-I"], ["--version"], ["-Z"]])
def test_what_isnt_sending_mail_is_refused(postfix, monkeypatch, args):
    assert run(monkeypatch, *args, "x@example.nl") == sitesendmail.EX_USAGE
    assert postfix.sent == []


def test_options_that_take_a_value_dont_make_it_a_recipient():
    assert sitesendmail.parse(["-N", "never", "-F", "Garage Renard", "-oi", "-odb", "-t"]) == ("", [], True)


def test_the_site_is_the_user_whose_home_holds_the_folder(monkeypatch):
    monkeypatch.setattr(sitesendmail.pwd, "getpwall", lambda: USERS)

    assert sitesendmail.site(SITE_HOME / "public_html" / "www" / "wp-admin") == "garagerenard"
    assert sitesendmail.site(SITE_HOME) == "garagerenard"
    assert sitesendmail.site(SITE_HOME / "shop" / "www") == "shop"  # the nearest home


def test_outside_every_site_it_is_the_user_this_runs_as(monkeypatch):
    """System users' homes, like www-data's /var/www, and nobody's / aren't sites."""
    monkeypatch.setattr(sitesendmail.pwd, "getpwall", lambda: USERS)
    myself = pwd.getpwuid(os.getuid()).pw_name

    assert sitesendmail.site(Path("/var/www/mailautodiscover")) == myself
    assert sitesendmail.site(Path("/tmp")) == myself


def test_a_header_that_names_nobody_adds_no_recipient():
    message = b"To: undisclosed-recipients:;\r\nCc: real@example.nl\r\n\r\nhello\r\n"

    assert sitesendmail.header_recipients(message) == ["real@example.nl"]


def test_an_unknown_option_is_a_usage_error():
    with pytest.raises(getopt.GetoptError):
        sitesendmail.parse(["--unknown"])
