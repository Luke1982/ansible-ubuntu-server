"""Account activity: logins recorded by Dovecot, and sending and bounces from Postfix's mail log.

The sending numbers come close to what postfwd counts, but aren't the same: the log gives each message's
recipients after forwards are expanded, while postfwd counts the recipients a mail client names.
"""

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import SendLimit
from .db import Database

BOUNCE_PERIOD = timedelta(days=7)
MAX_BOUNCES = 5
REFUSED_PERIOD = timedelta(days=1)
WINDOWS_WITHOUT_LIMITS = (3600, 86400)

_LINE = re.compile(r"(?P<when>\d{4}-\d\d-\d\dT\S+|[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) \S+ [^:\s]+: (?P<message>.*)")
_CLIENT = re.compile(r"(?P<queue_id>\w+): client=\S*(?:.*\bsasl_username=(?P<login>[^,\s]+))?")
_QUEUED = re.compile(r"(?P<queue_id>\w+): from=<[^>]*>, size=\d+, nrcpt=(?P<recipients>\d+)")
_BOUNCED = re.compile(r"(?P<queue_id>\w+): to=<(?P<recipient>[^>]*)>, .*\bdsn=(?P<dsn>[\d.]+), status=bounced \((?P<reason>.*)\)")
_LIMIT_REJECT = re.compile(r"NOQUEUE: reject: .*Sending limit reached.*; from=<(?P<sender>[^>]*)>")
# Text only the lines above contain, to skip the rest of the log cheaply.
_MARKERS = (": client=", "nrcpt=", "status=bounced", "Sending limit reached")


@dataclass(frozen=True)
class Login:
    service: str
    when: datetime
    ip: str


@dataclass(frozen=True)
class LogLine:
    when: datetime
    message: str


@dataclass(frozen=True)
class Bounce:
    when: datetime
    recipient: str
    dsn: str
    reason: str


@dataclass(frozen=True)
class WindowUsage:
    seconds: int
    limit: int | None  # None when the account has no limit
    used: int


@dataclass(frozen=True)
class Sending:
    windows: list[WindowUsage]
    refused_recipients: int  # by the sending limit, in the last day
    bounces: list[Bounce]


class MailLog:
    """The lines of the mail log files, read one at a time. After reading, found and unreadable tell how it went."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self._paths = list(paths)
        self.found = False
        self.unreadable: list[Path] = []

    def __iter__(self) -> Iterator[str]:
        for path in self._paths:
            try:
                with path.open(errors="replace") as log:
                    self.found = True
                    yield from log
            except FileNotFoundError:
                continue  # A log that hasn't been rotated yet has no mail.log.1.
            except OSError:
                self.unreadable.append(path)


def last_login_per_service(db: Database, address: str) -> list[Login]:
    """The account's latest login per service, newest first. Dovecot keeps a row per IP address it logged in from."""
    rows = db.rows(
        "SELECT service, last_access, last_ip FROM last_login WHERE userid = %s ORDER BY last_access DESC", address
    )
    latest: dict[str, Login] = {}
    for row in rows:
        latest.setdefault(row["service"], Login(row["service"], _from_timestamp(row["last_access"]), row["last_ip"]))
    return list(latest.values())


def last_login_per_address(db: Database, domain: str | None = None) -> dict[str, datetime]:
    """Each address's latest login over any service, optionally for one domain only."""
    rows = db.rows(
        "SELECT userid, MAX(last_access) AS last_access FROM last_login"
        " WHERE %s IS NULL OR userid LIKE %s GROUP BY userid",
        domain, domain and f"%@{domain}",
    )
    return {row["userid"]: _from_timestamp(row["last_access"]) for row in rows}


def forget(db: Database, address: str) -> None:
    db.execute("DELETE FROM last_login WHERE userid = %s", address)


def parse_line(line: str, now: datetime) -> LogLine | None:
    """Reads a syslog line. Traditional timestamps are local time without a year: they get the latest year that
    doesn't put them in the future."""
    match = _LINE.match(line)
    if not match:
        return None
    text = match["when"]
    try:
        if "T" in text:
            when = datetime.fromisoformat(text)
            when = when if when.tzinfo else when.astimezone()
        else:
            local = datetime.strptime(f"{now.year} {text}", "%Y %b %d %H:%M:%S")
            when = local.astimezone()
            if when > now:
                when = local.replace(year=now.year - 1).astimezone()
    except ValueError:
        return None
    return LogLine(when, match["message"])


def sending(address: str, limits: Sequence[SendLimit], lines: Iterable[str], now: datetime) -> Sending:
    """What the account sent according to the log: recipients per limit window, recipients the limit refused, and
    recent bounces."""
    owned: set[str] = set()  # queue IDs whose current message is the account's
    uncounted: set[str] = set()  # the account's queue IDs whose recipients aren't counted yet
    sent: list[tuple[datetime, int]] = []
    bounces: list[Bounce] = []
    refused = 0
    for line in lines:
        if not any(marker in line for marker in _MARKERS):
            continue
        parsed = parse_line(line, now)
        if parsed is None:
            continue
        when, message = parsed.when, parsed.message
        if client := _CLIENT.match(message):
            # Postfix reuses queue IDs, so every new message decides again whose its ID is.
            queue_id = client["queue_id"]
            if (client["login"] or "").lower() == address:
                owned.add(queue_id)
                uncounted.add(queue_id)
            else:
                owned.discard(queue_id)
                uncounted.discard(queue_id)
        elif queued := _QUEUED.match(message):
            # Deferred mail is logged again at every retry; only the first time counts.
            if queued["queue_id"] in uncounted:
                uncounted.discard(queued["queue_id"])
                sent.append((when, int(queued["recipients"])))
        elif bounce := _BOUNCED.match(message):
            if bounce["queue_id"] in owned and now - when <= BOUNCE_PERIOD:
                bounces.append(Bounce(when, bounce["recipient"], bounce["dsn"], bounce["reason"]))
        elif reject := _LIMIT_REJECT.match(message):
            # Postfix logs a line for every recipient it refuses.
            if reject["sender"].lower() == address and now - when <= REFUSED_PERIOD:
                refused += 1

    windows = [(limit.seconds, limit.recipients) for limit in limits] or [
        (seconds, None) for seconds in WINDOWS_WITHOUT_LIMITS
    ]
    usage = [
        WindowUsage(seconds, limit, sum(count for queued_at, count in sent if now - queued_at <= timedelta(seconds=seconds)))
        for seconds, limit in windows
    ]
    bounces.sort(key=lambda bounce: bounce.when, reverse=True)
    return Sending(usage, refused, bounces[:MAX_BOUNCES])


def _from_timestamp(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)
