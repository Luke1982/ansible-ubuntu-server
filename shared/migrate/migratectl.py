#!/usr/bin/env python3
"""migratectl: the new server's half of a migration.

The old server starts it over SSH (`sudo -n migratectl`) and talks to it one line at a time: a JSON object in,
a JSON object out. Standard output carries nothing else, so anything a person should read goes to standard error.
mailctl and domainctl are only ever run as commands, never imported, so either may be missing.
"""

import glob
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

PROTOCOL_VERSION = 16  # it goes up with every change to the requests or the answers; migrate.py refuses another
TOOLS = ("mailctl", "domainctl")
LIST_TIMEOUT = 60
# OpenLiteSpeed's own PHP, where the web role installs it, for a server that has no php on the path at all.
OLS_PHP = "/usr/local/lsws/lsphp*/bin/lsphp"
# Where domainctl's own wordpress module runs wp-cli from, so a job that runs wp has somewhere to point here.
WP_CLI = "/usr/local/bin/wp"


def _php():
    """The PHP a cron job must run on this server: its own php, which is also the one wp-cli uses, or else
    OpenLiteSpeed's lsphp. The old server says which PHP its jobs ran, never where a PHP is here."""
    found = shutil.which("php")
    return found or max(glob.glob(OLS_PHP), default=None)


def _wp():
    """Where wp-cli is on this server, for a cron job that runs it: the place domainctl itself runs it from, when
    it is really there, or else what the path says. None when this server has no wp-cli at all."""
    return WP_CLI if os.path.exists(WP_CLI) else shutil.which("wp")


# Addresses on the internet, to ask which route the kernel takes; nothing is sent to them.
ROUTE_PROBES = ("1.1.1.1", "2606:4700:4700::1111")


def _addresses():
    """The addresses this server is reached on, so the old server can say where a site's DNS must point: the
    source address the kernel picks for its default route, for IPv4 and for IPv6. This is what
    serverctl.system.server_ips does, written out again because migratectl never imports the tools. An address it
    can't find is left out rather than guessed: the report then says to look on the new server itself."""
    found = []
    for probe in ROUTE_PROBES:
        try:
            done = subprocess.run(["ip", "-json", "route", "get", probe], capture_output=True, text=True,
                                  timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL)
            routes = json.loads(done.stdout) if done.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            continue  # no route for that kind of address, or no ip command at all
        for route in routes if isinstance(routes, list) else []:
            source = route.get("prefsrc") if isinstance(route, dict) else None
            if isinstance(source, str) and source and source not in found:
                found.append(source)
    return found


def _own_sha256():
    """Of this very file, so the old server can tell whether it is the migratectl beside its own migrate.py."""
    try:
        with open(os.path.abspath(__file__), "rb") as file:
            return hashlib.sha256(file.read()).hexdigest()
    except OSError:
        return None


def hello(request):
    return {
        "ok": True,
        "version": PROTOCOL_VERSION,
        "sha256": _own_sha256(),
        "server": socket.gethostname(),
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
        "php": _php(),
        "wp": _wp(),
        "addresses": _addresses(),
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
                              stdin=subprocess.DEVNULL,
                              env={**os.environ, "COLUMNS": "200"})
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreadable(f"{cannot} ({error})") from error
    if done.returncode != 0:
        raise Unreadable(f"{cannot}: {command[0]} stopped with an error")
    lines = [line.rstrip() for line in done.stdout.splitlines() if line.strip()]
    rule = next((i for i, line in enumerate(lines) if i > 0 and _is_rule(line)), None)
    if rule is None:
        if any(line.startswith("There are no") or " has no accounts yet" in line for line in lines):
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


def _signage_servers(cache):
    """The piSignage servers here, as domainctl lists them: user, domain, port, running, certificate."""
    if "signage" not in cache:
        cache["signage"] = _table_rows(["domainctl", "signage", "list"], "piSignage servers", by_column=True)
    return cache["signage"]


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


SITE_STATES = ("served", "made", "absent")


