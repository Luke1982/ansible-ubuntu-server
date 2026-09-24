"""Just enough of Sieve's grammar to read the rules in a filter script.

Filter editors write a plain shape: a require line, then one "if" per rule with a test and a few actions. This
reads that shape into commands and tests, so they can be turned into webmail's own filters. It is not a Sieve
interpreter: anything it can't read is reported as such, and the script it came from is left alone.
"""

import re
from dataclasses import dataclass, field
from typing import Any

# A comment a filter editor writes above a rule to remember its name: "# rule:[Family]".
RULE_NAME = re.compile(r"#\s*rule:\s*\[(?P<name>[^]]*)]")
_TOKENS = re.compile(r"""
    (?P<space>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<text>text:[^\n]*\n.*?^\.\s*$)
  | (?P<number>\d+[KMG]?)
  | (?P<tag>:[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[(){}\[\],;])
""", re.VERBOSE | re.DOTALL | re.MULTILINE)


class Unreadable(Exception):
    """The script isn't the plain shape a filter editor writes."""


@dataclass(frozen=True)
class Call:
    """A command or a test: its name, its :tags and its arguments."""
    name: str
    tags: tuple[str, ...] = ()
    arguments: tuple[Any, ...] = ()
    block: tuple["Call", ...] = ()
    label: str = ""


@dataclass
class _Token:
    kind: str
    text: str


def parse(content: str) -> list[Call]:
    """The script's top-level commands, in order."""
    return _Parser(_scan(content)).commands()


def _scan(content: str) -> list[_Token]:
    tokens, at, label = [], 0, ""
    while at < len(content):
        found = _TOKENS.match(content, at)
        if not found:
            raise Unreadable(f"can't read {content[at:at + 20]!r}")
        at = found.end()
        kind = found.lastgroup
        if kind == "comment":
            name = RULE_NAME.match(found.group())
            if name:
                tokens.append(_Token("label", name.group("name")))
            continue
        if kind != "space":
            tokens.append(_Token(kind, found.group()))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._at = 0

    def commands(self) -> list[Call]:
        found = []
        while self._peek():
            found.append(self._command())
        return found

    def _command(self) -> Call:
        label = ""
        while self._peek() and self._peek().kind == "label":
            label = self._take().text
        token = self._take()
        if token.kind != "name":
            raise Unreadable(f"expected a command, found {token.text!r}")
        tags, arguments = self._arguments()
        block: tuple[Call, ...] = ()
        if self._peek() and self._peek().text == "{":
            block = tuple(self._block())
        elif self._peek() and self._peek().text == ";":
            self._take()
        else:
            raise Unreadable(f"the command {token.text} ends with neither ; nor a block")
        return Call(token.text.lower(), tags, arguments, block, label)

    def _block(self) -> list[Call]:
        self._take()  # {
        found = []
        while self._peek() and self._peek().text != "}":
            found.append(self._command())
        if not self._peek():
            raise Unreadable("a block that is never closed")
        self._take()  # }
        return found

    def _arguments(self) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        return self._until((";", "{", "}"))

    def _until(self, enders: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        tags: list[str] = []
        arguments: list[Any] = []
        while True:
            token = self._peek()
            if token is None or token.text in enders:
                return tuple(tags), tuple(arguments)
            if token.kind == "tag":
                tags.append(self._take().text.removeprefix(":"))
            elif token.text == ",":
                self._take()
            else:
                arguments.append(self._value())

    def _value(self) -> Any:
        token = self._take()
        if token.kind == "string":
            return _unquote(token.text)
        if token.kind == "text":
            return token.text
        if token.kind == "number":
            return token.text
        if token.text == "[":
            found = []
            while self._peek() and self._peek().text != "]":
                if self._peek().text == ",":
                    self._take()
                    continue
                found.append(self._value())
            if not self._peek():
                raise Unreadable("a list that is never closed")
            self._take()  # ]
            return found
        if token.kind == "name":
            if self._peek() and self._peek().text == "(":
                self._take()
                tags, arguments = self._call_arguments()
                return Call(token.text.lower(), tags, arguments)
            # A test written without brackets, like: header :contains "subject" "invoice". It reaches as far as
            # the comma, bracket or brace that ends it.
            tags, arguments = self._until((",", ")", "]", "{", "}", ";"))
            return Call(token.text.lower(), tags, arguments)
        raise Unreadable(f"can't read {token.text!r}")

    def _call_arguments(self) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        tags: list[str] = []
        arguments: list[Any] = []
        while self._peek() and self._peek().text != ")":
            token = self._peek()
            if token.kind == "tag":
                tags.append(self._take().text.removeprefix(":"))
            elif token.text == ",":
                self._take()
            else:
                arguments.append(self._value())
        if not self._peek():
            raise Unreadable("a test that is never closed")
        self._take()  # )
        return tuple(tags), tuple(arguments)

    def _peek(self) -> _Token | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise Unreadable("the script stops in the middle of a rule")
        self._at += 1
        return token


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text[1:-1])
