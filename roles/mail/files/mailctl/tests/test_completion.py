"""The bash completion file lists mailctl's commands by hand, so it drifts silently when a command is added or
taken away. This compares both lists."""

import re
from pathlib import Path

import typer
from conftest import ROLE_FILES

from mailctl import cli

COMPLETION = ROLE_FILES / "mailctl.bash-completion"
_LIST = re.compile(r'^\s*(?:(?P<group>\w[\w-]*)\)\s*)?words="(?P<words>[^"]*)"', re.MULTILINE)


def listed() -> dict[str, set[str]]:
    """The words the completion file offers: under "" for mailctl itself, which it answers at word 1, and one
    entry per group of commands."""
    found = {}
    for line in _LIST.finditer(COMPLETION.read_text()):
        group = line.group("group") or ""
        words = {word for word in line.group("words").split() if not word.startswith("-")}
        found["" if group == "1" else group] = words
    return found


def commands(group=None) -> set[str]:
    """What mailctl answers to, leaving out the hidden ones."""
    command = typer.main.get_command(cli.app)
    if group:
        command = command.commands[group]
    return {name for name, one in command.commands.items() if not one.hidden}


def test_the_completion_file_offers_every_command_mailctl_has():
    assert listed()[""] == commands()


def test_the_completion_file_offers_every_command_of_every_group():
    offered = listed()

    for group, words in sorted(offered.items()):
        if group:
            assert words == commands(group), f"the words for {group}"


def test_every_group_the_completion_file_lists_is_a_group_mailctl_has():
    for group in listed():
        assert group == "" or group in commands(), f"{group} is no longer a command"
