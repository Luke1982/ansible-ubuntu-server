import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mailctl.core import activity
from mailctl.core.activity import Bounce, LogLine, Login, WindowUsage
from mailctl.core.config import SendLimit

ZONE = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=ZONE)
ADDRESS = "info@example.nl"
LIMITS = (SendLimit(recipients=300, seconds=3600), SendLimit(recipients=1000, seconds=86400))
LIMIT_REJECT = (
    "NOQUEUE: reject: RCPT from laptop[198.51.100.3]: 450 4.7.1 <x@gmail.com>: Recipient address rejected:"
    " Sending limit reached: 300 recipients per 3600 seconds. Try again later.;"
    " from=<{sender}> to=<x@gmail.com> proto=ESMTP helo=<laptop>"
)


@pytest.fixture(autouse=True)
def amsterdam_time():
    """Traditional log timestamps are in local time; these tests use a zone with daylight saving time."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Amsterdam"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def line(minutes_ago, program, message):
    return f"{(NOW - timedelta(minutes=minutes_ago)).isoformat()} mail {program}[1234]: {message}\n"


def sent(queue_id, minutes_ago, recipients, login=ADDRESS):
    return [
        line(minutes_ago, "postfix/submission/smtpd",
             f"{queue_id}: client=laptop[198.51.100.3], sasl_method=PLAIN, sasl_username={login}"),
        line(minutes_ago, "postfix/qmgr", f"{queue_id}: from=<{login}>, size=1234, nrcpt={recipients} (queue active)"),
    ]


def received(queue_id, minutes_ago):
    return line(minutes_ago, "postfix/smtpd", f"{queue_id}: client=mx.gmail.com[142.250.1.1]")


def bounced(queue_id, minutes_ago, recipient):
    return line(
        minutes_ago, "postfix/smtp",
        f"{queue_id}: to=<{recipient}>, relay=mx.gmail.com[142.250.1.1]:25, delay=1, delays=0/0/0/1,"
        " dsn=5.1.1, status=bounced (host mx.gmail.com said: 550 5.1.1 no such user)",
    )


def test_parse_line_reads_rfc3339_timestamps():
    parsed = activity.parse_line("2026-09-14T11:59:30.123456+02:00 mail postfix/qmgr[99]: ABC: removed", NOW)

    assert parsed == LogLine(datetime(2026, 9, 14, 11, 59, 30, 123456, tzinfo=ZONE), "ABC: removed")


def test_parse_line_reads_traditional_timestamps_in_the_current_year():
    parsed = activity.parse_line("Sep  4 08:15:00 mail postfix/qmgr[99]: ABC: removed", NOW)

    assert parsed == LogLine(datetime(2026, 9, 4, 8, 15, tzinfo=ZONE), "ABC: removed")


def test_parse_line_puts_traditional_timestamps_later_than_now_in_the_previous_year():
    assert activity.parse_line("Dec 31 23:59:59 mail postfix/qmgr[99]: ABC: removed", NOW).when.year == 2025


def test_parse_line_uses_the_time_zone_offset_of_the_logged_date():
    winter = datetime(2026, 11, 2, 12, 0, tzinfo=timezone(timedelta(hours=1)))

    parsed = activity.parse_line("Oct 24 10:00:00 mail postfix/qmgr[99]: ABC: removed", winter)

    assert parsed.when.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("text", ["", "not a log line", "Feb 30 10:00:00 mail postfix/qmgr[1]: ABC: removed"])
def test_parse_line_skips_lines_it_cant_read(text):
    assert activity.parse_line(text, NOW) is None


def test_sending_counts_recipients_per_limit_window():
    lines = [*sent("A1", 10, 2), *sent("A2", 120, 10), *sent("A3", 60 * 30, 50)]

    assert activity.sending(ADDRESS, LIMITS, lines, NOW).windows == [
        WindowUsage(seconds=3600, limit=300, used=2),
        WindowUsage(seconds=86400, limit=1000, used=12),
    ]


def test_sending_counts_deferred_mail_once():
    lines = [*sent("A1", 50, 5), line(20, "postfix/qmgr", "A1: from=<info@example.nl>, size=1234, nrcpt=5 (queue active)")]

    assert activity.sending(ADDRESS, LIMITS, lines, NOW).windows[0].used == 5


def test_sending_ignores_other_accounts_and_matches_logins_regardless_of_case():
    lines = [*sent("A1", 5, 3, login="Info@Example.nl"), *sent("B1", 5, 7, login="piet@example.nl")]

    assert activity.sending(ADDRESS, LIMITS, lines, NOW).windows[0].used == 3


def test_sending_follows_a_queue_id_that_postfix_reuses_for_other_mail():
    lines = [
        *sent("A1", 50, 2),
        *sent("A1", 40, 9, login="piet@example.nl"),
        bounced("A1", 39, "piets-recipient@gmail.com"),
        received("A1", 30),
        bounced("A1", 29, "nobody@gmail.com"),
    ]

    result = activity.sending(ADDRESS, LIMITS, lines, NOW)

    assert result.windows[0].used == 2
    assert result.bounces == []


def test_sending_counts_a_reused_queue_id_again_for_the_same_account():
    assert activity.sending(ADDRESS, LIMITS, [*sent("A1", 50, 2), *sent("A1", 20, 3)], NOW).windows[0].used == 5


def test_sending_without_limits_shows_the_last_hour_and_day():
    assert activity.sending(ADDRESS, (), sent("A1", 5, 3), NOW).windows == [
        WindowUsage(seconds=3600, limit=None, used=3),
        WindowUsage(seconds=86400, limit=None, used=3),
    ]


def test_sending_lists_the_five_newest_bounces_of_the_account():
    lines = []
    for day in range(7):
        minutes_ago = 60 * 24 * day + 1
        lines += [*sent(f"Q{day}", minutes_ago + 1, 1), bounced(f"Q{day}", minutes_ago, f"user{day}@gmail.com")]

    bounces = activity.sending(ADDRESS, LIMITS, lines, NOW).bounces

    assert [bounce.recipient for bounce in bounces] == [f"user{day}@gmail.com" for day in range(5)]
    assert bounces[0] == Bounce(
        NOW - timedelta(minutes=1), "user0@gmail.com", "5.1.1", "host mx.gmail.com said: 550 5.1.1 no such user"
    )


def test_sending_leaves_out_bounces_older_than_a_week():
    lines = [*sent("Q1", 60 * 24 * 8, 1), bounced("Q1", 60 * 24 * 8, "old@gmail.com")]

    assert activity.sending(ADDRESS, LIMITS, lines, NOW).bounces == []


def test_sending_counts_the_recipients_the_limit_refused_in_the_last_day():
    lines = [
        line(10, "postfix/submission/smtpd", LIMIT_REJECT.format(sender="info@example.nl")),
        line(60 * 25, "postfix/submission/smtpd", LIMIT_REJECT.format(sender="info@example.nl")),
        line(10, "postfix/submission/smtpd", LIMIT_REJECT.format(sender="piet@example.nl")),
    ]

    assert activity.sending(ADDRESS, LIMITS, lines, NOW).refused_recipients == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_mail_log_reads_the_files_in_order_and_notes_unreadable_ones(tmp_path):
    rotated = tmp_path / "mail.log.1"
    rotated.write_text("one\n")
    current = tmp_path / "mail.log"
    current.write_text("two\nthree\n")
    locked = tmp_path / "locked.log"
    locked.write_text("secret\n")
    locked.chmod(0)
    log = activity.MailLog([tmp_path / "missing.log", rotated, current, locked])

    assert [text.rstrip("\n") for text in log] == ["one", "two", "three"]
    assert log.found
    assert log.unreadable == [locked]


def test_mail_log_notes_that_no_file_was_found(tmp_path):
    log = activity.MailLog([tmp_path / "mail.log"])

    assert list(log) == []
    assert not log.found


def insert_login(database, address, service, timestamp, ip="198.51.100.3"):
    database.execute(
        "INSERT INTO last_login (userid, service, last_ip, last_access) VALUES (%s, %s, %s, %s)",
        address, service, ip, timestamp,
    )


def test_last_login_per_service_gives_the_latest_login_per_service_newest_first(database):
    insert_login(database, ADDRESS, "imap", 1757851200, ip="198.51.100.3")
    insert_login(database, ADDRESS, "imap", 1757851000, ip="198.51.100.9")
    insert_login(database, ADDRESS, "pop3", 1757937600)
    insert_login(database, "piet@example.nl", "imap", 1757940000)

    assert activity.last_login_per_service(database, ADDRESS) == [
        Login("pop3", datetime.fromtimestamp(1757937600, timezone.utc), "198.51.100.3"),
        Login("imap", datetime.fromtimestamp(1757851200, timezone.utc), "198.51.100.3"),
    ]


def test_last_login_per_service_follows_dovecots_writes_from_changing_addresses(database):
    """Dovecot's dict inserts a row and only updates last_access when the row already exists."""
    for ip, timestamp in (("198.51.100.1", 100), ("198.51.100.2", 200), ("198.51.100.1", 300)):
        database.execute(
            "INSERT INTO last_login (userid, service, last_ip, last_access) VALUES (%s, 'imap', %s, %s)"
            " ON DUPLICATE KEY UPDATE last_access = VALUES(last_access)",
            ADDRESS, ip, timestamp,
        )

    assert activity.last_login_per_service(database, ADDRESS) == [
        Login("imap", datetime.fromtimestamp(300, timezone.utc), "198.51.100.1")
    ]