def _site_here(user, cache):
    """What `domainctl state` says about one site user: "served", "made" or "absent".

    `domainctl list` lists virtual hosts, and domainctl saves none until the domain points at this server. A site
    whose user, directories and files are all here therefore isn't in that list, which is the state every site is
    in while a migration runs. `domainctl state` is the one thing that can tell such a site from one that was
    never made, so an unreadable answer is an error and never "absent".
    """
    states = cache.setdefault("site_states", {})
    if user not in states:
        cannot = f"couldn't ask domainctl how far the site {user} is on this server"
        try:
            done = subprocess.run(["domainctl", "state", user], capture_output=True, text=True,
                                  timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreadable(f"{cannot} ({error})") from error
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip().splitlines()
            raise Unreadable(f"{cannot}: {said[-1] if said else 'domainctl stopped with an error'}")
        words = done.stdout.split()
        if len(words) != 1 or words[0] not in SITE_STATES:
            raise Unreadable(f"{cannot}: it answered with something else than one word I know")
        states[user] = words[0]
    return states[user]


def _signage_state(item, cache):
    """Whether this name's piSignage server is here. It is not a site in domainctl's list of sites: it has a
    virtual host of its own, so the list of the piSignage servers is the one that knows it."""
    domain, user = _text(item, "name"), _text(item, "user")
    for row in _signage_servers(cache):
        if row[0] == user or (len(row) > 1 and row[1] == domain):
            return "exists", f"The piSignage server of {domain} is already here."
    return "new", f"There is no piSignage server for {domain} on this server yet."


def _site_state(item, cache):
    if item.get("signage"):
        return _signage_state(item, cache)
    domain, user = _text(item, "name"), _text(item, "user")
    for row in _sites(cache):
        if row[1:2] == ["–"]:
            continue  # a site waiting for the DNS has no domain in the list yet; domainctl state says what it is
        served = [row[1], *re.findall(r"[^\s,]+", row[2])] if len(row) > 2 else row[1:2]
        if row[0] == user:
            if domain in served[:1]:
                return "exists", f"The site {domain} is already here."
            return "refused", f"The user {user} already serves another site here. Use another user name."
        if domain in served:
            return "refused", f"{domain} is already served here by the user {row[0]}."
    here = _site_here(user, cache)
    if here == "made":
        return "exists", (f"The user {user} and its directories are already here, waiting for the DNS: the virtual "
                          f"host is saved once {domain} points at this server.")
    if here == "served":
        # Not in the list, so which domain it serves can't be read here; it is somebody else's site either way.
        return "refused", f"The user {user} already has a site on this server. Use another user name."
    return "new", f"{domain} is not on this server yet."


def _cron_state(item, cache):
    """Whether this job is in the user's crontab already, by the marker the apply writes, which carries the
    item's own id. Neither tool can say, so the crontab itself is read, with the same command the apply uses; a
    crontab that can't be read is an error and never "not there", or the job would be added twice."""
    user, key = _text(item, "user").strip().lower(), _text(item, "id")
    if not _USER_NAME.fullmatch(user):
        raise ValueError(f"{user!r} isn't a valid user name")
    crontabs = cache.setdefault("crontabs", {})
    if user not in crontabs:
        cannot = f"couldn't read the crontab of {user} on this server"
        try:
            done = subprocess.run(["crontab", "-u", user, "-l"], capture_output=True, text=True,
                                  timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreadable(f"{cannot} ({error})") from error
        if done.returncode == 0:
            crontabs[user] = done.stdout
        elif done.returncode == 1 and "no crontab for" in done.stderr:
            crontabs[user] = ""  # no crontab yet is not a failure; anything else is
        else:
            said = done.stderr.strip().splitlines()
            raise Unreadable(f"{cannot}" + (f" ({said[-1]})" if said else ""))
    if f"# migratectl: {key}" in crontabs[user].splitlines():
        return "exists", f"This job is already in the crontab of {user}."
    return "new", f"The crontab of {user} doesn't have this job yet."


KINDS = {  # kind -> (tool it needs, how to judge an item)
    "mail_domain": ("mailctl", _mail_domain_state),
    "site": ("domainctl", _site_state),
    "cron": ("crontab", _cron_state),  # the command that reads it is the one that writes it
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
DNS_TIMEOUT = 900  # it waits for the name to resolve here, then for Let's Encrypt
COMMAND_TIMEOUT = 600
# A mail import makes every account, filter and spam setting one at a time, so a big domain legitimately takes
# long: it gets the half hour the old server gives an apply, less a little, so this answer still reaches it.
IMPORT_TIMEOUT = 1500


class Failed(Exception):
    """A command did not do its job. The message says what, in words for a person."""


def _run(command, what, stdin=None, env=None, timeout=None):
    # The request pipe is our own standard input: no child may read from it.
    extra = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    if env is not None:
        extra["env"] = env
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout or COMMAND_TIMEOUT, **extra)
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


def _quoted(value):
    """A value between SQL quotes, the way domainctl's own wordpress module quotes one. The names here are
    checked before they get this far, but the quoting is where that belongs."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _user_taken(user):
    """Whether this server already has that Linux user. domainctl refuses to take one over unless it is told
    to, so that a site is never put into somebody else's home directory by accident."""
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def _free_database(name):
    """A name like this one that no database on this server has, for the old server to offer instead."""
    stem = name[:28]
    for number in range(2, 100):
        candidate = f"{stem}_{number}"
        if not _database_taken(candidate):
            return candidate
    return f"{stem}_99"


def _database_taken(name):
    query = f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = {_quoted(name)};\n" \
            f"SELECT COUNT(*) FROM mysql.user WHERE User = {_quoted(name)};\n"
    counts = _run(["mariadb", "--batch", "--skip-column-names"], "Looking in MariaDB", stdin=query).split()
    if len(counts) != 2:
        raise Failed("Looking in MariaDB didn't give an answer I could read.")
    return any(count != "0" for count in counts)


# The rules of serverctl.names, written out again because migratectl never imports the tools.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_LABEL}")
_USER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_LOCAL_PART = r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*"
_ADDRESS = re.compile(rf"{_LOCAL_PART}@(?:{_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_LABEL}")
MAX_ADDRESS = 100  # mailctl's own limit, as in the old server's half


def _site_fields(item):
    if item.get("kind") != "site":
        raise ValueError(f"I can't use the kind {item.get('kind')!r} here")
    # Like serverctl.names: lower-cased and stripped first, and the cleaned value is what goes on to domainctl.
    # domainctl allows a site domain up to the DNS limit, not the 50 of a mail domain.
    name, user = _text(item, "name").strip().lower(), _text(item, "user").strip().lower()
    if len(name) > 253 or not _DOMAIN.fullmatch(name):
        raise ValueError(f"{name!r} isn't a valid domain name")
    # The site's database here is the one the old server was told to use in place of the site's own name,
    # when that one was taken; its own is still the name the dump comes out of, over there.
    database = item.get("new_database") or item.get("database") or None
    if isinstance(database, str):
        database = database.strip().lower()
    for what, value in (("user", user), ("database", database)):
        if value is not None and not (isinstance(value, str) and len(value) <= 32 and _USER_NAME.fullmatch(value)):
            raise ValueError(f"{value!r} isn't a valid {what} name: use lower case letters, digits, - and _, "
                             f"starting with a letter or digit, at most 32 characters")
    return name, user, database


HTACCESS = ".htaccess"
HTACCESS_HEADER = ("# Written by migrate when {name} was moved here: on the old server this name was nothing\n"
                   "# but a redirect, and these are its rules. Every site here is an instance of one shared\n"
                   "# OpenLiteSpeed template, which this file is the way around: the template loads it\n"
                   "# (autoLoadHtaccess), so the rules below are this site's own.\n")
MAX_RULES = 200  # more than this is not a redirect any more, and nothing here writes a file of configuration blind


def _redirect_rules(item):
    """The rules of a redirect item, checked before anything is written: Apache redirect lines and nothing
    else, each on one line of its own, since they go into a file this server reads as configuration."""
    rules = item.get("redirect")
    rules = rules.get("rules") if isinstance(rules, dict) else rules
    if not isinstance(rules, list) or not rules:
        return None
    if len(rules) > MAX_RULES:
        raise ValueError(f"that is {len(rules)} redirect rules, and I write at most {MAX_RULES}")
    out = []
    for rule in rules:
        if not isinstance(rule, str) or "\n" in rule or "\r" in rule or len(rule) > 2000:
            raise ValueError("a redirect rule has to be one line of at most 2000 characters")
        first = rule.split()[0].lower() if rule.split() else ""
        if first not in ("redirect", "redirectmatch", "rewritecond", "rewriterule", "rewriteengine"):
            raise ValueError(f"{first or 'that'!r} isn't a redirect rule I put in a .htaccess")
        out.append(rule.strip())
    return out


def _write_htaccess(docroot, name, user, rules):
    """The site's own .htaccess, with its redirect in it. Written whole every time: for a site that is nothing
    but a redirect, this file is the site, so what the old server has is what this one serves."""
    path = os.path.join(docroot, HTACCESS)
    with open(path, "w") as handle:
        handle.write(HTACCESS_HEADER.format(name=name) + "\nRewriteEngine On\n" + "\n".join(rules) + "\n")
    os.chmod(path, 0o644)
    shutil.chown(path, user, user)
    return path


def _apply_site(item, answers):
    name, user, database = _site_fields(item)
    docroot = _docroot(user)
    try:
        rules = _redirect_rules(item)
    except ValueError as error:
        return {"ok": False, "error": f"I won't set {name} up as a redirect: {error}."}
    questions = []
    # A redirect brings no files, so files already there are not in its way; its own .htaccess is asked about
    # below instead.
    files = [] if rules else _files_in(docroot)
    if files:
        questions.append(("files", f"The web root {docroot} already holds files ({', '.join(files[:5])}"
                                   f"{', ...' if len(files) > 5 else ''}). Copying the site's files there will "
                                   f"overwrite files with the same name. Go on?"))
    if _user_taken(user):
        questions.append(("user", f"The Linux user {user} is already on this server, with its home directory "
                                  f"{os.path.join(HOMES, user)}. Going on puts the site in that home directory "
                                  f"and leaves whatever is there. Go on?"))
    if database and _database_taken(database):
        free = _free_database(database)
        questions.append(("database", f"The database {database} is already on this server. Going on empties it and "
                                      f"puts the site's data in its place. Go on? Or give the site a database of "
                                      f"another name here; {free} is free."))
    for about, question in questions:
        reply = answers.get(about)
        if reply == "no":
            return {"ok": False, "skipped": True, "error": f"Left {name} alone, as you asked. Nothing was changed."}
        if reply != "yes":
            return {"ok": False, "ask": {"question": question, "about": about}}
    steps = []
    try:
        # The user is already here and the answer above said to use it: domainctl only takes one over when
        # it is told to, so the site doesn't land in somebody else's home directory by accident.
        take_over = ["--existing-user"] if answers.get("user") == "yes" else []
        _run(["domainctl", "add", name, "--user", user, "--no-dns", "--yes", *take_over],
             f"Making the site {name}")
        steps.append(f"Made the site {name}, its user {user} and its directories.")
        if rules:
            written = _write_htaccess(docroot, name, user, rules)
            steps.append(f"Put the redirect of {name} in {written}: {len(rules)} rule"
                         f"{'' if len(rules) == 1 else 's'}. A redirect has no files to copy after it.")
        if database:
            config = os.path.join(docroot, "wp-config.php")
            if _write_stub(config, user):
                steps.append(f"Put a stand-in wp-config.php in {docroot}; the site's own one replaces it when the "
                             f"files arrive.")
            _run(["domainctl", "wp", "adopt", user, "--database", database, "--replace", "--yes"],
                 f"Making the database {database}")
            steps.append(f"Made the database {database} and a MariaDB user of the same name. Its password is in the "
                         f"site's own wp-config.php.")
    except (Failed, OSError, LookupError) as error:
        return {"ok": False, "error": f"{error} {_what_exists(user, docroot, database)} "
                                      f"Fix the cause and send the site again."}
    if rules:
        return {"ok": True, "docroot": docroot, "redirect": True, "steps": steps}
    answer = {"ok": True, "docroot": docroot, "database": database, "db_user": database, "steps": steps}
    if database:
        # Only a WordPress site gets its database this way, and only that one has a wp-config.php to say it in.
        answer["note"] = ("The database's password is in the site's own wp-config.php, and changes when the "
                          "dump is read in.")
    return answer


def _write_stub(path, user):
    """wp adopt only works on a wp-config.php, and the site's own has not arrived yet. O_EXCL so a real one is
    never overwritten; returns whether a stub was written."""
    text = "<?php\n" + STUB_MARK + "\n" + "".join(
        f"define( '{key}', '' );\n" for key in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST"))
    text += "die( 'This is a stand-in wp-config.php: the site\\'s own never arrived.' );\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as file:
        file.write(text)
    shutil.chown(path, user, user)
    return True


def _what_exists(user, docroot, database):
    """After a failure: what is there now, so a half-built site can be told from an untouched one."""
    made = [f"The user {user} and the directories under {docroot}"] if os.path.isdir(docroot) else []
    config = os.path.join(docroot, "wp-config.php")
    if _is_ours(config) and os.path.exists(config):
        made.append("a stand-in wp-config.php")
    if database:
        try:
            made.append(f"the database {database}" if _database_taken(database) else "")
        except Failed:
            made.append(f"maybe the database {database} (I couldn't look)")
    made = [m for m in made if m]
    return "Now there is " + ", ".join(made) + "." if made else "Nothing was made."


def _only_item(request):
    item = request.get("item")
    if not isinstance(item, dict):
        raise ValueError("the request needs an item, like {\"item\": {...}}")
    return item


# The rules of mailctl's addresses module, written out again because migratectl never imports the tools.
# A hash is "{SCHEME}hash", or a bare crypt hash "$id$..." whose scheme is named by its id. Dovecot here can read a
# hash when its scheme is in the list `doveadm pw -l` prints; one it can't read makes `mailctl import` without a
# terminal refuse everything, so those accounts are named back and their password is asked for on the old server.
OWN_SCHEME = "SHA512-CRYPT"
CRYPT_IDS = {"1": "MD5-CRYPT", "5": "SHA256-CRYPT", "6": "SHA512-CRYPT", "2a": "BLF-CRYPT", "2b": "BLF-CRYPT",
             "2y": "BLF-CRYPT"}


def _scheme_of(password_hash):
    if password_hash.startswith("{") and "}" in password_hash:
        return password_hash[1:password_hash.index("}")].upper()
    if password_hash.startswith("$") and password_hash.count("$") >= 2:
        return CRYPT_IDS.get(password_hash.split("$")[1], "CRYPT")
    return "UNKNOWN"


def _readable_schemes():
    schemes = {scheme.upper() for scheme in
               _run(["doveadm", "pw", "-l"], "Asking Dovecot which schemes it reads").split()}
    if not schemes:  # no list would name every hash as unreadable, so it is a failure, never an answer
        raise Failed("Asking Dovecot which schemes it reads gave an empty list.")
    return schemes


def _existing_accounts(name, data):
    """(addresses already on this server, or None when that can't be read). mailctl import skips those, so their
    hashes are not judged. A domain that isn't here yet has no accounts."""
    try:
        here = {row[0] for row in _mail_domains({})}
        domains = {name, *[d for d in data.get("domains", []) if isinstance(d, str)]}
        found = set()
        for domain in sorted(domains & here):
            found.update(row[0].lower() for row in _table_rows(["mailctl", "address", "list", domain], "accounts"))
        return found
    except Unreadable:
        return None


def _sort_accounts(data, existing=None):
    """The accounts of an import file that this server can't read the password of and that aren't here yet:
    addresses, never hashes. With existing=None every account is judged, which asks for too much rather than too
    little."""
    accounts = [a for a in data.get("addresses", []) if isinstance(a, dict) and isinstance(a.get("address"), str)
                and (existing is None or a["address"].lower() not in existing)]
    hashed = [a for a in accounts if not a.get("password") and isinstance(a.get("password_hash"), str)
              and a["password_hash"] and _scheme_of(a["password_hash"]) != OWN_SCHEME]
    if not hashed:
        return []
    readable = _readable_schemes()
    return [a["address"] for a in hashed if _scheme_of(a["password_hash"]) not in readable]


def _said(done):
    return {"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _apply_mail_domain(item):
    name = _text(item, "name")
    data = item.get("data")
    if not isinstance(data, dict):
        raise ValueError("the item has no data (the JSON for mailctl import)")
    existing = _existing_accounts(name, data)
    needs = _sort_accounts(data, existing)
    result = {}
    if existing is None:
        result["note"] = ("I couldn't read which accounts are already on this server, so I couldn't tell them apart "
                          "from new ones: every account was judged, and one that exists may be named here for "
                          "nothing.")
    if needs:
        # mailctl would refuse the whole file for these; saying so here keeps nothing half-made.
        return {"ok": False, **result, "needs_password": needs,
                "error": f"{name} needs a password for {len(needs)} account(s) before it can be imported: their "
                         f"password hash is in a scheme this server can't read. Nothing was created. Send the same "
                         f"item again with, for those accounts, the plain password in a \"password\" field "
                         f"instead of the \"password_hash\"."}
    fd, path = tempfile.mkstemp(prefix="migratectl-", suffix=".json")  # created 0600, root's
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        try:
            done = subprocess.run(["mailctl", "import", path, "--yes", "--verbose", "--keep-hashes"],
                                  capture_output=True, text=True, timeout=IMPORT_TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            # Killed in the middle: the domain, some of its accounts and their files may be there already.
            return {"ok": False, **result,
                    "error": f"Importing {name} was stopped after {IMPORT_TIMEOUT // 60} minutes, so part of the "
                             f"domain may have been created: the accounts are made one at a time. Ask for the "
                             f"status again, and on this server look at what is there with: mailctl address list "
                             f"{name}. Sending it again adds only what is missing."}
        except (OSError, subprocess.SubprocessError) as error:
            return {"ok": False, **result, "error": f"Importing {name} didn't work ({error}). Nothing is known to "
                                                    f"have been created."}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    reply = {**result, "mailctl": _said(done)}
    if done.returncode == 0:
        return {"ok": True, **reply, "steps": [f"Imported {name} with mailctl."]}
    lines = (done.stderr or done.stdout or "").strip().splitlines()
    if "New passwords are needed" in (done.stderr or "") + (done.stdout or ""):
        # mailctl refuses before it creates anything, so this is the same answer as our own check gives.
        return {"ok": False, **reply, "needs_password": needs,
                "error": f"mailctl refused {name} for want of passwords: {lines[0] if lines else ''} Nothing was "
                         f"created. Send the same item again with, for those accounts, the plain password in a "
                         f"\"password\" field instead of the \"password_hash\"."}
    return {"ok": False, **reply, "error": f"mailctl import didn't work" + (f": {lines[-1]}" if lines else ".")}


def _apply_cron(item):
    user, line, key = _text(item, "user").strip().lower(), _text(item, "line"), _text(item, "id")
    if not _USER_NAME.fullmatch(user):
        raise ValueError(f"{user!r} isn't a valid user name")
    settings = item.get("settings") or []
    if not isinstance(settings, list) or not all(isinstance(s, str) for s in settings):
        raise ValueError("the settings must be a list of NAME=value lines")
    if any("\n" in text for text in [line, key, *settings]):
        raise ValueError("a cron line must be one line")
    marker = f"# migratectl: {key}"
    try:
        listed = subprocess.run(["crontab", "-u", user, "-l"], capture_output=True, text=True,
                                timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise Failed(f"Reading the crontab of {user} didn't work ({error}).") from error
    if listed.returncode == 0:
        existing = listed.stdout
    elif listed.returncode == 1 and "no crontab for" in listed.stderr:
        existing = ""  # no crontab yet is not an error; any other failure is, or the next step would replace it
    else:
        lines = listed.stderr.strip().splitlines()
        raise Failed(f"The crontab of {user} could not be read" + (f" ({lines[-1]})" if lines else "")
                     + ". Nothing was changed.")
    if marker in existing.splitlines():
        return {"ok": True, "already": True, "steps": [f"The job was already in the crontab of {user}."]}
    text = existing + ("" if not existing or existing.endswith("\n") else "\n")
    text += "\n".join([marker, *settings, line]) + "\n"
    _run(["crontab", "-u", user, "-"], f"Installing the crontab of {user}", stdin=text)
    return {"ok": True, "already": False, "steps": [f"Added the job to the crontab of {user}."]}


def apply(request):
    answers = request.get("answers", {})
    if not isinstance(answers, dict):
        return {"ok": False, "error": "The answers must be a JSON object."}
    try:
        item = _only_item(request)
        kind = item.get("kind")
        if kind == "site":
            return _apply_site(item, answers)  # the only kind with a question to answer
        if kind == "cron":
            return _apply_cron(item)
        if kind == "mail_domain":
            return _apply_mail_domain(item)
        raise ValueError(f"I can't apply the kind {kind!r} yet")
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
        config = os.path.join(docroot, "wp-config.php")
        if os.path.exists(config) and _is_ours(config):
            return {"ok": False, "error": f"The files never brought a real wp-config.php: {config} is still the "
                                          f"stand-in. Copy the site's files again, then finish it."}
        _run(["chown", "-R", "-h", f"{user}:{user}", docroot], "Giving the files to the site's user")
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


def password(request):
    """Gives one account a new password, with mailctl, which hashes it. The password comes inside the request
    and goes into mailctl's own standard input: it is never an argument of a command, so it is in no process
    list and no shell history. Nothing is kept here: the old server is the one that shows it, once."""
    address = request.get("address")
    address = address.strip().lower() if isinstance(address, str) else ""
    if len(address) > MAX_ADDRESS or not _ADDRESS.fullmatch(address):
        return {"ok": False, "error": f"{address!r} isn't a mail address I can set a password for."}
    secret = request.get("password")
    if not isinstance(secret, str) or not secret.strip() or "\n" in secret:
        return {"ok": False, "error": "A password request needs a password of its own, on one line."}
    try:
        _run(["mailctl", "address", "password", address, "--password-stdin"],
             f"Setting a new password for {address}", stdin=secret + "\n")
    except Failed as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "steps": [f"Set a new password for {address}."]}


def dns(request):
    """The text of `mailctl dns show DOMAIN`, as it comes, for the old server to print."""
    domain = request.get("domain")
    domain = domain.strip().lower() if isinstance(domain, str) else ""
    if len(domain) > 253 or not _DOMAIN.fullmatch(domain):
        return {"ok": False, "error": f"{domain!r} isn't a valid domain name."}
    try:
        # COLUMNS: the table is printed for the old server's terminal, which is 80 wide.
        text = _run(["mailctl", "dns", "show", domain], f"Asking for the DNS records of {domain}",
                    env=dict(os.environ, COLUMNS="80"))
    except Failed as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "text": text}


def point_dns(request):
    """Moves a site's DNS to this server at TransIP and finishes the site: `domainctl add DOMAIN --yes`,
    which changes the records, waits until the name resolves here, and then asks Let's Encrypt for the real
    certificate. It is the one request that changes something the whole internet sees, so the old server asks
    before it sends it. The TransIP login has to be saved here already (domainctl dns credentials): there is
    no terminal to ask for it in."""
    domain = request.get("domain")
    domain = domain.strip().lower() if isinstance(domain, str) else ""
    if len(domain) > 253 or not _DOMAIN.fullmatch(domain):
        return {"ok": False, "error": f"{domain!r} isn't a valid domain name."}
    try:
        text = _run(["domainctl", "add", domain, "--yes"], f"Moving the DNS of {domain} and finishing the site",
                    timeout=DNS_TIMEOUT, env=dict(os.environ, COLUMNS="80"))
    except Failed as error:
        return {"ok": False, "error": str(error)}
    # domainctl sets the site up either way: a domain whose records it can't change gets its user, its
    # directories and a certificate signed here, and says so. That is not the DNS moving, which is what was
    # asked for, so the answer is no until the name really comes here.
    #
    # domainctl's own check is what decides, because it asks the zone's name servers when its resolver still
    # has the old address; and it prints this line only once that check has passed. Asking this machine's
    # resolver here is the fallback, and on its own it called a move that had just worked a failure: the old
    # record was still in the cache, with the time to live it was published with.
    if f"{domain} points to this server." in " ".join((text or "").split()):
        return {"ok": True, "text": text,
                "steps": [f"Moved the DNS of {domain} to this server and finished the site."]}
    if _points_here(domain) is False:
        return {"ok": False, "text": text,
                "error": f"{domain} still doesn't point at this server, so its DNS was not moved and Let's "
                         f"Encrypt has given it no certificate. The site itself is set up here and waiting "
                         f"for the records. What this server said is under this."}
    return {"ok": True, "text": text, "steps": [f"Moved the DNS of {domain} to this server and finished the site."]}


def _points_here(domain):
    """Whether the name now brings you to this server, or None when that can't be told and nothing is claimed.

    domainctl waits for this itself before it asks Let's Encrypt, and asks the same resolver, so a move it
    finished is a yes here as well: this says no only where it really stopped half way.
    """
    ours = set(_addresses())
    try:
        found = {answer[4][0] for answer in socket.getaddrinfo(domain, None, proto=socket.IPPROTO_TCP)}
    except socket.gaierror as error:
        if error.errno != socket.EAI_NONAME:
            return None  # the lookup itself didn't work, which says nothing about the records
        found = set()
    except OSError:
        return None
    return bool(found & ours) if ours else None


# SpamAssassin's Bayes database, as the mail role makes it (bayes-sql.cf.j2). What the old server learned is read
# into a database beside it first, and takes its place in one step once all of it is here: a connection lost
# halfway leaves this server's own learning as it was.
BAYES = "bayes"
BAYES_INCOMING = "bayes_incoming"
BAYES_TABLES = ("bayes_expire", "bayes_global_vars", "bayes_seen", "bayes_token", "bayes_vars")
BAYES_USER = "vmail"  # bayes_sql_override_username: every account learns into this one user's rows


def _mariadb(sql, what):
    return _run(["mariadb", "--batch", "--skip-column-names"], what, stdin=sql)


def _bayes_tables(database):
    found = _mariadb(f"SELECT table_name FROM information_schema.tables WHERE table_schema = {_quoted(database)};\n",
                     f"Looking at the database {database}").split()
    return [table for table in BAYES_TABLES if table in found]


def _learned(database):
    """How much the Bayes in this database has learned: spam and good messages, and the tokens it knows."""
    counts = _mariadb(f"SELECT COALESCE(SUM(spam_count), 0), COALESCE(SUM(ham_count), 0) FROM `{database}`.bayes_vars;\n"
                      f"SELECT COUNT(*) FROM `{database}`.bayes_token;\n",
                      f"Counting what the database {database} has learned").split()
    if len(counts) != 3 or not all(count.isdigit() for count in counts):
        raise Failed(f"Counting what the database {database} has learned didn't give an answer I could read.")
    return dict(zip(("spam", "ham", "tokens"), map(int, counts)))


def _own_bayes():
    if _bayes_tables(BAYES) != list(BAYES_TABLES):
        raise Failed(f"This server has no complete Bayes database ({BAYES}) for SpamAssassin. Build it with this "
                     f"repository's mail role first.")


def bayes(request):
    """How much SpamAssassin here has learned, so the old server can say what its own learning replaces."""
    try:
        _own_bayes()
        return {"ok": True, **_learned(BAYES)}
    except Failed as error:
        return {"ok": False, "error": str(error)}


def bayes_prepare(request):
    """An empty database for the old server's dump to go into, beside this server's own Bayes."""
    try:
        _own_bayes()
        _mariadb(f"DROP DATABASE IF EXISTS `{BAYES_INCOMING}`;\nCREATE DATABASE `{BAYES_INCOMING}`;\n",
                 f"Making the database {BAYES_INCOMING}")
    except Failed as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "database": BAYES_INCOMING}


def bayes_swap(request):
    """Adds the learning that came in to this server's own, where SpamAssassin reads it: the counts of both are
    added up, so nothing either server learned is thrown away."""
    try:
        missing = [table for table in BAYES_TABLES if table not in _bayes_tables(BAYES_INCOMING)]
        if missing:
            return {"ok": False, "error": f"What came in has no {', '.join(missing)}, so this server's own spam "
                                          f"learning was left as it was."}
        column = _mariadb(f"SELECT column_type FROM information_schema.columns WHERE table_schema = "
                          f"{_quoted(BAYES_INCOMING)} AND table_name = 'bayes_token' AND column_name = 'token';\n",
                          "Looking at how the tokens are stored")
        if "binary" not in column:
            # The fix the mail role makes to its own: with a character set, MariaDB refuses most tokens.
            _mariadb(f"ALTER TABLE `{BAYES_INCOMING}`.bayes_token MODIFY token binary(5) NOT NULL DEFAULT '';\n",
                     "Storing the tokens as bytes")
        users = _mariadb(f"SELECT DISTINCT username FROM `{BAYES_INCOMING}`.bayes_vars;\n",
                         "Looking whose learning came in").split()
        if len(users) == 1 and users[0] != BAYES_USER:
            # One server-wide learning under another name: SpamAssassin here would never look at it.
            _mariadb(f"UPDATE `{BAYES_INCOMING}`.bayes_vars SET username = {_quoted(BAYES_USER)};\n",
                     f"Giving the learning to {BAYES_USER}")
            users = [BAYES_USER]
        _own_bayes()
        _mariadb(_merge_sql(users or [BAYES_USER]), "Adding the spam learning to this server's own")
        _mariadb(f"DROP DATABASE `{BAYES_INCOMING}`;\n", "Clearing away what came in")
        learned = _learned(BAYES)
    except Failed as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, **learned, "steps": ["Added the spam learning to what this server had learned itself."]}


def _merge_sql(users):
    """The SQL that adds what came in to this server's own learning, for each user whose learning came with it.

    Nothing is thrown away on either side: a token both servers know keeps both their counts added up, and the
    time it was last seen is the later of the two; a message either of them has already learned from stays
    learned, so learning it again can't count it twice. It is one transaction, so SpamAssassin reading at that
    moment sees either all of it or none.
    """
    blocks = ["START TRANSACTION;"]
    for user in users:
        name = _quoted(user)
        blocks.append(f"""
SET @source := (SELECT id FROM `{BAYES_INCOMING}`.bayes_vars WHERE username = {name});
INSERT INTO `{BAYES}`.bayes_vars (username, spam_count, ham_count, token_count, last_expire,
                                  last_atime_delta, last_expire_reduce, oldest_token_age, newest_token_age)
SELECT username, 0, 0, 0, last_expire, last_atime_delta, last_expire_reduce, oldest_token_age, newest_token_age
  FROM `{BAYES_INCOMING}`.bayes_vars
 WHERE id = @source AND NOT EXISTS (SELECT 1 FROM `{BAYES}`.bayes_vars WHERE username = {name});
SET @target := (SELECT id FROM `{BAYES}`.bayes_vars WHERE username = {name});
INSERT INTO `{BAYES}`.bayes_token (id, token, spam_count, ham_count, atime)
SELECT @target, incoming.token, incoming.spam_count, incoming.ham_count, incoming.atime
  FROM `{BAYES_INCOMING}`.bayes_token AS incoming
 WHERE incoming.id = @source
    ON DUPLICATE KEY UPDATE
       spam_count = `{BAYES}`.`bayes_token`.spam_count + VALUES(spam_count),
       ham_count = `{BAYES}`.`bayes_token`.ham_count + VALUES(ham_count),
       atime = GREATEST(`{BAYES}`.`bayes_token`.atime, VALUES(atime));
INSERT IGNORE INTO `{BAYES}`.bayes_seen (id, msgid, flag)
SELECT @target, incoming.msgid, incoming.flag
  FROM `{BAYES_INCOMING}`.bayes_seen AS incoming WHERE incoming.id = @source;
UPDATE `{BAYES}`.bayes_vars AS mine, `{BAYES_INCOMING}`.bayes_vars AS incoming
   SET mine.spam_count = mine.spam_count + incoming.spam_count,
       mine.ham_count = mine.ham_count + incoming.ham_count,
       mine.oldest_token_age = LEAST(mine.oldest_token_age, incoming.oldest_token_age),
       mine.newest_token_age = GREATEST(mine.newest_token_age, incoming.newest_token_age)
 WHERE mine.id = @target AND incoming.id = @source;
UPDATE `{BAYES}`.bayes_vars
   SET token_count = (SELECT COUNT(*) FROM `{BAYES}`.bayes_token WHERE id = @target)
 WHERE id = @target;
""".strip())
    return "\n".join(blocks + ["COMMIT;", ""])


REPAIR_TIMEOUT = 900  # it may publish DNS records and wait for a certificate, as point_dns does
DOCTOR_TIMEOUT = 300  # it only looks things up, here and in the DNS, and changes nothing
DOMAIN_IS_RIGHT = "everything is set up"  # what mailctl doctor says about a domain with nothing left to do


def _doctor(domain):
    """(whether the domain itself is right now, what mailctl doctor said), or (None, "") when it couldn't be
    asked.

    A repair prints what it found before it puts any of it right, so its own output is no answer to "is it
    right now": every check it then fixed stands in it as a cross, and the old server would relay those as
    what is still wrong. This asks afterwards instead.

    Doctor's exit status covers this server as well as the domain. A section of its own says the domain needs
    nothing, which is what is asked here: a reverse DNS this server hasn't got is not this domain's business,
    and it would be repeated under every one of them.
    """
    try:
        done = subprocess.run(["mailctl", "doctor", domain], capture_output=True, text=True,
                              timeout=DOCTOR_TIMEOUT, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, COLUMNS="80"))
    except (OSError, subprocess.SubprocessError):
        return None, ""
    text = (done.stdout or "") + (done.stderr or "")
    # The domain's own line, and not a line that merely holds its name: this server's own is built from it.
    return any(DOMAIN_IS_RIGHT in line and domain in line.split() for line in text.splitlines()), text


def repair(request):
    """`mailctl repair DOMAIN --yes`: every step it offers for the domain, without a question, since there is no
    terminal here to answer one. That is its DNS records at TransIP, the certificate, webmail and autodiscover; the
    TransIP login has to be saved here already. What it printed comes back either way, for the old server to show."""
    domain = request.get("domain")
    domain = domain.strip().lower() if isinstance(domain, str) else ""
    if len(domain) > 253 or not _DOMAIN.fullmatch(domain):
        return {"ok": False, "error": f"{domain!r} isn't a valid domain name."}
    try:
        done = subprocess.run(["mailctl", "repair", domain, "--yes"], capture_output=True, text=True,
                              timeout=REPAIR_TIMEOUT, stdin=subprocess.DEVNULL, env=dict(os.environ, COLUMNS="80"))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"mailctl repair {domain} was stopped after {REPAIR_TIMEOUT // 60} minutes. Run "
                                      f"it on this server to see how far it got."}
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"mailctl repair {domain} didn't work ({error})."}
    text = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        lines = text.strip().splitlines()
        return {"ok": False, "text": text,
                "error": f"mailctl repair {domain} stopped with an error" + (f": {lines[-1]}" if lines else ".")}
    right, said = _doctor(domain)
    # What comes back is what the domain is like now, not the catalogue of what the repair found and fixed.
    # When something is left, the repair's own output goes with it: it holds the reason a step couldn't be
    # done, which is the one thing the check afterwards cannot say.
    answer = {"ok": True, "text": said, "right": right,
              "steps": [f"Ran mailctl repair {domain}, saying yes to every step."]}
    if right is not True:
        answer["repair"] = text
    return answer


