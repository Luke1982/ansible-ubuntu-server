#!/usr/bin/env python3
"""migratectl: the new server's half of a migration.

The old server starts it over SSH (`sudo -n migratectl`) and talks to it one line at a time: a JSON object in,
a JSON object out. Standard output carries nothing else, so anything a person should read goes to standard error.
mailctl and domainctl are only ever run as commands, never imported, so either may be missing.
"""

import json
import os
import re
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


def _table_rows(command, what, by_column=False):
    """The rows of a table that a tool prints for people, each as its list of fields.

    The tools have no machine-readable output, so this is the one place that reads their tables. It only trusts
    a table it can see whole: a header with a border line under it. Output it can't make sense of is an error,
    never an empty list, because "nothing there" would make the caller create things that already exist.

    A table with a cell that holds several words (the aliases of a site) is cut at the columns of its header,
    since the words can't be told from separate fields. A cell too long for its column continues on a line of
    its own with the other cells empty; that line is merged back into the row above it.
    """
    cannot = f"couldn't read the list of {what} on this server"
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=LIST_TIMEOUT,
                              env={**os.environ, "COLUMNS": "200"})
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreadable(f"{cannot} ({error})") from error
    if done.returncode != 0:
        raise Unreadable(f"{cannot}: {command[0]} stopped with an error")
    lines = [line.rstrip() for line in done.stdout.splitlines() if line.strip()]
    rule = next((i for i, line in enumerate(lines) if i > 0 and _is_rule(line)), None)
    if rule is None:
        if any(line.startswith("There are no") for line in lines):
            return []
        raise Unreadable(cannot)
    body = [line for line in lines[rule + 1:] if not _is_rule(line)]
    if not body:
        raise Unreadable(cannot)
    if not by_column:
        return [line.split() for line in body]
    starts = [m.start() for m in re.finditer(r"\S+(?: \S+)*", lines[rule - 1])]
    rows = []
    for line in body:
        cells = [line[start:end].strip() for start, end in zip(starts, starts[1:] + [None])]
        if rows and not cells[0]:
            rows[-1] = [f"{old} {new}".strip() for old, new in zip(rows[-1], cells)]
        else:
            rows.append(cells)
    return rows


def _mail_domains(cache):
    if "mail" not in cache:
        cache["mail"] = _table_rows(["mailctl", "domain", "list"], "mail domains")
    return cache["mail"]


def _sites(cache):
    if "sites" not in cache:
        cache["sites"] = _table_rows(["domainctl", "list"], "sites", by_column=True)
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
        served = [row[1], *re.findall(r"[^\s,]+", row[2])] if len(row) > 2 else row[1:2]
        if row[0] == user:
            if domain in served[:1]:
                return "exists", f"The site {domain} is already here."
            return "refused", f"The user {user} already serves another site here. Use another user name."
        if domain in served:
            return "refused", f"{domain} is already served here by the user {row[0]}."
    return "new", f"{domain} is not on this server yet."


def _cron_state(item, cache):
    # A crontab can't be read through either tool, so a cron job is only ever added, never compared.
    return "new", (f"Nothing on this server was checked for this job; it will be added to the crontab of "
                   f"{_text(item, 'user')}.")


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


HOMES = "/home"
WEB_ROOT = "public_html/www"  # where domainctl puts a site's web root, inside the user's home
STUB_MARK = "// put here by migratectl; the site's own wp-config.php replaces it when its files arrive"
# What domainctl itself leaves in a new web root: not the site's files, so not worth asking about.
OWN_FILES = (".well-known",)
PLACEHOLDER_SENTENCES = ("put here by domainctl", "This site is set up and waiting for its files.")
OWN_INDEXES = ("index.php", "index.htm", "index.xhtml", "default.php")
COMMAND_TIMEOUT = 600


class Failed(Exception):
    """A command did not do its job. The message says what, in words for a person."""


def _run(command, what, stdin=None):
    try:
        done = subprocess.run(command, capture_output=True, text=True, input=stdin, timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise Failed(f"{what} didn't work ({error}).") from error
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        raise Failed(f"{what} didn't work" + (f": {said[-1]}" if said else "."))
    return done.stdout


def _docroot(user):
    return os.path.join(HOMES, user, WEB_ROOT)


def _is_ours(path):
    """A file the tools put there themselves, which the site's own files are free to replace."""
    try:
        with open(path, errors="replace") as file:
            text = file.read(4096)
    except OSError:
        return False
    if os.path.basename(path) == "wp-config.php":
        return STUB_MARK in text
    return any(sentence in text for sentence in PLACEHOLDER_SENTENCES)


def _files_in(docroot):
    try:
        names = os.listdir(docroot)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if n not in OWN_FILES
                  and not (n in ("index.html", "wp-config.php") and _is_ours(os.path.join(docroot, n))))


