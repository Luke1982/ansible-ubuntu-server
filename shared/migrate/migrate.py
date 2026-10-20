#!/usr/bin/env python3
"""Moves what is on this server to a new one. Copy this one file to the old server and run it there.

It only reads: it changes nothing on this server. Standard library only and nothing newer than Python 3.6, since an
old server has an old Python and none of our other files.
"""

import json
import os
import re
import subprocess

MAIL_TABLES = ("virtual_domains", "virtual_users", "virtual_aliases", "virtual_sender_aliases")
SPAM_DATABASE = "spamassassin"
VERSION = 1  # of the mail file; the one mailctl import reads
VMAIL = "/var/vmail"

# The limits and patterns below are mailctl's own, so a file made here is one its reader takes whole.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN = re.compile(r"(?:%s\.)+(?=[a-z0-9-]*[a-z])%s" % (_LABEL, _LABEL))
_LOCAL_PART = re.compile(r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*")
_PASSWORD_HASH = re.compile(r"(\{[A-Za-z0-9.-]+\}\S+|\$[0-9a-z]+\$\S+)")
_FILTER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
_DOMAIN_ARGUMENT = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")
MAX_DOMAIN, MAX_ADDRESS, MAX_LOCAL_PART, MAX_HASH, MAX_VALUE, MAX_SCRIPT = 50, 100, 64, 150, 100, 1 << 20
DOVEADM_HEADER = "sieve script:\n"


class CommandFailed(Exception):
    """A command that did not run, or ran and failed. 'missing' says the program isn't on this server."""

    def __init__(self, message, missing=False):
        super().__init__(message)
        self.message = message
        self.missing = missing


def run(*args, stdin=None):
    """The output of a command. This is the one place a command is run, so tests and dry runs can stand in for it."""
    try:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise CommandFailed("%s isn't installed" % args[0], missing=True)
    except OSError as problem:
        raise CommandFailed("can't run %s: %s" % (args[0], problem))
    out, err = process.communicate(stdin.encode() if stdin is not None else None)
    if process.returncode != 0:
        said = err.decode("utf-8", "replace").strip() or "it stopped with code %d" % process.returncode
        raise CommandFailed("%s failed: %s" % (args[0], " ".join(said.split())))
    return out.decode("utf-8", "replace")


def _unescape(value):
    """The client's batch mode escapes tabs, line breaks, NULs and backslashes in a value; this undoes it."""
    return re.sub(r"\\([\\nt0])", lambda found: {"n": "\n", "t": "\t", "0": "\0", "\\": "\\"}[found.group(1)], value)


def query(run, sql):
    """The rows of a query, each a list of texts. Tries the mariadb client, then the mysql one."""
    problem = None
    for client in ("mariadb", "mysql"):
        try:
            output = run(client, "--batch", "--skip-column-names", "-e", sql)
        except CommandFailed as failed:
            if not failed.missing:
                raise
            problem = failed
            continue
        return [[_unescape(field) for field in line.split("\t")] for line in output.split("\n") if line]
    raise CommandFailed("neither the mariadb nor the mysql client is installed", missing=True) from problem


def mail_database(run, notes=None):
    """The database that has all four mail tables, or None. What it looked for goes into notes, when given."""
    notes = notes if notes is not None else []
    names = ", ".join("'%s'" % table for table in MAIL_TABLES)
    try:
        rows = query(run, "SELECT table_schema, COUNT(DISTINCT table_name) FROM information_schema.tables "
                          "WHERE table_name IN (%s) GROUP BY table_schema ORDER BY table_schema" % names)
    except CommandFailed as problem:
        notes.append("Couldn't look for the mail database: %s." % problem.message)
        return None
    complete = [row[0] for row in rows if len(row) == 2 and row[1] == str(len(MAIL_TABLES))]
    if not complete:
        notes.append("There is no database with the mail tables (%s), so there is no mail to move."
                     % ", ".join(MAIL_TABLES))
        return None
    if len(complete) > 1:
        notes.append("More than one database has the mail tables (%s); using %s."
                     % (", ".join(complete), "mailserver" if "mailserver" in complete else complete[0]))
    return "mailserver" if "mailserver" in complete else complete[0]


def mail_inventory(run, database, domain=None, mail_root=VMAIL):
    """The mail of this server as the file 'mailctl import' reads, for one domain or all of them.

    Beside the file's own parts it holds 'left_out' (what mailctl would refuse, each with the reason) and 'notes'
    (what couldn't be read). Take those out with file_part() before writing the file.
    """
    if domain is not None and not _DOMAIN_ARGUMENT.fullmatch(domain):
        # It goes into the queries, so it is a domain name and nothing else.
        raise ValueError("'%s' isn't a domain name." % domain)
    inventory = {"version": VERSION, "domains": [], "addresses": [], "forwards": [], "spam": [], "sieve": [],
                 "left_out": [], "notes": []}
    left_out, notes = inventory["left_out"], inventory["notes"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", database or ""):
        notes.append("'%s' isn't a database name." % database)
        return inventory
    wanted = domain.lower() if domain else None
    where = {"domains": "", "users": "", "aliases": "", "senders": ""}
    if wanted:
        where = {
            "domains": "WHERE LOWER(name) = '%s'" % wanted,
            "users": "WHERE LOWER(SUBSTRING_INDEX(email, '@', -1)) = '%s'" % wanted,
            "aliases": "AND LOWER(SUBSTRING_INDEX(source, '@', -1)) = '%s'" % wanted,
            "senders": "WHERE LOWER(SUBSTRING_INDEX(alias, '@', -1)) = '%s'" % wanted,
        }

    def rows(sql, what):
        try:
            return query(run, sql)
        except CommandFailed as problem:
            notes.append("Couldn't read %s: %s." % (what, problem.message))
            return []

    domains = rows("SELECT DISTINCT name FROM %s.virtual_domains %s ORDER BY name" % (database, where["domains"]),
                   "the domains")
    if wanted and not domains:
        notes.append("There's no domain %s on this server." % wanted)
    for row in domains:
        name = row[0].strip().lower()
        if not _valid_domain(name):
            left_out.append("The domain %s: it isn't a valid domain name." % row[0])
        elif name not in inventory["domains"]:
            inventory["domains"].append(name)

    accounts = _accounts(rows("SELECT email, password FROM %s.virtual_users %s ORDER BY email"
                              % (database, where["users"]), "the accounts"), left_out)
    inventory["addresses"] = accounts
    known = {account["address"] for account in accounts}

    senders = rows("SELECT alias, users FROM %s.virtual_sender_aliases %s" % (database, where["senders"]),
                   "who may send as which address")
    inventory["forwards"] = _forwards(rows(
        "SELECT source, destination FROM %s.virtual_aliases WHERE LOWER(source) <> LOWER(destination) %s "
        "ORDER BY source, destination" % (database, where["aliases"]), "the forwards"), senders, left_out)

    inventory["spam"] = _spam(run, wanted, notes, left_out)
    for address in sorted(known):
        for script in filters_of(run, address, mail_root, notes):
            inventory["sieve"].append(dict(script, address=address))
    _one_active_filter(inventory["sieve"], left_out)
    for entry in list(inventory["sieve"]):
        reason = _filter_problem(entry)
        if reason:
            inventory["sieve"].remove(entry)
            left_out.append("The filter %s of %s: %s" % (entry["name"], entry["address"], reason))
    return inventory


def file_part(inventory):
    """The inventory as the file itself: without the reports that only the person running this reads."""
    return {key: inventory[key] for key in ("version", "domains", "addresses", "forwards", "spam", "sieve")}


def _valid_domain(name):
    return len(name) <= MAX_DOMAIN and _DOMAIN.fullmatch(name) is not None


def _address_problem(value):
    """Why mailctl would refuse this address, in plain words; None when it takes it."""
    address = value.strip().lower()
    local_part, _, domain_part = address.rpartition("@")
    if "@" not in address:
        return "it has no @."
    if not local_part:
        return "it has no part before the @ (a catch-all address), which mailctl can't set up."
    if len(address) > MAX_ADDRESS or len(local_part) > MAX_LOCAL_PART or not _LOCAL_PART.fullmatch(local_part):
        return "it isn't an address mailctl accepts (letters, digits, dots, dashes, underscores and plus signs)."
    if not _valid_domain(domain_part):
        return "its domain isn't a valid domain name."
    return None


def _accounts(rows, left_out):
    accounts, seen = [], set()
    for row in rows:
        if len(row) != 2:
            continue
        address, password_hash = row[0].strip().lower(), row[1]
        reason = _address_problem(address)
        if reason is None and address in seen:
            reason = "the same account is there twice."
        if reason is None and (len(password_hash) > MAX_HASH or not _PASSWORD_HASH.fullmatch(password_hash)):
            reason = "its password isn't stored in a form Dovecot reads."
        if reason:
            left_out.append("The account %s: %s" % (row[0], reason))
            continue
        seen.add(address)
        accounts.append({"address": address, "password_hash": password_hash})
    return accounts


def _forwards(rows, senders, left_out):
    # Postfix keeps who may send as an address as a list of logins; a forward may send as its source when its
    # destination is in that list.
    allowed = {}
    for row in senders:
        if len(row) == 2:
            allowed[row[0].strip().lower()] = set(part for part in re.split(r"[\s,]+", row[1].lower()) if part)
    forwards, seen = [], set()
    for row in rows:
        if len(row) != 2:
            continue
        source, destination = row[0].strip().lower(), row[1].strip().lower()
        reason = None
        for role, address in (("source", source), ("destination", destination)):
            problem = _address_problem(address)
            if problem:
                reason = "its %s %s: %s" % (role, address or "(empty)", problem)
                break
        if reason:
            left_out.append("The forward from %s to %s: %s" % (row[0], row[1], reason))
        elif (source, destination) not in seen:
            seen.add((source, destination))
            forwards.append({"source": source, "destination": destination,
                             "send_as": destination in allowed.get(source, ())})
    return forwards


def _spam(run, wanted, notes, left_out):
    try:
        has_database = bool(query(run, "SHOW DATABASES LIKE '%s'" % SPAM_DATABASE))
    except CommandFailed as problem:
        notes.append("Couldn't look for the spam settings: %s." % problem.message)
        return []
    if not has_database:
        notes.append("There's no database %s, so there are no spam settings to move." % SPAM_DATABASE)
        return []
    try:
        rows = query(run, "SELECT username, preference, value FROM %s.userpref ORDER BY prefid" % SPAM_DATABASE)
    except CommandFailed as problem:
        notes.append("Couldn't read the spam settings: %s." % problem.message)
        return []
    settings = []
    for row in rows:
        if len(row) != 3:
            continue
        username, setting, value = row
        # The whole server's settings are under $GLOBAL, or @GLOBAL when set up by hand; a domain's under %domain.
        target = "server" if username in ("$GLOBAL", "@GLOBAL") else username.lstrip("%").strip().lower()
        if target != "server" and not (_address_problem(target) is None if "@" in target else _valid_domain(target)):
            left_out.append("The spam setting %s for %s: it's not for the server, an address or a domain."
                            % (setting, username))
            continue
        if wanted and not (target == wanted or target.endswith("@" + wanted)):
            continue  # the server's settings belong to the server, not to one domain
        if not setting.strip() or len(setting) > MAX_VALUE or len(value) > MAX_VALUE:
            left_out.append("The spam setting %s for %s: it is empty or longer than %d characters."
                            % (setting, username, MAX_VALUE))
            continue
        settings.append({"target": target, "setting": setting, "value": value})
    return settings


def filters_of(run, address, mail_root=VMAIL, notes=None):
    """The Sieve filters of an account as {name, active, content}: from doveadm, or when that can't, from the
    account's sieve folder."""
    notes = notes if notes is not None else []
    try:
        return _filters_by_doveadm(run, address)
    except CommandFailed as problem:
        found = _filters_in_folder(address, mail_root)
        if found is None:
            if not problem.missing:
                notes.append("Couldn't read the filters of %s: %s." % (address, problem.message))
            elif not any(note.startswith("doveadm isn't installed") for note in notes):
                notes.append("doveadm isn't installed and %s has no sieve folder under %s, so filters can't be read."
                             % (address, mail_root))
            return []
        return found


def _filters_by_doveadm(run, address):
    scripts = []
    for line in run("doveadm", "sieve", "list", "-u", address).split("\n"):
        if not line.strip():
            continue
        name = line[:-len(" ACTIVE")] if line.endswith(" ACTIVE") else line
        content = run("doveadm", "sieve", "get", "-u", address, name)
        if content.startswith(DOVEADM_HEADER):
            content = content[len(DOVEADM_HEADER):]  # doveadm's field header, not part of the script
        scripts.append({"name": name, "active": name != line, "content": content})
    return scripts


def _filters_in_folder(address, mail_root):
    local_part, _, domain_part = address.rpartition("@")
    home = os.path.join(mail_root, domain_part, local_part)
    folder = os.path.join(home, "sieve")
    if not os.path.isdir(folder):
        return None
    active = os.path.realpath(os.path.join(home, ".dovecot.sieve"))
    scripts = []
    for file_name in sorted(os.listdir(folder)):
        path = os.path.join(folder, file_name)
        if not file_name.endswith(".sieve") or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            scripts.append({"name": file_name[:-len(".sieve")], "active": os.path.realpath(path) == active,
                            "content": handle.read()})
    return scripts


def _filter_problem(entry):
    if not _FILTER_NAME.fullmatch(entry["name"]):
        return "its name isn't one mailctl accepts (letters, digits, spaces, dots, dashes and underscores, up to 64)."
    if len(entry["content"].encode("utf-8")) > MAX_SCRIPT:
        return "it is bigger than %d KB." % (MAX_SCRIPT // 1024)
    if "\0" in entry["content"]:
        return "it holds NUL characters, so it isn't a Sieve script."
    return None


def _one_active_filter(scripts, left_out):
    """An account has one active filter in mailctl; the first stays active and the others are kept, switched off."""
    active = set()
    for script in scripts:
        if script["active"] and script["address"] in active:
            script["active"] = False
            left_out.append("The filter %s of %s stays but is not switched on: an account has one active filter."
                            % (script["name"], script["address"]))
        elif script["active"]:
            active.add(script["address"])
