"""Checks that aren't about DNS: what doctor says about the mailboxes themselves."""

from mailctl.commands import checks
from mailctl.core.dns_check import Status


def quota_output(*accounts: tuple[str, int, str]) -> str:
    """doveadm's answer for every account: its name, the kilobytes it holds and its limit."""
    lines = ["Username\tQuota name\tType\tValue\tLimit\t%"]
    for address, used, limit in accounts:
        lines.append(f"{address}\tUser quota\tSTORAGE\t{used}\t{limit}\t0")
        lines.append(f"{address}\tUser quota\tMESSAGE\t12\t-\t0")
    return "\n".join(lines) + "\n"


def doveadm(fake_command, output):
    return fake_command("doveadm", f"cat <<'END_OF_OUTPUT'\n{output}END_OF_OUTPUT\n")


def test_mailboxes_with_room_to_spare_are_no_news(fake_command):
    doveadm(fake_command, quota_output(("info@example.nl", 1000, "26214400"),
                                       ("sales@example.nl", 26214400 * 84 // 100, "26214400")))

    found = checks.full_mailboxes()

    assert found.status is Status.OK
    assert "No mailbox is over 85% full" in found.detail


def test_a_mailbox_that_is_nearly_full_is_worth_hearing_about(fake_command):
    doveadm(fake_command, quota_output(("info@example.nl", 26214400 * 91 // 100, "26214400"),
                                       ("sales@example.nl", 1000, "26214400")))

    found = checks.full_mailboxes()

    assert found.status is Status.WARN
    assert "info@example.nl (91%)" in found.detail
    assert "sales@example.nl" not in found.detail
    assert "mailctl address quota info@example.nl SIZE" in found.detail


def test_the_fullest_mailboxes_come_first_and_the_rest_are_counted(fake_command):
    accounts = [(f"user{number}@example.nl", 26214400 * (86 + number) // 100, "26214400") for number in range(7)]
    doveadm(fake_command, quota_output(*accounts))

    found = checks.full_mailboxes()

    assert found.detail.startswith("user6@example.nl (92%)")
    assert "and 2 more" in found.detail


def test_a_mailbox_without_a_limit_is_never_nearly_full(fake_command):
    doveadm(fake_command, quota_output(("info@example.nl", 99999999, "-")))

    assert checks.full_mailboxes().status is Status.OK


def test_a_server_that_keeps_no_quotas_has_nothing_to_say(fake_command):
    """Dovecot without the quota it needs for this answers with nothing, or with an error; either way there
    is no mailbox here that can be too full, and doctor shouldn't say so every time it runs."""
    fake_command("doveadm", "echo 'no such command' >&2; exit 64")

    assert checks.full_mailboxes() is None

    doveadm(fake_command, quota_output())

    assert checks.full_mailboxes() is None
