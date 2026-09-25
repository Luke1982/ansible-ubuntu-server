"""Whether SpamAssassin really looks at the mail that comes in.

Postfix hands every message to spamass-milter, which asks spamd for a score and lets the message through untouched
when spamd doesn't answer. A spamd that starts but can't do its work — without the Perl driver for the settings it
keeps in MariaDB, for instance — therefore breaks nothing anybody notices: mail keeps arriving, unmarked and
unfiltered, and only the missing X-Spam headers of a message already delivered show it. This puts a test message
down the same road and looks at what comes back.
"""

import re
from collections.abc import Callable

from . import system
from .dns_check import Check, Status
from .errors import MailctlError

NAME = "Spam filter"
# The body every SpamAssassin scores 1000 by definition, so this doesn't depend on a rule, a setting or a lookup.
GTUBE = "XJS*C4JDBQADN1.NSBN3*2IDNEN*GTUBE-STANDARD-ANTI-UBE-TEST-EMAIL*C.34X"
TEST_MESSAGE = (
    "From: spam-check@example.com\n"
    "To: postmaster@localhost\n"
    "Subject: mailctl doctor test message\n"
    "\n"
    f"{GTUBE}\n"
)
_STATUS = re.compile(r"^X-Spam-Status:[^\n]*", re.MULTILINE)


def scan(message: str = TEST_MESSAGE) -> str:
    """The message as SpamAssassin gives it back, with the headers it adds. spamc hands back what it was given
    when it can't reach spamd, which is exactly the case this check is about."""
    return system.run_answer("spamc", stdin=message)


def milters() -> str:
    """What Postfix hands an incoming message to."""
    return system.run("postconf", "-h", "smtpd_milters").strip()


def check(scanner: Callable[[], str] | None = None, wiring: Callable[[], str] | None = None) -> Check:
    """Whether a test message comes back scored. The two steps are given in for the tests, and looked up here so
    nothing runs when the check isn't made."""
    handed_to = _attempt(wiring or milters)
    if handed_to is not None and "spamass" not in handed_to:
        return Check(NAME, Status.FAIL,
                     "Postfix hands incoming mail to no spam filter: smtpd_milters has no spamass-milter socket, "
                     "so nothing that arrives is looked at. Run the Ansible playbook to set it up.")
    try:
        answer = (scanner or scan)()
    except MailctlError as problem:
        return Check(NAME, Status.FAIL, f"{problem.message} Mail arrives unscanned while it isn't there.")
    if "X-Spam-Flag: YES" in answer:
        return Check(NAME, Status.OK, "SpamAssassin scores the mail that comes in.")
    if _STATUS.search(answer):
        return Check(NAME, Status.WARN,
                     f"SpamAssassin scored a test message that every server marks as spam, and didn't mark it: "
                     f"{_STATUS.search(answer).group()}. See what it goes by with: mailctl spam show")
    return Check(NAME, Status.FAIL,
                 "SpamAssassin didn't score a test message, so mail arrives unmarked and nothing is filtered: "
                 "spamass-milter lets a message through when spamd can't answer. See why with: "
                 "journalctl -u spamd -n 20")


def _attempt(look: Callable[[], str]) -> str | None:
    """What it found, or None when it couldn't look: the scan itself says enough about a server without Postfix."""
    try:
        return look()
    except MailctlError:
        return None
