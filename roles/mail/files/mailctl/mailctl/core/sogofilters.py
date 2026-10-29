"""The filters webmail shows, and the Sieve script Dovecot runs.

SOGo is where filters are edited. It keeps them as JSON in its own database, and writes them out as one Sieve
script, named "sogo", over ManageSieve; Dovecot runs that script when a message is delivered. SOGo never sees a
script anyone else wrote: it renders its own from that JSON and makes it the active one, so a filter kept
anywhere else stops running the moment someone saves filters in webmail.

Filters that come from another server are therefore translated into the same list, and written out the same way,
so that there is one place where filters live and one place where they are edited. An out-of-office reply goes
to SOGo's out-of-office settings, which it renders into the same script. A rule this can't express as one of
SOGo's is reported instead, and left as the script it came from.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, NamedTuple

from . import mailbox
from .db import Database
from .sieveparse import Call, Unreadable, parse

KEY = "SOGoSieveFilters"  # where SOGo keeps them in its user profile
VACATION = "Vacation"  # where SOGo keeps the out-of-office reply
SCRIPT = "sogo"  # the script SOGo writes and activates
# What SOGo's own filter editor offers, which is what its renderer can write back out.
FIELDS = {"subject": "subject", "from": "from", "to": "to", "cc": "cc"}
OPERATORS = {"is": "is", "contains": "contains", "matches": "matches"}
NEGATED = {"is": "is_not", "contains": "contains_not", "matches": "matches_not"}
EVERY_MESSAGE = "allmessages"  # webmail's third way of matching: no rules, every message
_HEADER_TESTS = ("header", "address", "envelope")
_FLAG_ACTIONS = ("addflag", "setflag")
# Webmail names the two IMAP flags its editor offers, and refuses to save a filter list that holds a flag under
# any other name ("Sieve generation failure: Action with invalid flag argument"). A label of its own, like
# "$label1", it keeps as it is; every other flag is beyond its editor.
WEBMAIL_FLAGS = {"\\seen": "seen", "\\flagged": "flagged"}
SIEVE_FLAGS = {"seen": "\\Seen", "flagged": "\\Flagged"}
_PLAIN_ACTIONS = ("discard", "keep", "stop", "reject")
# A test on today's date as one end of an out-of-office period: which end, and how many days from the date in the
# test that end is. "Later than the 22nd" starts on the 23rd.
_BOUNDS = {"ge": ("start", 0), "gt": ("start", 1), "le": ("end", 0), "lt": ("end", -1)}


@dataclass(frozen=True)
class Untranslatable(Exception):
    """A rule webmail's filter editor can't express, with the reason to tell the user."""
    reason: str

    def __str__(self) -> str:
        return self.reason


class Translation(NamedTuple):
    filters: list[dict[str, Any]]
    left: list[str]  # a line about each rule left behind
    vacation: dict[str, Any] | None = None  # the out-of-office reply, as webmail keeps it


def translate(content: str, name: str) -> Translation:
    """The rules in a Sieve script as webmail's filters and out-of-office reply, and a line about each rule left
    behind."""
    try:
        commands = parse(content)
    except Unreadable as problem:
        return Translation([], [f"{name}: {problem}"])
    filters, left, vacation = [], [], None
    number = 0
    for command in commands:
        if command.name == "require":
            continue
        number += 1
        where = f"{command.label or f'rule {number}'} in {name}"
        try:
            reply = _vacation(command)
        except Untranslatable as problem:
            left.append(f"{where}: {problem}")
            continue
        if reply is not None:
            if vacation is not None:
                left.append(f"{where}: a second out-of-office reply, where webmail has one")
            vacation = vacation or reply
            continue
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
    return Translation(filters, left, vacation)


def render(filters: list[dict[str, Any]], vacation: dict[str, Any] | None = None) -> str:
    """The Sieve script for these filters and out-of-office reply, as SOGo writes it: one if-block per active
    filter, then the reply when it is on."""
    blocks = [_block(one) for one in filters if one.get("active", 1)]
    used = {"fileinto"} if any("fileinto" in block for block in blocks) else set()
    used |= {"imap4flags"} if any("addflag" in block for block in blocks) else set()
    used |= {"mailbox"} if any(":create" in block for block in blocks) else set()
    used |= {"body"} if any("body :text" in block for block in blocks) else set()
    used |= {"reject"} if any("reject " in block for block in blocks) else set()
    reply = _reply_block(vacation)
    if reply:
        used |= {"vacation", "date", "relational"} if reply.startswith("if ") else {"vacation"}
        blocks.append(reply)
    header = f"require {json.dumps(sorted(used))};\n\n" if used else ""
    return header + "\n".join(blocks)


def last_day(vacation: dict[str, Any]) -> date | None:
    """The last day an out-of-office reply is sent, or None when it has no end."""
    return _day(vacation["endDate"]) if vacation.get("endDateEnabled") and vacation.get("endDate") else None


@dataclass(frozen=True)
class Adopted:
    """What became of an account's imported rules."""
    added: list[dict[str, Any]]  # the rules webmail shows from now on
    already: int  # rules webmail had a filter of that name for, left as they were
    left: list[str]  # a line about each rule webmail's filters can't express
    vacation: dict[str, Any] | None = None  # the out-of-office reply webmail shows from now on


