"""site-sendmail: the sendmail of the websites' PHP (sendmail_path in its php.ini, tasks/configure-website-mail.yml).

Every site runs PHP as the same user, so Postfix can't tell by the user which site a message comes from. PHP does run
a script in the site's own folder, inside the home directory of the site's user, and that user is the site. This hands
the message to Postfix over SMTP from 127.0.0.1, greeting it with the site's name, and postfwd counts the website
sending limits by that name (templates/postfwd.cf.j2). The sendmail command itself is closed to the websites' user,
so a script can't go around this.
"""

import getopt
import os
import pwd
import re
import smtplib
import sys
from email.utils import getaddresses
from pathlib import Path

from .core import config
from .core.errors import MailctlError

SERVER = ("127.0.0.1", 25)
TIMEOUT = 60

# sendmail's exit statuses (sysexits.h). Never EX_TEMPFAIL: PHP's mail() takes that for "queued for later" and
# reports the mail as sent, while nothing here keeps it.
EX_USAGE = 64
EX_UNAVAILABLE = 69

# The options of Postfix's sendmail, so none of them is taken for a recipient. Those followed by ":" take a value.
# Only -t and -f (or -r) change anything here: the others are about queues, aliases and delivery reports.
OPTIONS = "A:B:C:F:GIL:N:O:R:UV:X:b:ce:f:h:imno:p:q:r:tvx"

# Below the system's own users, and nobody, no user is a site.
FIRST_SITE_UID = 1000
NOBODY_UID = 65534

_BLANK_LINE = re.compile(rb"\r\n\r\n")
_HEADER_START = re.compile(rb"(?m)^(?=[^ \t\r\n])")


class Usage(Exception):
    """A command line this sendmail doesn't do."""


def main(args: list[str]) -> int:
    try:
        sender, recipients_given, from_headers = parse(args)
    except (getopt.GetoptError, Usage) as problem:
        return _fail(str(problem), EX_USAGE)
    message = _crlf(sys.stdin.buffer.read())
    recipients = list(recipients_given)
    if from_headers:
        recipients += header_recipients(message)
        message = without_bcc(message)
    if not recipients:
        return _fail("no recipients", EX_USAGE)
    try:
        hostname = config.load().hostname
    except MailctlError as problem:
        return _fail(problem.message, EX_UNAVAILABLE)
    name = site(_working_directory())
    try:
        refused = submit(f"{name}.{hostname}", sender or f"{name}@{hostname}", recipients, message)
    except smtplib.SMTPResponseException as problem:
        return _fail(f"{problem.smtp_code} {_text(problem.smtp_error)}", EX_UNAVAILABLE)
    except (OSError, smtplib.SMTPException) as problem:
        return _fail(f"can't hand the message to Postfix: {problem}", EX_UNAVAILABLE)
    for recipient, (code, reason) in refused.items():
        _fail(f"{recipient}: {code} {_text(reason)}", EX_UNAVAILABLE)
    return EX_UNAVAILABLE if refused else 0


def parse(args: list[str]) -> tuple[str, list[str], bool]:
    """The envelope sender ("" for the default), the recipients named, and whether to read them from the headers."""
    options, recipients = getopt.gnu_getopt(args, OPTIONS)
    sender, from_headers = "", False
    for option, value in options:
        if option in ("-f", "-r"):
            sender = value
        elif option == "-t":
            from_headers = True
        elif option == "-b" and value != "m" or option in ("-q", "-I"):
            raise Usage(f"{option}{value} isn't something this sendmail does: it only sends mail")
    return sender, recipients, from_headers


def header_recipients(message: bytes) -> list[str]:
    """The addresses in the To, Cc and Bcc headers, as sendmail -t reads them."""
    values = [value for name, value in _headers(message) if name.lower() in (b"to", b"cc", b"bcc")]
    unfolded = [re.sub(rb"\r\n[ \t]", b" ", value).decode(errors="replace") for value in values]
    return [address for _, address in getaddresses(unfolded) if address]


def without_bcc(message: bytes) -> bytes:
    """The message without its Bcc headers, which sendmail -t keeps from the recipients."""
    head, body = _split(message)
    kept = [field for field in _HEADER_START.split(head) if not field.lower().startswith(b"bcc:")]
    return b"".join(kept) + body


def site(directory: Path) -> str:
    """The site a script in this folder belongs to: the user whose home directory holds it. Outside every site, the
    user this runs as."""
    homes = [
        (Path(user.pw_dir), user.pw_name)
        for user in pwd.getpwall()
        if user.pw_uid >= FIRST_SITE_UID and user.pw_uid != NOBODY_UID and user.pw_dir not in ("", "/")
    ]
    inside = [(len(home.parts), name) for home, name in homes if directory == home or home in directory.parents]
    if inside:
        return max(inside)[1]
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return "unknown"


def submit(helo: str, sender: str, recipients: list[str], message: bytes) -> dict[str, tuple[int, bytes]]:
    """Hands the message to Postfix. Returns the recipients it refused while taking the others."""
    with smtplib.SMTP(*SERVER, local_hostname=helo, timeout=TIMEOUT) as smtp:
        try:
            return smtp.sendmail(sender, recipients, message)
        except smtplib.SMTPRecipientsRefused as problem:
            return problem.recipients


def _headers(message: bytes) -> list[tuple[bytes, bytes]]:
    head, _ = _split(message)
    fields = [field for field in _HEADER_START.split(head) if b":" in field]
    return [(name.strip(), value.strip()) for name, value in (field.split(b":", 1) for field in fields)]


def _split(message: bytes) -> tuple[bytes, bytes]:
    """The header lines, each ending in its line break, and the rest: the blank line and the body."""
    end = _BLANK_LINE.search(message)
    if end is None:
        return message, b""
    return message[:end.start() + 2], message[end.start() + 2:]


def _crlf(message: bytes) -> bytes:
    """PHP writes lines that end in a bare \\n; SMTP wants \\r\\n."""
    return re.sub(rb"\r?\n", b"\r\n", message)


def _working_directory() -> Path:
    try:
        return Path(os.getcwd())
    except OSError:  # the folder was removed while the script ran
        return Path("/")


def _text(reply: bytes | str) -> str:
    return reply.decode(errors="replace") if isinstance(reply, bytes) else reply


def _fail(problem: str, status: int) -> int:
    print(f"site-sendmail: {problem}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