# ---- Copying the messages of one mailbox over -------------------------------

# imapsync copies a mailbox from another server into this one over IMAP, without ever making a duplicate: a run
# that is stopped carries on where it was the next time. The mail role puts it here
# (roles/mail/tasks/install-imapsync.yml). A mailbox can hold tens of gigabytes, far longer than the old server
# waits for an answer, so it is started here and left running on its own; the old server asks "copy_mail_status"
# how far it is, whenever it likes, and may close the connection in between.
IMAPSYNC = "/usr/local/bin/imapsync"
COPY_ROOT = "/var/lib/migrate/mail"
COPIED_LINE = re.compile(r"^msg .* copied to ", re.M)
COPY_LINES = 12  # lines of the log that come back, so the old server can show what it is doing


def _copy_files(address):
    """The log of one mailbox's copy, the pid of the copy that is running, and the password file it reads."""
    stem = os.path.join(COPY_ROOT, address.replace("/", "_"))
    return stem + ".log", stem + ".pid", stem + ".password"


def _copy_address(request):
    address = request.get("address")
    address = address.strip().lower() if isinstance(address, str) else ""
    if len(address) > MAX_ADDRESS or not _ADDRESS.fullmatch(address):
        raise ValueError(f"{address!r} isn't a mail address I can copy the mail of")
    return address