def _database_taken(name):
    query = f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '{name}';\n" \
            f"SELECT COUNT(*) FROM mysql.user WHERE User = '{name}';\n"
    counts = _run(["mariadb", "--batch", "--skip-column-names"], "Looking in MariaDB", stdin=query).split()
    if len(counts) != 2:
        raise Failed("Looking in MariaDB didn't give an answer I could read.")
    return any(count != "0" for count in counts)


def _php_value(text, key):
    found = re.search(rf"define\s*\(\s*['\"]{key}['\"]\s*,\s*'((?:[^'\\]|\\.)*)'", text)
    return re.sub(r"\\(.)", r"\1", found[1]) if found else None


def _site_fields(item):
    if item.get("kind") != "site":
        raise ValueError(f"I can't apply the kind {item.get('kind')!r} yet")
    name, user = _text(item, "name"), _text(item, "user")
    database = item.get("database") or None
    if database is not None and not (isinstance(database, str) and re.fullmatch(r"[A-Za-z0-9_$-]{1,64}", database)):
        raise ValueError("the database name may only hold letters, digits, - and _")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError(f"{user!r} can't be a user name")
    return name, user, database


def _apply_site(item, answers):
    name, user, database = _site_fields(item)
    docroot = _docroot(user)
    questions = []
    files = _files_in(docroot)
    if files:
        questions.append(("files", f"The web root {docroot} already holds files ({', '.join(files[:5])}"
                                   f"{', ...' if len(files) > 5 else ''}). Copying the site's files there will "
                                   f"overwrite files with the same name. Go on?"))
    if database and _database_taken(database):
        questions.append(("database", f"The database {database} is already on this server. Going on empties it and "
                                      f"puts the site's data in its place. Go on?"))
    for about, question in questions:
        reply = answers.get(about)
        if reply == "no":
            return {"ok": False, "skipped": True, "error": f"Left {name} alone, as you asked. Nothing was changed."}
        if reply != "yes":
            return {"ok": False, "ask": {"question": question, "about": about}}
    steps = []
    try:
        _run(["domainctl", "add", name, "--user", user, "--no-dns", "--yes"], f"Making the site {name}")
        steps.append(f"Made the site {name} and its user {user}.")
        password = None
        if database:
            config = os.path.join(docroot, "wp-config.php")
            if not os.path.exists(config):
                # wp adopt only works on a site's own wp-config.php. The real one replaces this when the files arrive.
                with open(config, "w") as file:
                    file.write("<?php\n" + STUB_MARK + "\n" + "".join(
                        f"define( '{key}', '' );\n" for key in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST")))
                shutil.chown(config, user, user)
            _run(["domainctl", "wp", "adopt", user, "--database", database, "--replace", "--yes"],
                 f"Making the database {database}")
            steps.append(f"Made the database {database} and a MariaDB user of the same name.")
            with open(config, errors="replace") as file:
                password = _php_value(file.read(), "DB_PASSWORD")
    except (Failed, OSError, LookupError) as error:
        done = " ".join(steps)
        return {"ok": False, "error": f"{error} {done + ' ' if done else ''}Fix the cause and send the site again."}
    return {"ok": True, "docroot": docroot, "database": database, "db_user": database, "password": password,
            "steps": steps}


def _only_item(request):
    item = request.get("item")
    if not isinstance(item, dict):
        raise ValueError("the request needs an item, like {\"item\": {...}}")
    return item


def apply(request):
    answers = request.get("answers", {})
    if not isinstance(answers, dict):
        return {"ok": False, "error": "The answers must be a JSON object."}
    try:
        return _apply_site(_only_item(request), answers)
    except ValueError as error:
        return {"ok": False, "error": f"I can't apply this item: {error}."}
    except Failed as error:
        return {"ok": False, "error": str(error)}


def finish_site(request):
    try:
        _, user, _ = _site_fields(_only_item(request))
        docroot = _docroot(user)
        if not os.path.isdir(docroot):
            return {"ok": False, "error": f"There is no web root at {docroot} yet. Make the site first."}
        _run(["chown", "-R", f"{user}:{user}", docroot], "Giving the files to the site's user")
        index = os.path.join(docroot, "index.html")
        # index.html is served before index.php, so the placeholder would hide the site that just arrived.
        if any(os.path.exists(os.path.join(docroot, own)) for own in OWN_INDEXES) and _is_ours(index):
            os.unlink(index)
        _run(["domainctl", "sync"], "Updating the web server")
    except ValueError as error:
        return {"ok": False, "error": f"I can't finish this item: {error}."}
    except (Failed, OSError) as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "steps": [f"Gave the files in {docroot} to {user}.", "Updated the web server."]}


HANDLERS = {"hello": hello, "status": status, "apply": apply, "finish_site": finish_site}


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
