"""Postfix's queue: what is waiting to be sent, and the warning when that is a lot.

A long queue usually means one account or website sends a lot of mail, mostly spam after its password was guessed or
the site was broken into: much of it can't be delivered and waits for another try.
"""

import json
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import system
from .config import Config

# While the queue stays long, the warning comes again after this.
WARN_AGAIN = timedelta(hours=4)
# The senders a warning names.
TOP_SENDERS = 10
# How postqueue shows the sender of a bounce, which has none.
BOUNCE_SENDERS = ("", "MAILER-DAEMON")


@dataclass(frozen=True)
class Queued:
    sender: str  # "" for a bounce
    arrived: datetime


def queued() -> list[Queued]:
    """Every message in the queue, the ones on hold included."""
    messages = []
    for line in system.run("postqueue", "-j").splitlines():
        if line.strip():
            message = json.loads(line)
            sender = message.get("sender") or ""
            messages.append(Queued(
                sender="" if sender in BOUNCE_SENDERS else sender.lower(),
                arrived=datetime.fromtimestamp(message["arrival_time"], timezone.utc),
            ))
    return messages


def by_sender(messages: Sequence[Queued]) -> list[tuple[str, int]]:
    """How many messages each sender has waiting, most first."""
    counts = Counter(message.sender for message in messages)
    return sorted(counts.items(), key=lambda count: (-count[1], count[0]))


def sender_label(sender: str, hostname: str) -> str:
    """The sender as a warning names it. site-sendmail sends a website's mail as SITE@HOSTNAME."""
    if not sender:
        return "bounces (no sender)"
    if sender.endswith(f"@{hostname}"):
        return f"{sender} (the website of {sender.split('@')[0]})"
    return sender


def warning(config: Config, messages: Sequence[Queued], now: datetime) -> tuple[str, str]:
    """The subject and text of the warning mail."""
    senders = by_sender(messages)
    width = len(str(senders[0][1]))
    lines = [f"  {count:>{width}}  {sender_label(sender, config.hostname)}" for sender, count in senders[:TOP_SENDERS]]
    if len(senders) > TOP_SENDERS:
        lines.append(f"  and {len(senders) - TOP_SENDERS} other senders")
    waited = now - min(message.arrived for message in messages)
    subject = f"Mail queue on {config.hostname}: {len(messages)} messages waiting"
    text = "\n".join([
        f"{len(messages)} messages are waiting to be sent on {config.hostname}; this warning starts at "
        f"{config.queue_warn_at}.",
        "",
        "That usually means one account or website is sending a lot of mail, often spam after its password was "
        "guessed or the site was broken into. Who the waiting mail is from:",
        "",
        *lines,
        "",
        f"The oldest has waited {_hours(waited)}.",
        "",
        "On the server, mailq lists the queue and mailctl status ADDRESS shows how much an account sent.",
        f"While the queue stays this long, this warning comes again every {_hours(WARN_AGAIN)}.",
    ])
    return subject, text + "\n"


def send_warning(config: Config, subject: str, text: str) -> None:
    message = f"From: mailctl <root@{config.hostname}>\nTo: {config.queue_warn_to}\nSubject: {subject}\n\n{text}"
    system.run("sendmail", "-i", "--", config.queue_warn_to, stdin=message)


def last_warning(state: Path) -> datetime | None:
    """When the warning last went out, while the queue has been long since."""
    try:
        return datetime.fromtimestamp(float(json.loads(state.read_text())["warned"]), timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember_warning(when: datetime, state: Path) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"warned": when.timestamp(), "written": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")


def forget_warning(state: Path) -> None:
    """The queue is short again: the next time it is long, the warning goes out at once."""
    state.unlink(missing_ok=True)


def _hours(elapsed: timedelta) -> str:
    minutes = int(elapsed.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'}"
