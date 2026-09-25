"""The filters webmail shows, and the Sieve script Dovecot runs.

SOGo is where filters are edited. It keeps them as JSON in its own database, and writes them out as one Sieve
script, named "sogo", over ManageSieve; Dovecot runs that script when a message is delivered. SOGo never sees a
script anyone else wrote: it renders its own from that JSON and makes it the active one, so a filter kept
anywhere else stops running the moment someone saves filters in webmail.

Filters that come from another server are therefore translated into the same list, and written out the same way,
so that there is one place where filters live and one place where they are edited. A rule this can't express as
one of SOGo's is reported instead, and left as the script it came from.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import mailbox
from .db import Database
from .sieveparse import Call, Unreadable, parse

KEY = "SOGoSieveFilters"  # where SOGo keeps them in its user profile
SCRIPT = "sogo"  # the script SOGo writes and activates
# What SOGo's own filter editor offers, which is what its renderer can write back out.
FIELDS = {"subject": "subject", "from": "from", "to": "to", "cc": "cc"}
OPERATORS = {"is": "is", "contains": "contains", "matches": "matches"}
NEGATED = {"is": "is_not", "contains": "contains_not", "matches": "matches_not"}
EVERY_MESSAGE = "allmessages"  # webmail's third way of matching: no rules, every message
_HEADER_TESTS = ("header", "address", "envelope")
_FLAG_ACTIONS = ("addflag", "setflag")
_PLAIN_ACTIONS = ("discard", "keep", "stop", "reject")


@dataclass(frozen=True)
class Untranslatable(Exception):
    """A rule webmail's filter editor can't express, with the reason to tell the user."""
    reason: str

    def __str__(self) -> str:
        return self.reason


def translate(content: str, name: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The filters in a Sieve script as webmail's own, and a line about each rule left behind."""
    try:
        commands = parse(content)
    except Unreadable as problem:
        return [], [f"{name}: {problem}"]
    filters, left = [], []
    number = 0
    for command in commands:
        if command.name == "require":
            continue
        number += 1
        where = f"{command.label or f'rule {number}'} in {name}"
        if command.name != "if":
            left.append(f"{where}: {command.name} outside a rule, which webmail's filters don't have")
            continue
        if len(command.arguments) != 1:
            left.append(f"{where}: an if with {len(command.arguments)} tests")
            continue
        try:
            filters.append(_filter(command.label or f"{name} {number}", command.arguments[0], list(command.block)))
        except Untranslatable as problem:
            left.append(f"{where}: {problem}")
    return filters, left


def render(filters: list[dict[str, Any]]) -> str:
    """The Sieve script for these filters, as SOGo writes it: one if-block per active filter."""
    blocks = [_block(one) for one in filters if one.get("active", 1)]
    used = {"fileinto"} if any("fileinto" in block for block in blocks) else set()
    used |= {"imap4flags"} if any("addflag" in block for block in blocks) else set()
    used |= {"mailbox"} if any(":create" in block for block in blocks) else set()
    used |= {"body"} if any("body :text" in block for block in blocks) else set()
    used |= {"reject"} if any("reject " in block for block in blocks) else set()
    header = f"require {json.dumps(sorted(used))};\n\n" if used else ""
    return header + "\n".join(blocks)


@dataclass(frozen=True)
class Adopted:
    """What became of an account's imported rules."""
    added: list[dict[str, Any]]  # the rules webmail shows from now on
    already: int  # rules webmail had a filter of that name for, left as they were
    left: list[str]  # a line about each rule webmail's filters can't express


def adopt(db: Database, address: str, scripts: Iterable[tuple[str, str]]) -> Adopted:
    """Puts the rules of these (name, content) scripts in webmail's filter list, and writes webmail's own script
    so they run at once instead of when someone next saves filters there.

    Nothing is written when no rule fits webmail's filters: the caller then leaves the scripts to run as they are.
    """
    filters, left = [], []
    for name, content in scripts:
        translated, reasons = translate(content, name)
        filters += translated
        left += reasons
    if not filters:
        return Adopted([], 0, left)
    existing = read(db, address)
    taken = {one.get("name") for one in existing}
    added = [one for one in filters if one.get("name") not in taken]
    write(db, address, existing + added)
    mailbox.put_sieve(address, SCRIPT, render(existing + added))
    mailbox.activate_sieve(address, SCRIPT)
    return Adopted(added, len(filters) - len(added), left)


def read(db: Database, address: str) -> list[dict[str, Any]]:
    """The filters webmail has for the account."""
    return _defaults(db, address).get(KEY) or []


def write(db: Database, address: str, filters: list[dict[str, Any]]) -> None:
    """Puts the filters in webmail's list, leaving the account's other webmail settings alone."""
    defaults = _defaults(db, address)
    defaults[KEY] = filters
    stored = json.dumps(defaults)
    if db.value("SELECT c_uid FROM sogo.sogo_user_profile WHERE c_uid = %s", address) is None:
        db.execute("INSERT INTO sogo.sogo_user_profile (c_uid, c_defaults) VALUES (%s, %s)", address, stored)
    else:
        db.execute("UPDATE sogo.sogo_user_profile SET c_defaults = %s WHERE c_uid = %s", stored, address)


def _defaults(db: Database, address: str) -> dict[str, Any]:
    stored = db.value("SELECT c_defaults FROM sogo.sogo_user_profile WHERE c_uid = %s", address)
    if not stored:
        return {}
    try:
        return json.loads(stored)
    except json.JSONDecodeError:
        return {}


