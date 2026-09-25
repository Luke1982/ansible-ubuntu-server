"""Whether a filter is one Dovecot will take."""

from pathlib import Path

import pytest

from mailctl.core import sieve
from mailctl.core.sieve import Script

WORKS = 'require ["fileinto"];\nif header :contains "subject" "invoice" { fileinto "Invoices"; }\n'


@pytest.mark.parametrize("name, valid", [
    ("roundcube", True), ("Holiday 2026", True), ("a.b_c-d", True),
    (".svtmp", False), ("", False), ("a" * 65, False), ("../escape", False), ("with/slash", False),
])
def test_a_filter_name_is_what_managesieve_and_a_file_name_both_allow(name, valid):
    assert sieve.valid_name(name) is valid


def test_a_filter_dovecot_compiles_has_nothing_wrong_with_it(fake_command):
    fake_command("sievec")

    assert sieve.check([Script("roundcube", WORKS, True)]) == []


def test_a_filter_that_doesnt_compile_is_reported_with_what_sievec_says(fake_command):
    # sievec names the file it was given; the name of the filter is what means something to the user.
    fake_command("sievec", 'echo "$1: line 2: error: unexpected end of file" >&2; exit 1')

    problems = sieve.check([Script("broken", 'if header :contains "subject" {\n', False)])

    assert len(problems) == 1
    assert problems[0].startswith("broken: ")  # the filter's name, not the temporary file sievec was given
    assert "line 2: error: unexpected end of file" in problems[0]
    assert "/tmp" not in problems[0]


def test_every_filter_is_given_to_sievec(fake_command):
    sievec = fake_command("sievec")

    sieve.check([Script("good", WORKS, True), Script("also good", WORKS, False)])

    assert [Path(call[0]).name for call in sievec.calls] == ["good.sieve", "also good.sieve"]