def _running_copy(pidfile):
    """The copy of this mailbox that is still running, by its pid, or None. A pid file left behind by one that
    is over says nothing on its own, and after a reboot the number may be another program altogether, so what
    that process really is is looked at."""
    try:
        with open(pidfile) as handle:
            pid = int(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            started = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    return pid if "imapsync" in started else None


def _account_here(address):
    """Whether this server has that mailbox. It is never guessed: copying into a mailbox that isn't here is
    nothing but a pile of errors, and the old server is told to move the accounts first."""
    domain = address.split("@")[1]
    return any(row[0].strip().lower() == address
               for row in _table_rows(["mailctl", "address", "list", domain], f"the accounts of {domain}"))


# A mailbox that has been moved from server to server before carries the scars of it: its sent mail sits in a
# folder called INBOX.Sent as well as in Sent, its deleted mail in INBOX.Trash as well as in Trash, and on the
# old server both names open the same folder. --automap knows Sent and Trash and Drafts and Junk by those
# names, but not with INBOX. in front of them, so without this imapsync makes a folder called INBOX.Sent here
# too and the mailbox ends up holding its sent mail twice, in two folders, taking twice the room. This says
# that a folder whose name is one of those with INBOX. in front belongs in the folder of that name here. Only
# those names lose the prefix: a folder under INBOX that is nobody's Sent or Trash is the only copy of the
# customer's mail there is, and moving it out from under INBOX would put it somewhere they don't look.
# imapsync applies this only to the folders --automap didn't recognise, and lets several of the old server's
# folders meet in one of ours.
UNDER_INBOX = ("s,^INBOX\\.(Sent|Sent Items|Sent Messages|Verzonden items|Verzonden Items|Drafts|Concepten|"
               "Trash|Deleted Items|Verwijderde items|Junk|Junk E-mail|Spam|Ongewenste e-mail)$,$1,i")


def _imapsync_command(address, host, passfile):
    """What is run. --automap puts Sent, Drafts and Trash in the folders this server calls them, whatever the
    other server's names for them are; --regextrans2 takes care of the same folders with INBOX. in front of
    them; --nofoldersizes starts copying at once instead of first counting a mailbox that may be tens of
    gigabytes; --nolog keeps imapsync's own second copy of the log out of the way. A message that is on both
    servers is left alone, so a copy that is started again adds only what is new."""
    return [IMAPSYNC,
            "--host1", host, "--user1", address, "--passfile1", passfile, "--ssl1",
            "--host2", "localhost", "--user2", address, "--passfile2", passfile, "--ssl2",
            "--automap", "--regextrans2", UNDER_INBOX, "--nofoldersizes", "--nolog", "--noreleasecheck"]


def copy_mail(request):
    """Starts copying one mailbox from the old server into this one, and leaves it running. The password comes
    inside the request and is written to a file only root may read: it is the account's own on both servers,
    and never an argument of a command, so it is in no process list."""
    try:
        address = _copy_address(request)
    except ValueError as error:
        return {"ok": False, "error": f"{error}."}
    secret = request.get("password")
    if not isinstance(secret, str) or not secret.strip() or "\n" in secret:
        return {"ok": False, "error": "A request to copy mail needs the password of the mailbox, on one line."}
    host = request.get("host")
    host = host.strip().lower() if isinstance(host, str) else ""
    if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9.:_-]+", host):
        return {"ok": False, "error": "A request to copy mail needs the name or address of the server to copy "
                                      "the mailbox from."}
    if not os.access(IMAPSYNC, os.X_OK):
        return {"ok": False, "error": f"There is no imapsync at {IMAPSYNC} on this server, so no mail can be "
                                      f"copied into it. Run this repository's playbook again: the mail role "
                                      f"installs it."}
    try:
        if not _account_here(address):
            return {"ok": False, "error": f"{address} is not a mailbox on this server, so there is nothing to "
                                          f"copy into. Move the mail of its domain first."}
    except Unreadable as error:
        return {"ok": False, "error": f"{error}, so I can't tell whether {address} is here."}
    log, pidfile, passfile = _copy_files(address)
    if _running_copy(pidfile):
        return {"ok": True, "started": False, "running": True,
                "steps": [f"The mail of {address} was already being copied here."]}
    try:
        os.makedirs(COPY_ROOT, mode=0o700, exist_ok=True)
        with open(os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            handle.write(secret + "\n")
        # Appended to, so what an earlier run of the same mailbox did is still there to read.
        with open(log, "a") as out:
            out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')}: copying {address} from {host} ====\n")
            out.flush()
            # Its own session, so it keeps running when the old server closes this connection.
            running = subprocess.Popen(_imapsync_command(address, host, passfile), stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        with open(pidfile, "w") as handle:
            handle.write(f"{running.pid}\n")
    except OSError as error:
        return {"ok": False, "error": f"I couldn't start imapsync for {address} ({error})."}
    return {"ok": True, "started": True, "running": True, "pid": running.pid, "log": log,
            "steps": [f"Started copying the mail of {address} from {host}."]}


# imapsync can also be asked to do nothing but count: --justfoldersizes opens both mailboxes, counts every
# folder on each side and exits. It is the only answer to "is this mailbox across" that is worth anything,
# because it is the same program, reading the same two mailboxes the same way the copy does, and it counts the
# messages a mail program can see rather than the files lying in a maildir. Counting a big mailbox takes
# minutes, so it is only ever done when somebody asks for it.
COMPARE_TIMEOUT = 1500
# "Host1 folder     3/9 INBOX.Sent                        Size:  12345 Messages:  67 Biggest:  890", or the
# same with " does not exist" where the numbers would be. The folders of the two servers are paired by the
# counter in front of them, not by their names: --automap means the names may differ.
_FOLDER_LINE = re.compile(
    r"^Host(?P<side>[12]) folder\s+(?P<counter>\S+)\s+(?P<name>.+?)\s+"
    r"(?:Size:\s*(?P<bytes>\d+)\s+Messages:\s*(?P<messages>\d+)\s+Biggest:\s*\d+|does not exist\S*)\s*$",
    re.M)
_TOTAL_LINE = r"^Host%d %s:\s+(\d+)"


def _compare_command(address, host, passfile):
    """What is run to compare the two mailboxes: the copy's own command, told to count and stop. It carries the
    copy's folder mapping as well, so the folders it pairs up are the ones the copy would write into."""
    return [IMAPSYNC,
            "--host1", host, "--user1", address, "--passfile1", passfile, "--ssl1",
            "--host2", "localhost", "--user2", address, "--passfile2", passfile, "--ssl2",
            "--automap", "--regextrans2", UNDER_INBOX, "--justfoldersizes", "--nolog", "--noreleasecheck"]


def _total(text, side, what):
    found = re.search(_TOTAL_LINE % (side, what), text, re.M)
    return int(found.group(1)) if found else None


def _compare_folders(text):
    """The folders imapsync counted, paired by the number it puts in front of each, as
    [{name, name_there, here, there}] in the order it read them."""
    found, order = {}, []
    for match in _FOLDER_LINE.finditer(text):
        counter, side = match.group("counter"), match.group("side")
        if counter not in found:
            found[counter] = {"name": None, "name_there": None, "here": None, "there": None}
            order.append(counter)
        folder = found[counter]
        held = ({"bytes": int(match.group("bytes")), "messages": int(match.group("messages"))}
                if match.group("bytes") is not None else None)
        if side == "1":
            folder["name"], folder["here"] = match.group("name"), held
        else:
            folder["name_there"], folder["there"] = match.group("name"), held
    return [found[counter] for counter in order]


def _compare_trouble(text, code):
    """What to say when the counting didn't come to an answer: imapsync's own last words about it."""
    for pattern in (r"^.*(?:Err |ERROR|Failure|failed|Can't |can not |NO Login|AUTHENTICATIONFAILED).*$",):
        lines = [line.strip() for line in re.findall(pattern, text, re.M | re.I) if line.strip()]
        if lines:
            return lines[-1]
    last = [line.strip() for line in text.splitlines() if line.strip()]
    return last[-1] if last else f"imapsync stopped with return value {code} and said nothing."


def compare_mail(request):
    """Counts one mailbox on each server, folder by folder, with imapsync itself. Nothing is copied, nothing is
    changed: it only answers whether the messages are across, and which folder they are missing from."""
    try:
        address = _copy_address(request)
    except ValueError as error:
        return {"ok": False, "error": f"{error}."}
    secret = request.get("password")
    if not isinstance(secret, str) or not secret.strip() or "\n" in secret:
        return {"ok": False, "error": "A request to compare mail needs the password of the mailbox, on one line."}
    host = request.get("host")
    host = host.strip().lower() if isinstance(host, str) else ""
    if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9.:_-]+", host):
        return {"ok": False, "error": "A request to compare mail needs the name or address of the server to "
                                      "compare the mailbox with."}
    if not os.access(IMAPSYNC, os.X_OK):
        return {"ok": False, "error": f"There is no imapsync at {IMAPSYNC} on this server, so the mailboxes "
                                      f"can't be compared. Run this repository's playbook again: the mail "
                                      f"role installs it."}
    try:
        if not _account_here(address):
            return {"ok": False, "error": f"{address} is not a mailbox on this server, so there is nothing "
                                          f"here to count against. Move the mail of its domain first."}
    except Unreadable as error:
        return {"ok": False, "error": f"{error}, so I can't tell whether {address} is here."}
    _log, pidfile, passfile = _copy_files(address)
    if _running_copy(pidfile):
        return {"ok": False, "error": f"The mail of {address} is being copied here right now, so counting it "
                                      f"would only say what it held a moment ago. Let the copy finish first."}
    try:
        os.makedirs(COPY_ROOT, mode=0o700, exist_ok=True)
        with open(os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            handle.write(secret + "\n")
        done = subprocess.run(_compare_command(address, host, passfile), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=COMPARE_TIMEOUT)
        text = done.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Counting the mailbox of {address} took longer than "
                                      f"{COMPARE_TIMEOUT // 60} minutes, so I stopped it. Nothing was changed."}
    except OSError as error:
        return {"ok": False, "error": f"I couldn't run imapsync for {address} ({error})."}
    finally:
        try:
            os.unlink(passfile)
        except OSError:
            pass
    folders = _compare_folders(text)
    if not folders:
        trouble = _compare_trouble(text, done.returncode)
        return {"ok": False, "error": f"imapsync counted nothing of {address}: {trouble}"}
    here = {"bytes": _total(text, 1, "Total size"), "messages": _total(text, 1, "Nb messages")}
    there = {"bytes": _total(text, 2, "Total size"), "messages": _total(text, 2, "Nb messages")}
    return {"ok": True, "folders": folders, "here": here, "there": there}


def _last_number(pattern, text, default=None):
    found = re.findall(pattern, text, re.M)
    return int(found[-1]) if found else default


# The log is appended to, so the file holds every run there has ever been. Counting over all of them says a
# mailbox is being copied that was copied days ago, and shows the old run's numbers for the new one.
_RUN_HEADER = re.compile(r"^==== .*: copying .* ====$", re.M)
SYNC_IS_GOOD = "The sync looks good"
_LEFT_HERE = r"^Messages found in host1 not in host2\s*:\s*(\d+)"
_TRANSFERRED = r"^Messages transferred\s*:\s*(\d+)"
_ENDED = r"^Exiting with return value ([0-9]+)"


def _last_run(text):
    """What the last run of imapsync wrote, out of a log that holds them all."""
    starts = [found.start() for found in _RUN_HEADER.finditer(text)]
    return text[starts[-1]:] if starts else text


def _last_sync(address):
    """What imapsync said the last time it finished copying this mailbox: whether everything it could see here
    is on this server, and how many messages it still found here that this server hasn't got. None when it has
    never run, or is running now: half a run says nothing.

    It is the only honest answer to "is this mailbox across". Counting the files of a maildir says what lies on
    the disk, and dovecot does not serve every one of those as a message.
    """
    log = _copy_files(address)[0]
    try:
        with open(log, errors="replace") as handle:
            text = _last_run(handle.read())
    except OSError:
        return None
    if not re.search(_ENDED, text, re.M):
        return None
    try:
        ago = int(time.time() - os.path.getmtime(log))
    except OSError:
        return None
    # How long ago, not when: the two servers' clocks are their own, and a difference between them would make
    # mail that came in after the copy look as if it came in before it.
    return {"good": SYNC_IS_GOOD in text, "left": _last_number(_LEFT_HERE, text),
            "transferred": _last_number(_TRANSFERRED, text),
            "errors": _last_number(r"Detected ([0-9]+) errors?", text), "ago": max(ago, 0)}


def copy_mail_status(request):
    """How far the copy of one mailbox is. It can be asked at any time, by any run of the old server's tool:
    what is running is looked up here, never remembered there."""
    try:
        address = _copy_address(request)
    except ValueError as error:
        return {"ok": False, "error": f"{error}."}
    log, pidfile, passfile = _copy_files(address)
    if not os.path.exists(log):
        return {"ok": True, "started": False, "running": False}
    running = bool(_running_copy(pidfile))
    try:
        with open(log, errors="replace") as handle:
            text = handle.read()
    except OSError as error:
        return {"ok": False, "error": f"I can't read what the copy of {address} did ({error})."}
    if not running:
        # Only imapsync reads it, and only while it runs: a mailbox's password is kept no longer than that.
        try:
            os.unlink(passfile)
        except OSError:
            pass
    text = _last_run(text)  # this copy's own numbers, not every copy this mailbox ever had
    lines = [line for line in text.splitlines() if line.strip()]
    return {"ok": True, "started": True, "running": running,
            "copied": len(COPIED_LINE.findall(text)),
            "transferred": _last_number(_TRANSFERRED, text),
            "errors": _last_number(r"Detected ([0-9]+) errors?", text),
            "exit": _last_number(_ENDED, text),
            "good": SYNC_IS_GOOD in text,
            "left": _last_number(_LEFT_HERE, text),
            "last": lines[-1] if lines else "",
            "text": "\n".join(lines[-COPY_LINES:])}


VMAIL = "/var/vmail"  # where the mail role has dovecot keep the mailboxes: /var/vmail/<domain>/<user>
MAIL_FOLDERS = ("cur", "new")  # where a maildir keeps its messages; tmp holds half-delivered ones
MAX_MAILBOXES = 200  # one customer's accounts, with room to spare; a request asking for more is a mistake


def mailbox_size(address, mail_root=None):
    """{bytes, messages} of one mailbox on this server, or None when there is no mailbox for it here.

    The messages themselves are counted, wherever the maildir sits inside the account's own folder, and not
    dovecot's indexes beside them: this server builds its own, so a mailbox that is wholly copied over would
    still look a different size from the old server's if they were counted in.
    """
    mail_root = VMAIL if mail_root is None else mail_root
    local_part, _, domain = address.rpartition("@")
    folder = os.path.join(mail_root, domain, local_part)
    if not os.path.isdir(folder):
        return None
    total = messages = 0
    for place, _folders, files in os.walk(folder):
        if os.path.basename(place) not in MAIL_FOLDERS:
            continue
        for name in files:
            try:
                total += os.lstat(os.path.join(place, name)).st_size
            except OSError:
                continue  # a message delivered or moved while this was walking it
            messages += 1
    return {"bytes": total, "messages": messages}


def mail_sizes(request):
    """How much mail the mailboxes of these accounts hold on this server, so the old server can hold its own
    beside it and say whether the messages are across. It only measures: nothing here is changed or read but
    the size of the files, and an address with no mailbox here is said to have none rather than nothing."""
    addresses = request.get("addresses")
    if not isinstance(addresses, list):
        return {"ok": False, "error": "A mail_sizes request needs a list of addresses, like "
                                      "{\"addresses\": [...]}."}
    if len(addresses) > MAX_MAILBOXES:
        return {"ok": False, "error": f"That is {len(addresses)} addresses, and I measure at most "
                                      f"{MAX_MAILBOXES} in one request."}
    found = {}
    for address in addresses:
        try:
            address = _copy_address({"address": address})
        except ValueError:
            continue  # not an address at all, so there is no mailbox of it here to measure
        one = mailbox_size(address)
        found[address] = dict(one, here=True) if one else {"here": False}
        # What imapsync said last time it copied this mailbox. The files on the disk are only a hint; this is
        # the answer of the program that does the copying, and it is the one that settles it.
        sync = _last_sync(address)
        if sync:
            found[address]["sync"] = sync
    return {"ok": True, "mailboxes": found}


# ---- A database for a site that is not WordPress ----------------------------
#
# A WordPress site keeps its database settings in wp-config.php, which domainctl wp adopt rewrites. A site that
# is not WordPress keeps them wherever its builder put them, and nothing here can know where. So this makes the
# database and the user and hands the password back: the old server shows it, and a person puts it in place.
DUMP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.sql")
PASSWORD_LENGTH = 24


def _dump_in_docroot(user, name):
    """Where the dump lies: inside the site's own web root, where it arrived with the site's files. The old
    server sends a file name and never a path, so nothing here can be pointed at a file somewhere else."""
    if not isinstance(name, str) or not DUMP_NAME.fullmatch(name):
        raise ValueError(f"{name!r} isn't a name of a dump I'll read: letters, digits, dot, - and _, ending "
                         f"in .sql")
    return os.path.join(_docroot(user), name)


def adopt_database(request):
    """Makes a database and a MariaDB user for a site that is not WordPress, and reads the dump that came over
    with the site's files into it. The password is made here and given back once: this server writes it
    nowhere the site can read, because it doesn't know where the site keeps its settings."""
    try:
        name, user, database = _site_fields(dict(request.get("item") or {}, kind="site"))
    except (ValueError, LookupError) as error:
        return {"ok": False, "error": f"{error}."}
    if not database:
        return {"ok": False, "error": "A request to make a database needs the name to make it under."}
    try:
        dump = _dump_in_docroot(user, request.get("dump"))
    except ValueError as error:
        return {"ok": False, "error": f"{error}."}
    if not os.path.isfile(dump):
        return {"ok": False, "error": f"The dump {dump} isn't there. The site's files have to be copied over "
                                      f"first: the dump comes with them."}
    account = request.get("db_user") or database
    if not (isinstance(account, str) and len(account) <= 32 and _USER_NAME.fullmatch(account)):
        return {"ok": False, "error": f"{account!r} isn't a valid MariaDB user name: lower case letters, "
                                      f"digits, - and _, at most 32 characters."}
    given = request.get("password")
    if given is not None and (not isinstance(given, str) or not given.strip() or "\n" in given):
        return {"ok": False, "error": "The password of the site's database has to be one line of text."}
    if _database_taken(database) and not request.get("replace"):
        free = _free_database(database)
        return {"ok": False, "ask": {"about": "database",
                                     "question": f"The database {database} is already on this server. Going on "
                                                 f"empties it and puts this site's data in its place. Go on? Or "
                                                 f"give it a database of another name here; {free} is free."}}
    # The site's own settings came over with its files, and they hold a user name and a password. Making the
    # same ones here is what lets the site work there without a line of it being changed; without them, a new
    # password is made and a person puts it in.
    password = given or secrets.token_urlsafe(PASSWORD_LENGTH)
    try:
        _run(["domainctl", "database", "add", user, "--database", database, "--dump", dump,
              "--db-user", account, "--password", password, "--replace", "--yes"],
             f"Making the database {database} of {name}", timeout=IMPORT_TIMEOUT)
    except Failed as error:
        return {"ok": False, "error": str(error)}
    made = f"Made the database {database} with the MariaDB user {account}" if account != database else \
        f"Made the database {database} and a MariaDB user of the same name"
    return {"ok": True, "database": database, "user": account, "password": password, "host": "localhost",
            "its_own": bool(given),
            "steps": [f"{made}, and read the dump of {name} into it."]}


HANDLERS = {"hello": hello, "status": status, "apply": apply, "finish_site": finish_site,
            "password": password, "dns": dns, "point_dns": point_dns, "repair": repair,
            "bayes": bayes, "bayes_prepare": bayes_prepare, "bayes_swap": bayes_swap,
            "copy_mail": copy_mail, "copy_mail_status": copy_mail_status,
            "compare_mail": compare_mail, "mail_sizes": mail_sizes,
            "adopt_database": adopt_database}


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