def _filter(name: str, test: Any, actions: list[Any]) -> dict[str, Any]:
    match, tests = _match(test)
    return {
        "name": name,
        "match": match,
        "active": 1,
        "rules": [_rule(one) for one in tests],
        "actions": [one for action in actions for one in _actions(action)],
    }


def _match(test: Any) -> tuple[str, list[Any]]:
    if isinstance(test, Call) and test.name == "true":
        return EVERY_MESSAGE, []  # "if true", which webmail calls every message
    if isinstance(test, Call) and test.name in ("allof", "anyof"):
        inner = list(test.arguments)
        if len(inner) == 1 and isinstance(inner[0], Call) and inner[0].name == "true":
            return EVERY_MESSAGE, []
        return ("all" if test.name == "allof" else "any"), inner
    return "all", [test]


def _rule(test: Any) -> dict[str, str]:
    negated = isinstance(test, Call) and test.name == "not"
    if negated:
        if len(test.arguments) != 1:
            raise Untranslatable("a 'not' over more than one test")
        test = test.arguments[0]
    if not isinstance(test, Call) or test.name not in (*_HEADER_TESTS, "body"):
        raise Untranslatable(f"the test {_describe(test)}, which webmail's filters don't have")
    operator = next((tag for tag in test.tags if tag in OPERATORS), None)
    if operator is None:
        raise Untranslatable(f"a {test.name} test with {', '.join(':' + tag for tag in test.tags) or 'no'} match type")
    arguments = list(test.arguments)
    if "comparator" in test.tags:
        arguments = arguments[1:]  # :comparator takes the name of the comparator with it
    if test.name == "body":
        field, values = "body", arguments[-1:]
    else:
        if len(arguments) < 2:
            raise Untranslatable(f"a {test.name} test without a header and a value")
        field, values = _field(arguments[-2]), arguments[-1:]
    return {"field": field, "operator": NEGATED[operator] if negated else OPERATORS[operator],
            "value": _one(values, "value")}


def _field(fields: Any) -> str:
    listed = list(fields) if isinstance(fields, (list, tuple)) else [fields]
    names = [name.lower() for name in listed if isinstance(name, str)]
    if not names:
        raise Untranslatable("a test without a header name")
    if len(names) == 2 and set(names) == {"to", "cc"}:
        return "to_or_cc"
    if len(names) > 1:
        raise Untranslatable(f"one test over {len(names)} headers ({', '.join(names)})")
    if names[0] not in FIELDS:
        raise Untranslatable(f"the header {names[0]}, which webmail's filters don't offer")
    return FIELDS[names[0]]


def _one(values: Any, what: str) -> str:
    found = list(values) if isinstance(values, (list, tuple)) else [values]
    if len(found) != 1 or not isinstance(found[0], str):
        raise Untranslatable(f"{len(found)} {what}s where webmail's filters take one")
    return found[0]


def _actions(action: Any) -> list[dict[str, str]]:
    """What an action becomes in webmail's list: one of its actions, or more than one where it says more."""
    if not isinstance(action, Call):
        raise Untranslatable(f"the action {_describe(action)}")
    if action.name == "fileinto":
        return [{"method": "fileinto", "argument": _one(action.arguments, "folder")}]
    if action.name == "redirect":
        # ":copy" means the message is forwarded and stays, which is redirect and keep together.
        forward = [{"method": "redirect", "argument": _one(action.arguments, "address")}]
        return forward + [{"method": "keep"}] if "copy" in action.tags else forward
    if action.name in _PLAIN_ACTIONS:
        return [{"method": action.name}]
    if action.name in _FLAG_ACTIONS:
        flags = action.arguments[0] if action.arguments and isinstance(action.arguments[0], list) \
            else list(action.arguments)
        return [{"method": "addflag", "argument": _one([flag], "flag")} for flag in flags]
    raise Untranslatable(f"the action {action.name}, which webmail's filters don't have")


def _describe(node: Any) -> str:
    return node.name if isinstance(node, Call) else json.dumps(node)


def _block(one: dict[str, Any]) -> str:
    tests = [_test_line(rule) for rule in one.get("rules", [])]
    if one.get("match") == EVERY_MESSAGE:
        condition = "true"
    elif not tests:
        return ""
    elif len(tests) == 1:
        condition = tests[0]
    else:
        condition = f"{'allof' if one.get('match', 'all') == 'all' else 'anyof'} ({', '.join(tests)})"
    actions = [_action_line(action) for action in one.get("actions", [])]
    body = "\n".join(f"    {line}" for line in actions if line)
    return f"# rule:[{one.get('name', '')}]\nif {condition}\n{{\n{body}\n}}\n"


def _test_line(rule: dict[str, str]) -> str:
    operator = rule.get("operator", "contains")
    negated = operator.endswith("_not")
    match = operator.removesuffix("_not")
    field = rule.get("field", "subject")
    value = json.dumps(rule.get("value", ""))
    if field == "body":
        test = f"body :text :{match} {value}"
    else:
        headers = ["To", "Cc"] if field == "to_or_cc" else [field.capitalize()]
        names = json.dumps(headers) if len(headers) > 1 else json.dumps(headers[0])
        test = f"header :{match} {names} {value}"
    return f"not {test}" if negated else test


def _action_line(action: dict[str, str]) -> str:
    method, argument = action.get("method"), action.get("argument", "")
    if method == "fileinto":
        return f"fileinto :create {json.dumps(argument)};"
    if method == "redirect":
        return f"redirect {json.dumps(argument)};"
    if method == "addflag":
        return f"addflag {json.dumps(argument)};"
    if method in _PLAIN_ACTIONS:
        return f"{method} {json.dumps(argument)};" if method == "reject" else f"{method};"
    return ""
