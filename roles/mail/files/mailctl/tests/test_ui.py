from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest
from rich.console import Console

from mailctl import ui
from mailctl.core.dns_check import DnsRecord


@pytest.fixture
def terminal_output(monkeypatch):
    """What mailctl prints, on an ordinary 80-column console."""
    output = StringIO()
    monkeypatch.setattr(ui, "console", Console(file=output, width=80, highlight=False))
    return output


def test_text_shows_markup_and_control_characters_as_they_are():
    shown = ui.text("[bold]x[/] \x1b]0;title\x07 \x9b2J\ttab\nline")

    assert shown.plain == "[bold]x[/] \\x1b]0;title\\x07 \\x9b2J\ttab\nline"


def test_messages_show_stored_text_with_control_characters_made_visible(terminal_output):
    ui.warn("sales@example.nl\x1b[2J still forwards")
    ui.note("[bold]info@example.nl[/]")

    assert "sales@example.nl\\x1b[2J still forwards" in terminal_output.getvalue()
    assert "[bold]info@example.nl[/]" in terminal_output.getvalue()


def test_messages_and_dns_values_are_never_broken_into_lines(terminal_output):
    value = "v=DKIM1; h=sha256; k=rsa; p=" + "A" * 300
    command = "mailctl forward delete " + " ".join(["someone@example.nl"] * 6)

    ui.records([DnsRecord("TXT", "mail._domainkey.example.nl", value)])
    ui.warn(f"To stop that: {command}")

    assert value in terminal_output.getvalue()
    assert command in terminal_output.getvalue()


@pytest.mark.parametrize(
    "byte_count, shown",
    [(0, "0 B"), (1023, "1023 B"), (34567, "33.8 KB"), (1024**2 - 1, "1.0 MB"), (5 * 1024**4, "5.0 TB")],
)
def test_size(byte_count, shown):
    assert ui.size(byte_count) == shown


@pytest.mark.parametrize("seconds, shown", [(3600, "hour"), (7200, "2 hours"), (86400, "day"), (1800, "30 minutes")])
def test_period(seconds, shown):
    assert ui.period(seconds) == shown


def test_moment_says_how_long_ago():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    assert ui.moment(now - timedelta(hours=3, minutes=5), now).endswith("(3 hours ago)")
    assert ui.moment(now - timedelta(seconds=20), now).endswith("(just now)")


def test_plural():
    assert ui.plural(1, "address", "addresses") == "1 address"
    assert ui.plural(2, "address", "addresses") == "2 addresses"
    assert ui.plural(0, "forward") == "0 forwards"
