import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import Mailctl

from mailctl.core import queue, system

NOW = datetime.now(timezone.utc)


def message(sender: str, minutes_ago: int = 5) -> dict:
    """A message as postqueue -j shows it."""
    return {
        "queue_name": "deferred", "queue_id": f"Q{abs(hash((sender, minutes_ago))) % 10**8}",
        "arrival_time": int((NOW - timedelta(minutes=minutes_ago)).timestamp()), "message_size": 1200,
        "forced_expire": False, "sender": sender,
        "recipients": [{"address": "someone@gmail.com", "delay_reason": "connect to gmail: Connection timed out"}],
    }


@pytest.fixture
def waiting(tmp_path, fake_command):
    """waiting(*messages) makes postqueue -j show these messages."""
    listing = tmp_path / "postqueue.json"

    def show(*messages: dict) -> None:
        listing.write_text("".join(json.dumps(one) + "\n" for one in messages))
    show()
    fake_command("postqueue", f"cat '{listing}'")
    return show


@pytest.fixture
def sent(tmp_path, fake_command):
    """The warnings mailed, each as sendmail got it on its input."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    sendmail = fake_command("sendmail", f"cat > \"{outbox}/$(date +%s%N)\"")

    class Sent:
        calls = property(lambda self: sendmail.calls)
        mails = property(lambda self: [path.read_text() for path in sorted(outbox.iterdir())])
    return Sent()


@pytest.fixture
def settings(config, tmp_path, monkeypatch):
    """mailctl on this server, warning info@example.nl from 3 waiting messages."""
    configured = replace(config, queue_warn_at=3, queue_warn_to="info@example.nl",
                         queue_warning_state=tmp_path / "queue-warning.json")
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(asdict(configured), default=str))
    monkeypatch.setenv("MAILCTL_CONFIG", str(config_file))
    monkeypatch.setattr(system, "require_root", lambda: None)
    return configured


SITE = "garagerenard@server.hosting.example"
LONG_QUEUE = [message(SITE, 90), message(SITE, 30), message(SITE), message("MAILER-DAEMON"), message("info@klant.nl")]


def test_the_queue_is_every_message_with_its_sender_and_a_bounce_has_none(waiting):
    waiting(message("Info@Klant.nl", 10), message("MAILER-DAEMON"), message(""))

    found = queue.queued()

    assert [one.sender for one in found] == ["info@klant.nl", "", ""]
    assert NOW - found[0].arrived == pytest.approx(timedelta(minutes=10), abs=timedelta(seconds=2))


def test_senders_with_the_most_waiting_come_first():
    messages = [queue.Queued(sender, NOW) for sender in ("b@x.nl", "a@x.nl", "b@x.nl", "", "a@x.nl", "b@x.nl")]

    assert queue.by_sender(messages) == [("b@x.nl", 3), ("a@x.nl", 2), ("", 1)]


def test_a_short_queue_warns_nobody(settings, waiting, sent):
    waiting(message(SITE), message(SITE))

    output = Mailctl().ok("queue", "check")

    assert "2 messages waiting; the warning starts at 3." in output
    assert sent.calls == []


def test_a_long_queue_mails_who_sends_the_waiting_mail(settings, waiting, sent):
    waiting(*LONG_QUEUE)

    output = Mailctl().ok("queue", "check")

    assert "Warned info@example.nl." in output
    assert sent.calls == [["-i", "--", "info@example.nl"]]
    mail = sent.mails[0]
    assert "To: info@example.nl\n" in mail
    assert "Subject: Mail queue on server.hosting.example: 5 messages waiting\n" in mail
    assert f"  3  {SITE} (the website of garagerenard)\n" in mail
    assert "  1  bounces (no sender)\n" in mail
    assert "  1  info@klant.nl\n" in mail
    assert "The oldest has waited 1 hour." in mail


def test_the_warning_goes_out_once_while_the_queue_stays_long(settings, waiting, sent):
    waiting(*LONG_QUEUE)
    Mailctl().ok("queue", "check")

    output = Mailctl().ok("queue", "check")

    assert len(sent.calls) == 1
    assert "info@example.nl was warned" in output


def test_the_warning_comes_again_when_the_queue_has_stayed_long_for_hours(settings, waiting, sent):
    waiting(*LONG_QUEUE)
    queue.remember_warning(NOW - queue.WARN_AGAIN - timedelta(minutes=1), settings.queue_warning_state)

    Mailctl().ok("queue", "check")

    assert len(sent.calls) == 1


def test_once_the_queue_was_short_again_the_next_long_one_warns_at_once(settings, waiting, sent):
    waiting(*LONG_QUEUE)
    Mailctl().ok("queue", "check")
    waiting(message(SITE))
    Mailctl().ok("queue", "check")
    waiting(*LONG_QUEUE)

    Mailctl().ok("queue", "check")

    assert len(sent.calls) == 2


def test_without_an_address_to_warn_it_only_says_so(settings, waiting, sent, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(asdict(replace(settings, queue_warn_to="")), default=str))
    waiting(*LONG_QUEUE)

    output = Mailctl().ok("queue", "check")

    assert "Nobody to warn" in output
    assert sent.calls == []


def test_a_warning_names_the_ten_senders_with_the_most_and_counts_the_rest(settings):
    messages = [queue.Queued(f"sender{number:02}@klant.nl", NOW) for number in range(12) for _ in range(number + 1)]

    _, text = queue.warning(settings, messages, NOW)

    assert "  12  sender11@klant.nl\n" in text
    assert "sender01@klant.nl" not in text
    assert "  and 2 other senders\n" in text


def test_a_warning_state_that_cant_be_read_means_no_warning_went_out(tmp_path):
    state = tmp_path / "queue-warning.json"
    state.write_text("not json")

    assert queue.last_warning(state) is None


def test_the_last_warning_is_remembered(tmp_path):
    state = tmp_path / "state" / "queue-warning.json"
    when = datetime.fromtimestamp(int(time.time()), timezone.utc)

    queue.remember_warning(when, state)

    assert queue.last_warning(state) == when
