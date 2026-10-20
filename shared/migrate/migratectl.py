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
import stat
import subprocess
import sys
import tempfile
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


def _run(command, what, stdin=None, env=None):
    # The request pipe is our own standard input: no child may read from it.
    extra = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    if env is not None:
        extra["env"] = env
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, **extra)
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


# The rules of serverctl.names, written out again because migratectl never imports the tools.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_LABEL}")
_USER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _site_fields(item):
    if item.get("kind") != "site":
        raise ValueError(f"I can't use the kind {item.get('kind')!r} here")
    # Like serverctl.names: lower-cased and stripped first, and the cleaned value is what goes on to domainctl.
    # domainctl allows a site domain up to the DNS limit, not the 50 of a mail domain.
    name, user = _text(item, "name").strip().lower(), _text(item, "user").strip().lower()
    if len(name) > 253 or not _DOMAIN.fullmatch(name):
        raise ValueError(f"{name!r} isn't a valid domain name")
    database = item.get("database") or None
    if isinstance(database, str):
        database = database.strip().lower()
    for what, value in (("user", user), ("database", database)):
        if value is not None and not (isinstance(value, str) and len(value) <= 32 and _USER_NAME.fullmatch(value)):
            raise ValueError(f"{value!r} isn't a valid {what} name: use lower case letters, digits, - and _, "
                             f"starting with a letter or digit, at most 32 characters")
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
        steps.append(f"Made the site {name}, its user {user} and its directories.")
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
    return {"ok": True, "docroot": docroot, "database": database, "db_user": database,
            "note": "The database's password is in the site's own wp-config.php, and changes when the dump is read in.",
            "steps": steps}


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
# hash when its scheme is in the list `doveadm pw -l` prints. A readable scheme that is not strong (MD5-CRYPT, PLAIN)
# is "weak": it imports fine. An unreadable one makes `mailctl import` without a terminal refuse everything.
OWN_SCHEME = "SHA512-CRYPT"
STRONG_SCHEMES = {"SHA256-CRYPT", "SHA512-CRYPT", "BLF-CRYPT", "ARGON2I", "ARGON2ID", "PBKDF2", "SCRAM-SHA-256"}
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
    """(needs a password, weak) for the accounts of an import file that are not on this server yet: addresses,
    never hashes. With existing=None every account is judged, which asks for too much rather than too little."""
    accounts = [a for a in data.get("addresses", []) if isinstance(a, dict) and isinstance(a.get("address"), str)
                and (existing is None or a["address"].lower() not in existing)]
    hashed = [a for a in accounts if not a.get("password") and isinstance(a.get("password_hash"), str)
              and a["password_hash"] and _scheme_of(a["password_hash"]) != OWN_SCHEME]
    if not hashed:
        return [], []
    readable = _readable_schemes()
    needs = [a["address"] for a in hashed if _scheme_of(a["password_hash"]) not in readable]
    weak = [a["address"] for a in hashed if _scheme_of(a["password_hash"]) in readable
            and _scheme_of(a["password_hash"]) not in STRONG_SCHEMES]
    return needs, weak


def _said(done):
    return {"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def _apply_mail_domain(item):
    name = _text(item, "name")
    data = item.get("data")
    if not isinstance(data, dict):
        raise ValueError("the item has no data (the JSON for mailctl import)")
    existing = _existing_accounts(name, data)
    needs, weak = _sort_accounts(data, existing)
    result = {"weak": weak}
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
                                  capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                                  stdin=subprocess.DEVNULL)
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


def _apply_mail_passwords(item):
    passwords = item.get("passwords")
    if not isinstance(passwords, dict) or not passwords:
        raise ValueError("the item has no passwords, like {\"passwords\": {address: password}}")
    done, failed = [], {}
    for address, password in passwords.items():
        if not isinstance(address, str) or not isinstance(password, str) or not password:
            failed[str(address)] = "there is no password to set"
            continue
        try:
            _run(["mailctl", "address", "password", address, "--password-stdin"], "Setting the password",
                 stdin=password + "\n")
            done.append(address)
        except Failed as error:
            # The password is only ever on stdin, but keep it out of the words whatever the tool echoes.
            failed[address] = str(error).replace(password, "***")
    if failed:
        return {"ok": False, "done": done, "failed": failed,
                "error": f"The password could not be set for {len(failed)} of {len(passwords)} account(s): "
                         f"{', '.join(failed)}. The other accounts were done."}
    return {"ok": True, "done": done, "steps": [f"Set the password of {a}." for a in done]}


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


APPLIERS = {"site": _apply_site, "mail_domain": _apply_mail_domain, "mail_passwords": _apply_mail_passwords}


def apply(request):
    answers = request.get("answers", {})
    if not isinstance(answers, dict):
        return {"ok": False, "error": "The answers must be a JSON object."}
    try:
        item = _only_item(request)
        kind = item.get("kind")
        if kind == "site":
            return _apply_site(item, answers)
        if kind == "cron":
            return _apply_cron(item)
        if kind in ("mail_domain", "mail_passwords"):
            return APPLIERS[kind](item)
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


HANDLERS = {"hello": hello, "status": status, "apply": apply, "finish_site": finish_site, "dns": dns}


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
