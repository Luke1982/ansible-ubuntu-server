"""mailctl queue: Postfix's queue of mail waiting to be sent."""

from datetime import datetime, timezone

from .. import ui
from ..core import queue
from ..session import open_session
from .shared import group

app = group("Watch Postfix's queue of mail waiting to be sent.")


@app.command()
def check() -> None:
    """Warn by mail when a lot of mail is waiting to be sent.

    A long queue usually means one account or website is sending a lot of mail, often spam after its password was
    guessed or the site was broken into. From queue_warn_at messages (100 unless the playbook says otherwise) this
    mails queue_warn_to who the waiting mail is from, and again every few hours while the queue stays that long.

    The playbook runs this every 10 minutes (systemctl status mailctl-queue-check.timer).

    [dim]Example:[/] mailctl queue check
    """
    now = datetime.now(timezone.utc)
    with open_session() as session:
        settings = session.config
        messages = queue.queued()
        state = settings.queue_warning_state
        if len(messages) < settings.queue_warn_at:
            queue.forget_warning(state)
            ui.success(f"{ui.plural(len(messages), 'message')} waiting; the warning starts at {settings.queue_warn_at}.")
            return
        subject, text = queue.warning(settings, messages, now)
        ui.warn(subject)
        ui.line(text.rstrip())
        if not settings.queue_warn_to:
            ui.note("Nobody to warn: queue_warn_to isn't set (mail_queue_warn_to in the playbook).")
            return
        warned = queue.last_warning(state)
        if warned and now - warned < queue.WARN_AGAIN:
            ui.note(f"{settings.queue_warn_to} was warned {ui.moment(warned, now)}; again from "
                    f"{(warned + queue.WARN_AGAIN).astimezone():%H:%M} while it stays this long.")
            return
        queue.send_warning(settings, subject, text)
        queue.remember_warning(now, state)
        ui.success(f"Warned {settings.queue_warn_to}.")