def test_the_nightly_cleanup_keeps_only_the_newest_login_per_service(database):
    task = Path(__file__).resolve().parents[3] / "tasks" / "configure-dovecot.yml"
    cleanup = re.search(r"mariadb mailserver -e '([^']*)'", " ".join(task.read_text().split()))[1]
    insert_login(database, ADDRESS, "imap", 100, ip="198.51.100.1")
    insert_login(database, ADDRESS, "imap", 300, ip="198.51.100.2")
    insert_login(database, ADDRESS, "imap", 200, ip="198.51.100.3")
    insert_login(database, ADDRESS, "pop3", 50, ip="198.51.100.1")
    insert_login(database, "piet@example.nl", "imap", 10, ip="198.51.100.1")

    database.execute(cleanup)

    assert database.rows("SELECT userid, service, last_ip FROM last_login ORDER BY userid, service") == [
        {"userid": ADDRESS, "service": "imap", "last_ip": "198.51.100.2"},
        {"userid": ADDRESS, "service": "pop3", "last_ip": "198.51.100.1"},
        {"userid": "piet@example.nl", "service": "imap", "last_ip": "198.51.100.1"},
    ]


def test_last_login_per_address_gives_each_address_its_latest_login(database):
    insert_login(database, ADDRESS, "imap", 100)
    insert_login(database, ADDRESS, "imap", 200, ip="198.51.100.9")
    insert_login(database, "piet@other.nl", "imap", 300)

    assert activity.last_login_per_address(database, "example.nl") == {ADDRESS: datetime.fromtimestamp(200, timezone.utc)}
    assert activity.last_login_per_address(database).keys() == {ADDRESS, "piet@other.nl"}


def test_forget_removes_the_login_history(database):
    insert_login(database, ADDRESS, "imap", 100)

    activity.forget(database, ADDRESS)

    assert activity.last_login_per_service(database, ADDRESS) == []