def adopt(db: Database, address: str, scripts: Iterable[tuple[str, str, bool]]) -> Adopted:
    """Puts the rules of these (name, content, active) scripts in webmail's filter list and its out-of-office
    reply in webmail's out-of-office settings, and writes webmail's own script so they run at once instead of when
    someone next saves filters there. A reply stays switched off when its script wasn't the active one.

    Nothing is written when no rule fits webmail: the caller then leaves the scripts to run as they are.
    """
    filters, left, replies = [], [], []
    for name, content, active in scripts:
        found = translate(content, name)
        filters += found.filters
        left += found.left
        if found.vacation is not None:
            # Webmail lists the addresses it replies for, and starts with the account's own.
            reply = {**found.vacation, "autoReplyEmailAddresses": found.vacation["autoReplyEmailAddresses"] or [address]}
            replies.append((name, reply if active else {**reply, "enabled": 0}))
    defaults = _defaults(db, address)
    vacation = None
    for name, reply in sorted(replies, key=lambda one: not one[1]["enabled"]):  # the one that is on comes first
        if vacation is None and not _replying(defaults.get(VACATION)):
            vacation = reply
        else:
            left.append(f"the out-of-office reply in {name}: webmail has one already, left as it is")
    if not filters and vacation is None:
        return Adopted([], 0, left)
    existing = defaults.get(KEY) or []
    taken = {one.get("name") for one in existing}
    added = [one for one in filters if one.get("name") not in taken]
    write(db, address, existing + added, vacation)
    mailbox.put_sieve(address, SCRIPT, render(existing + added, vacation or defaults.get(VACATION)))
    mailbox.activate_sieve(address, SCRIPT)
    return Adopted(added, len(filters) - len(added), left, vacation)


@dataclass(frozen=True)
class Fixed:
    """What normalise() put right for one account."""
    filters: int = 0  # how many of its filters were changed
    dropped: tuple[str, ...] = ()  # actions taken out, with the reason


def normalise(db: Database, address: str, apply: bool = True) -> Fixed:
    """Puts right filters webmail can't save: the ones imported before mailctl gave its flags webmail's own names.

    Saving anything in webmail's filter editor fails whole ("Sieve generation failure: Action with invalid flag
    argument") when one filter anywhere in the list holds a flag under another name, so this goes through them
    all. An action with a flag webmail's editor doesn't have at all is taken out and named.
    """
    stored = read(db, address)
    filters, changed, dropped = [], 0, []
    for one in stored:
        actions, touched = [], False
        for action in one.get("actions") or []:
            if not isinstance(action, dict) or action.get("method") != "addflag":
                actions.append(action)
                continue
            try:
                flag = webmail_flag(str(action.get("argument", "")))
            except Untranslatable as problem:
                dropped.append(f"{one.get('name', 'a filter')}: {problem.reason}")
                touched = True
                continue
            if flag != action.get("argument"):
                action, touched = {**action, "argument": flag}, True
            actions.append(action)
        if touched:
            changed += 1
            one = {**one, "actions": actions}
        filters.append(one)
    if changed and apply:
        write(db, address, filters)
        # The script webmail renders from them runs as it did; it is written again so both say the same thing.
        mailbox.put_sieve(address, SCRIPT, render(filters, _defaults(db, address).get(VACATION)))
    return Fixed(changed, tuple(dropped))


def read(db: Database, address: str) -> list[dict[str, Any]]:
    """The filters webmail has for the account."""
    return _defaults(db, address).get(KEY) or []


def write(db: Database, address: str, filters: list[dict[str, Any]],
          vacation: dict[str, Any] | None = None) -> None:
    """Puts the filters in webmail's list, and the out-of-office reply in its settings when there is one, leaving
    the account's other webmail settings alone."""
    defaults = _defaults(db, address)
    defaults[KEY] = filters
    if vacation is not None:
        defaults[VACATION] = vacation
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


def _replying(vacation: Any) -> bool:
    """Whether webmail has an out-of-office reply of its own: one that is on, or one written and switched off."""
    return isinstance(vacation, dict) and bool(vacation.get("enabled") or vacation.get("autoReplyText"))


def _vacation(command: Call) -> dict[str, Any] | None:
    """The out-of-office reply in a rule, as webmail keeps it, or None when the rule isn't one: a vacation on its
    own, or an if that holds nothing but one, under a first day, a last day or both."""
    if command.name == "vacation":
        return _reply(command, {})
    if command.name != "if" or len(command.arguments) != 1 or [one.name for one in command.block] != ["vacation"]:
        return None
    dates = _dates(command.arguments[0])
    return None if dates is None else _reply(command.block[0], dates)


def _dates(test: Any) -> dict[str, date] | None:
    """The first and last day a test on today's date lets through, or None for a test on anything else."""
    tests = list(test.arguments) if isinstance(test, Call) and test.name == "allof" else [test]
    if not all(isinstance(one, Call) and one.name in ("true", "currentdate") for one in tests):
        return None
    dates: dict[str, date] = {}
    for one in tests:
        if one.name == "currentdate":
            end, day = _bound(one)
            if end in dates:
                raise Untranslatable(f"an out-of-office reply with two {end} dates")
            dates[end] = day
    return dates


