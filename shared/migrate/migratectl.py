#!/usr/bin/env python3
"""migratectl: the new server's half of a migration.

The old server starts it over SSH (`sudo -n migratectl`) and talks to it one line at a time: a JSON object in,
a JSON object out. Standard output carries nothing else, so anything a person should read goes to standard error.
mailctl and domainctl are only ever run as commands, never imported, so either may be missing.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import unicodedata

PROTOCOL_VERSION = 1
TOOLS = ("mailctl", "domainctl")
LIST_TIMEOUT = 60


def hello(request):
    return {
        "ok": True,
        "version": PROTOCOL_VERSION,
        "server": socket.gethostname(),
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
    }


class Unreadable(Exception):
    """A list of what the server has could not be read. The message says why, in words for a person."""


def _is_rule(line):
    """A line of table borders only, like the ━ line under a header."""
    return all(c.isspace() or unicodedata.name(c, "").startswith("BOX DRAWINGS") for c in line)


def _table_rows(command, what):
    """The rows of a table that a tool prints for people, each as its list of fields.

    The tools have no machine-readable output, so this is the one place that reads their tables. It only trusts
    a table it can see whole: a header with a border line under it. Output it can't make sense of is an error,
    never an empty list, because "nothing there" would make the caller create things that already exist.
    """
    cannot = f"couldn't read the list of {what} on this server"
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=LIST_TIMEOUT,
                              env={**os.environ, "COLUMNS": "200"})
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreadable(f"{cannot} ({error})") from error
    if done.returncode != 0:
        raise Unreadable(f"{cannot}: {command[0]} stopped with an error")
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    rule = next((i for i, line in enumerate(lines) if i > 0 and _is_rule(line)), None)
    if rule is None:
        if any(line.startswith("There are no") for line in lines):
            return []
        raise Unreadable(cannot)
    rows = [line.split() for line in lines[rule + 1:] if not _is_rule(line)]
    if not rows:
        raise Unreadable(cannot)
    return rows


def _mail_domains(cache):
    if "mail" not in cache:
        cache["mail"] = _table_rows(["mailctl", "domain", "list"], "mail domains")
    return cache["mail"]


def _sites(cache):
    if "sites" not in cache:
        cache["sites"] = _table_rows(["domainctl", "list"], "sites")
    return cache["sites"]


def _text(item, field):
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"the item has no {field}")
    return value


def _mail_domain_state(item, cache):
    domain = _text(item, "name")
    for row in _mail_domains(cache):
        if row[0] == domain:
            counts = row[1:3]
            if len(counts) == 2:
                return "exists", f"{domain} is already here, with {counts[0]} accounts and {counts[1]} forwards."
            return "exists", f"{domain} is already here."
    return "new", f"{domain} is not on this server yet."


def _site_state(item, cache):
    domain, user = _text(item, "name"), _text(item, "user")
    for row in _sites(cache):
        if row[0] == user:
            if len(row) > 1 and row[1] == domain:
                return "exists", f"The site {domain} is already here."
            return "refused", f"The user {user} already serves another site here. Use another user name."
        if len(row) > 1 and row[1] == domain:
            return "refused", f"{domain} is already served here by the user {row[0]}."
    return "new", f"{domain} is not on this server yet."


def _cron_state(item, cache):
    # A crontab can't be read through either tool, so a cron job is only ever added, never compared.
    return "new", f"The job will be added to the crontab of {_text(item, 'user')}."


KINDS = {  # kind -> (tool it needs, how to judge an item)
    "mail_domain": ("mailctl", _mail_domain_state),
    "site": ("domainctl", _site_state),
    "cron": ("domainctl", _cron_state),
}


def _judge(item, cache):
    if not isinstance(item, dict):
        raise ValueError("an item must be a JSON object")
    kind = item.get("kind")
    if kind not in KINDS:
        return "refused", f"I don't know the kind {kind!r}, so I can't take it."
    tool, judge = KINDS[kind]
    if shutil.which(tool) is None:
        return "refused", f"This server has no {tool}, so it can't take this."
    return judge(item, cache)


def status(request):
    items = request.get("items")
    if not isinstance(items, list):
        return {"ok": False, "error": "A status request needs a list of items, like {\"items\": [...]}."}
    cache, answers = {}, {}
    for item in items:
        key = item.get("id") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key:
            return {"ok": False, "error": "Every item needs an id, so the answer can be matched to it."}
        try:
            state, detail = _judge(item, cache)
        except Unreadable as error:
            state, detail = "refused", f"{str(error)[0].upper()}{str(error)[1:]}."
        except ValueError as error:
            state, detail = "refused", f"I can't judge this item: {error}."
        answers[key] = {"state": state, "detail": detail}
    return {"ok": True, "items": answers}


HANDLERS = {"hello": hello, "status": status}


def answer(request):
    """One request in, one answer out. It never raises: a bad request must not end the connection."""
    try:
        if not isinstance(request, dict):
            return {"ok": False, "error": "A request must be a JSON object, like {\"request\": \"hello\"}."}
        name = request.get("request")
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return {"ok": False, "error": f"I don't know the request {name!r}. Ask for \"hello\" to see what I can do."}
        return handler(request)
    except Exception as error:  # noqa: BLE001 - whatever went wrong, the old server needs an answer
        return {"ok": False, "error": f"That request failed: {error}"}


def main():
    # Bytes, so a line that is not UTF-8 is answered with an error instead of ending the process.
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # ValueError covers bad JSON and bad UTF-8; deep nesting recurses
            reply = {"ok": False, "error": "That line is not valid JSON. Send one JSON object per line."}
        else:
            reply = answer(request)
        try:
            line = json.dumps(reply)
        except (TypeError, ValueError, RecursionError):
            line = json.dumps({"ok": False, "error": "The answer could not be turned into JSON, so it was left out."})
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
