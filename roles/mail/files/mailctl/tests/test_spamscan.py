import pytest

from mailctl.core import spamscan
from mailctl.core.dns_check import Status
from mailctl.core.errors import MailctlError

MILTER = "unix:/var/spool/postfix/spamass/spamass.sock"
SCANNED = ("X-Spam-Checker-Version: SpamAssassin 4.0.0\n"
           "X-Spam-Flag: YES\n"
           "X-Spam-Status: Yes, score=1000.0 required=5.0 tests=GTUBE\n\nthe test message\n")
# What spamc hands back when spamd doesn't answer: the message, exactly as it was given.
UNSCANNED = spamscan.TEST_MESSAGE


def answering(text):
    return lambda: text


def refusing(message):
    def look():
        raise MailctlError(message)

    return look


def test_a_server_that_scores_the_test_message_is_fine():
    check = spamscan.check(scanner=answering(SCANNED), wiring=answering(MILTER))

    assert check.status is Status.OK
    assert "SpamAssassin scores the mail that comes in" in check.detail


def test_a_message_that_comes_back_as_it_went_in_is_a_problem():
    """The failure this check is for: spamass-milter lets a message through when spamd can't score it."""
    check = spamscan.check(scanner=answering(UNSCANNED), wiring=answering(MILTER))

    assert check.status is Status.FAIL
    assert "mail arrives unmarked and nothing is filtered" in check.detail
    assert "journalctl -u spamd" in check.detail


def test_a_message_that_was_scored_but_not_marked_is_a_warning():
    scored = SCANNED.replace("X-Spam-Flag: YES\n", "").replace("Yes, score", "No, score")

    check = spamscan.check(scanner=answering(scored), wiring=answering(MILTER))

    assert check.status is Status.WARN
    assert "X-Spam-Status: No, score=1000.0" in check.detail
    assert "mailctl spam show" in check.detail


def test_postfix_that_hands_mail_to_no_spam_filter_is_a_problem():
    check = spamscan.check(scanner=answering(SCANNED), wiring=answering("unix:/var/spool/postfix/opendkim/sock"))

    assert check.status is Status.FAIL
    assert "smtpd_milters has no spamass-milter socket" in check.detail


def test_spamc_that_isnt_there_is_a_problem():
    check = spamscan.check(scanner=refusing("spamc isn't installed."), wiring=answering(MILTER))

    assert check.status is Status.FAIL
    assert "spamc isn't installed." in check.detail


def test_postfix_that_cant_be_asked_leaves_the_scan_to_say_it():
    """A server without postconf still has an answer: what came back from the scan."""
    check = spamscan.check(scanner=answering(SCANNED), wiring=refusing("postconf isn't installed."))

    assert check.status is Status.OK


@pytest.mark.parametrize("command", ["spamc", "postconf"])
def test_the_real_steps_say_which_command_is_missing(command, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(MailctlError, match=f"{command} isn't installed."):
        spamscan.scan() if command == "spamc" else spamscan.milters()


def test_the_test_message_is_the_one_every_spamassassin_marks():
    assert "GTUBE-STANDARD-ANTI-UBE-TEST-EMAIL" in spamscan.TEST_MESSAGE