def _bound(test: Call) -> tuple[str, date]:
    options, rest = _options(test, ("value", "zone", "comparator", "count"))
    relation = options.get("value")
    if not isinstance(relation, str) or relation not in _BOUNDS or len(rest) != 2 or rest[0] != "date":
        raise Untranslatable("a test on today's date that isn't 'from' or 'until' a day")
    try:
        day = date.fromisoformat(rest[1])
    except (TypeError, ValueError):
        raise Untranslatable(f"the date {rest[1]}, which isn't a day") from None
    end, shift = _BOUNDS[relation]
    return end, day + timedelta(days=shift)


def _reply(call: Call, dates: dict[str, date]) -> dict[str, Any]:
    options, rest = _options(call, ("days", "seconds", "subject", "from", "addresses", "handle"))
    other = [tag for tag in options if tag not in ("days", "subject", "addresses")]
    if other:
        raise Untranslatable(f"an out-of-office reply with :{other[0]}, which webmail's doesn't have")
    if len(rest) != 1 or not isinstance(rest[0], str):
        raise Untranslatable("an out-of-office reply without a text")
    days = str(options.get("days", 7))
    if not days.isdigit():
        raise Untranslatable(f"an out-of-office reply every {days} days")
    listed = options.get("addresses", [])
    end = dates.get("end")
    reply = {
        "enabled": int(end is None or end >= date.today()),
        "autoReplyText": rest[0],
        "autoReplyEmailAddresses": listed if isinstance(listed, list) else [listed],
        "daysBetweenResponse": int(days),
        "customSubjectEnabled": int("subject" in options),
        "customSubject": options.get("subject", ""),
        "startDateEnabled": int("start" in dates),
        "endDateEnabled": int("end" in dates),
    }
    # Webmail keeps a day as the second it starts in the browser's time zone; noon is that day in every zone near
    # this one, both ways.
    reply.update({f"{one}Date": int(datetime.combine(day, time(12)).timestamp()) for one, day in dates.items()})
    return reply


def _options(call: Call, taking: tuple[str, ...]) -> tuple[dict[str, Any], list[Any]]:
    """A call's :tags, each with its argument when it is one of those taking one, and the arguments after them.
    The parser keeps tags and arguments apart in the order they came, and Sieve writes a tag's argument right
    after it, so that is the first argument no tag before it took."""
    rest = list(call.arguments)
    options: dict[str, Any] = {}
    for tag in call.tags:
        options[tag] = rest.pop(0) if tag in taking and rest else True
    return options, rest


def _day(second: Any) -> date:
    return datetime.fromtimestamp(int(second)).date()


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
        return [{"method": "addflag", "argument": webmail_flag(_one([flag], "flag"))} for flag in flags]
    raise Untranslatable(f"the action {action.name}, which webmail's filters don't have")


def webmail_flag(flag: str) -> str:
    """The name webmail gives a flag. Raises for one its editor doesn't have, like \\Answered."""
    known = WEBMAIL_FLAGS.get(flag.lower())
    if known:
        return known
    if flag.startswith("$") or flag in SIEVE_FLAGS:
        return flag
    raise Untranslatable(f"the flag {flag}, which webmail's filters don't have")


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


def _reply_block(vacation: dict[str, Any] | None) -> str:
    """The out-of-office reply as SOGo writes it, sent only from its first day until its last. Empty when it is
    off."""
    if not vacation or not vacation.get("enabled"):
        return ""
    tests = [f'currentdate :value "{relation}" "date" "{_day(vacation[f"{end}Date"]).isoformat()}"'
             for end, relation in (("start", "ge"), ("end", "le"))
             if vacation.get(f"{end}DateEnabled") and vacation.get(f"{end}Date")]
    command = f"vacation :days {int(vacation.get('daysBetweenResponse') or 7)}"
    addresses = ", ".join(_quoted(one) for one in vacation.get("autoReplyEmailAddresses") or [])
    command += f" :addresses [{addresses}]" if addresses else ""
    if vacation.get("customSubjectEnabled") and vacation.get("customSubject"):
        command += f" :subject {_quoted(vacation['customSubject'])}"
    # A line starting with a dot gets a second one, so the lone dot that ends the text is the only one.
    lines = str(vacation.get("autoReplyText", "")).splitlines()
    command += " text:\n" + "".join(("." + line if line.startswith(".") else line) + "\n" for line in lines) + ".\n;"
    if not tests:
        return command + "\n"
    condition = tests[0] if len(tests) == 1 else f"allof ({', '.join(tests)})"
    return f"if {condition}\n{{\n{command}\n}}\n"


def _quoted(value: Any) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


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
        return f"addflag {json.dumps(SIEVE_FLAGS.get(argument, argument))};"
    if method in _PLAIN_ACTIONS:
        return f"{method} {json.dumps(argument)};" if method == "reject" else f"{method};"
    return ""
