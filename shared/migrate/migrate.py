#!/usr/bin/env python3
"""Moves what is on this server to a new one. Copy this one file to the old server and run it there.

It only reads: it changes nothing on this server. Standard library only and nothing newer than Python 3.6, since an
old server has an old Python and none of our other files.
"""

import argparse
import copy
import getpass
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap

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
# domainctl's own rule for the name of a site's Linux user and of its database, which migratectl checks too.
_NEW_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
MAX_DOMAIN, MAX_ADDRESS, MAX_LOCAL_PART, MAX_HASH, MAX_VALUE, MAX_SCRIPT = 50, 100, 64, 150, 100, 1 << 20
SPAM_RENAMED = {"whitelist_from": "welcomelist_from", "blacklist_from": "blocklist_from"}  # SpamAssassin before 4
DOVEADM_HEADER = "sieve script:\n"
SITES_ENABLED = "/etc/apache2/sites-enabled"
APACHE_ROOT = "/etc/apache2"
# Of the talk with migratectl on the new server; both halves must come from the same release. It goes up whenever
# the requests or the answers change, so a mismatch is found at the handshake and not in the middle of a site.
PROTOCOL_VERSION = 2
KEY_NAME = "migrate_key"
KNOWN_HOSTS_NAME = "migrate_known_hosts"
AUTHORIZED_KEYS = "/home/ansible/.ssh/authorized_keys"
STATE_PATH = "/var/lib/migrate/state.json"
MAX_USER_NAME = 32  # the limit of Linux's user database, as in serverctl.names


class CommandFailed(Exception):
    """A command that did not run, or ran and failed. 'missing' says the program isn't on this server."""

    def __init__(self, message, missing=False):
        super().__init__(message)
        self.message = message
        self.missing = missing


LOOK_TIMEOUT = 120  # plenty for any command that only looks something up, and short enough to say so


def run_command(*args, stdin=None, env=None, timeout=LOOK_TIMEOUT):
    """The output of a command. This is the one place a command is run, so tests and dry runs can stand in for it.
    'env' is added to this program's own environment for that one command (a password goes here, never in args).

    A command that does not answer within 'timeout' seconds is killed and told about, like one that isn't there at
    all: a mysql client whose my.cnf points at a machine that drops packets, or doveadm on a mailbox on a dead
    mount, would otherwise hang the whole tool with nothing on the screen at all. rsync and the dump have no
    limit (run_visible and run_piped): those legitimately run for hours, and show their own progress."""
    try:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(os.environ, **env) if env else None)
    except FileNotFoundError:
        raise CommandFailed("%s isn't installed" % args[0], missing=True)
    except OSError as problem:
        raise CommandFailed("can't run %s: %s" % (args[0], problem))
    try:
        out, err = process.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            # Reaps the killed command. With a limit of its own: a child of its own can keep the pipes open, and
            # waiting here forever is the thing we just stopped doing.
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise CommandFailed("%s didn't answer within %d seconds, so it was not looked at" % (args[0], timeout))
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
    """The mail of this server as a Mail: .file is what 'mailctl import' reads, for one domain or all of them, and
    .left_out and .notes are the reports. They are kept apart so the file can be written as it is."""
    if domain is not None and not _DOMAIN_ARGUMENT.fullmatch(domain):
        # It goes into the queries, so it is a domain name and nothing else.
        raise ValueError("'%s' isn't a domain name." % domain)
    inventory = {"version": VERSION, "domains": [], "addresses": [], "forwards": [], "spam": [], "sieve": []}
    left_out, notes = [], []
    result = Mail(inventory, left_out, notes)
    if not re.fullmatch(r"[A-Za-z0-9_]+", database or ""):
        notes.append("'%s' isn't a database name." % database)
        return result
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
    no_forward = []  # said last, after the rest of what was left out
    inventory["forwards"] = _forwards(rows(
        "SELECT source, destination FROM %s.virtual_aliases WHERE LOWER(source) <> LOWER(destination) %s "
        "ORDER BY source, destination" % (database, where["aliases"]), "the forwards"), senders, left_out,
        no_forward)

    inventory["spam"] = _spam(run, wanted, notes, left_out)
    folders = []
    for address in sorted(known):
        for script in filters_of(run, address, mail_root, notes, folders):
            inventory["sieve"].append(dict(script, address=address))
    if folders:
        notes.append("The filters of %s were read from %s/<domain>/<user>/sieve, since doveadm couldn't give them. "
                     "An account that keeps its filters somewhere else shows none." % (", ".join(folders), mail_root))
    _one_active_filter(inventory["sieve"], left_out)
    for entry in list(inventory["sieve"]):
        reason = _filter_problem(entry)
        if reason:
            inventory["sieve"].remove(entry)
            left_out.append("The filter %s of %s: %s" % (entry["name"], entry["address"], reason))
    left_out.extend(no_forward)
    return result


class Mail(object):
    """What was found: the file mailctl reads, and apart from it what was left out of it and what couldn't be read.
    A report inside the file would make mailctl refuse it whole, so the reports are not in .file."""

    def __init__(self, file, left_out, notes):
        self.file = file
        self.left_out = left_out
        self.notes = notes


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


def _forwards(rows, senders, left_out, last=None):
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
    # The file has a place for this permission only as part of a forward, so one without a forward is named.
    last = left_out if last is None else last
    for source in sorted(allowed):
        for login in sorted(allowed[source]):
            if login != source and (source, login) not in seen:
                last.append("%s may send as %s, and no forward goes with it" % (login, source))
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
        reason = _spam_problem(setting, value)
        if reason:
            left_out.append("The spam setting %s for %s: %s" % (setting, username, reason))
            continue
        settings.append({"target": target, "setting": setting, "value": value})
    return settings


def _spam_problem(setting, value):
    """Why mailctl's reader would refuse this setting, in plain words; None when it takes it."""
    name = setting.strip().lower()
    name = SPAM_RENAMED.get(name, name)
    if name == "required_score":
        if not re.fullmatch(r"[+-]?\d+(\.\d+)?", value.strip()):
            return "'%s' isn't a number." % value.strip()
    elif name in ("welcomelist_from", "blocklist_from"):
        for pattern in value.split():
            if len(pattern) > MAX_VALUE or not re.fullmatch(r"\S*[@.]\S*", pattern):
                return "'%s' isn't an address or pattern like *@example.nl." % pattern
    elif not name or len(name) > MAX_VALUE or len(value) > MAX_VALUE:
        return "it is empty or longer than %d characters." % MAX_VALUE
    return None


def filters_of(run, address, mail_root=VMAIL, notes=None, folders=None):
    """The Sieve filters of an account as {name, active, content}: from doveadm, or when that can't, from the
    account's sieve folder. An account read from its folder is added to 'folders', so one note can say so."""
    notes = notes if notes is not None else []
    folders = folders if folders is not None else []
    try:
        return _filters_by_doveadm(run, address)
    except CommandFailed as problem:
        reason = problem.message
    try:
        found = _filters_in_folder(address, mail_root, notes)
    except (OSError, UnicodeError) as problem:
        notes.append("Couldn't read the filters of %s: %s." % (address, problem))
        return []
    if found is None:
        notes.append("Couldn't read the filters of %s: %s, and there is no folder %s." % (
            address, reason, _sieve_folder(address, mail_root)))
        return []
    folders.append(address)
    if not found:
        notes.append("The sieve folder of %s is there but holds no scripts (*.sieve)." % address)
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


def _sieve_folder(address, mail_root):
    local_part, _, domain_part = address.rpartition("@")
    return os.path.join(mail_root, domain_part, local_part, "sieve")


def _filters_in_folder(address, mail_root, notes):
    folder = _sieve_folder(address, mail_root)
    if not os.path.isdir(folder):
        return None
    active_file = os.path.join(os.path.dirname(folder), ".dovecot.sieve")
    active = os.path.realpath(active_file)
    scripts = []
    for file_name in sorted(os.listdir(folder)):
        path = os.path.join(folder, file_name)
        if not file_name.endswith(".sieve") or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            scripts.append({"name": file_name[:-len(".sieve")], "active": os.path.realpath(path) == active,
                            "content": handle.read()})
    if os.path.lexists(active_file) and not any(script["active"] for script in scripts):
        notes.append("%s is not one of the scripts in the sieve folder, so no filter of %s is switched on."
                     % (active_file, address))
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


# Where apache2ctl -S says a virtual host is: "port 443 namevhost example.nl (/etc/apache2/sites-enabled/x.conf:2)"
# (Apache 2.4), or the same without "port 443 namevhost" in front of the name (Apache 2.2).
# A port with a single virtual host has "*:443" in front of the name instead, not indented.
_VHOST_LINE = re.compile(r"^\s*(?:port\s+\d+\s+namevhost\s+|\S+:\d+\s+)?(\S+)\s+\((.+):(\d+)\)\s*$")
_VHOST_ALIAS = re.compile(r"^\s+alias\s+(\S+)\s*$")

# The quoted text of a PHP string, with its escapes still in it.
_QUOTED = r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""
_WP_DEFINE = r"""^[ \t]*define[ \t]*\([ \t]*['"]%s['"][ \t]*,[ \t]*""" + _QUOTED + r"""[ \t]*\)[ \t]*;"""
_WP_PREFIX = re.compile(r"^[ \t]*\$table_prefix[ \t]*=[ \t]*" + _QUOTED + r"[ \t]*;", re.MULTILINE)
WP_SETTINGS = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST")


def _php_text(found):
    """The text of a match of _QUOTED, with \\' \\" and \\\\ unescaped like move-wordpress.sh and domainctl do."""
    text = found.group(1) if found.group(1) is not None else found.group(2)
    return re.sub(r"\\([\\'\"])", r"\1", text)


def wp_settings(path):
    """DB_NAME, DB_USER, DB_PASSWORD and DB_HOST from a wp-config.php, and its $table_prefix as 'prefix'. A setting
    the file doesn't have is None. None when the file can't be read at all: that is not the same as 'has no
    settings', and the caller must not treat it so. DB_HOST is kept as it is: host, host:port or host:/socket."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    settings = {}
    for name in WP_SETTINGS:
        # The first define() wins, as in move-wordpress.sh.
        found = re.search(_WP_DEFINE % name, text, re.MULTILINE)
        settings[name] = _php_text(found) if found else None
    prefix = _WP_PREFIX.search(text)
    settings["prefix"] = _php_text(prefix) if prefix else None
    return settings


def database_sizes(run, notes=None):
    """Bytes per database (tables and indexes), as {name: bytes}. Empty, with a note, when the server can't say."""
    notes = notes if notes is not None else []
    try:
        rows = query(run, "SELECT table_schema, COALESCE(SUM(data_length + index_length), 0) "
                          "FROM information_schema.tables GROUP BY table_schema")
    except CommandFailed as problem:
        notes.append("Couldn't measure the databases: %s." % problem.message)
        return {}
    return {row[0]: int(row[1]) for row in rows if len(row) == 2 and row[1].isdigit()}


def _vhost_directives(lines):
    """ServerName, ServerAlias names and DocumentRoot of the lines of one <VirtualHost> block."""
    name, aliases, docroot = None, [], None
    for line in lines:
        words = line.split("#", 1)[0].split()
        if len(words) < 2:
            continue
        directive, arguments = words[0].lower(), [word.strip("\"'") for word in words[1:]]
        if directive == "servername" and name is None:
            name = arguments[0].lower()
        elif directive == "serveralias":
            aliases.extend(argument.lower() for argument in arguments)
        elif directive == "documentroot" and docroot is None:
            docroot = arguments[0] if os.path.isabs(arguments[0]) else os.path.join(APACHE_ROOT, arguments[0])
    return name, aliases, docroot


def _vhost_block(lines, start):
    """The lines inside the <VirtualHost> that begins at line number 'start' (counting from 1)."""
    block = []
    for line in lines[start:]:
        if line.strip().lower().startswith("</virtualhost"):
            break
        block.append(line)
    return block


def _vhosts_from_apache(output, notes):
    """[{name, aliases, docroot, problem}] from 'apache2ctl -S', each file read at the line it names."""
    found, order, files = {}, [], {}
    current = None
    for line in output.split("\n"):
        vhost = _VHOST_LINE.match(line)
        alias = _VHOST_ALIAS.match(line)
        if vhost and "default server" not in line and "is a NameVirtualHost" not in line:
            name = vhost.group(1).lower()
            current = found.get(name)
            if current is None:
                current = found[name] = {"name": name, "aliases": [], "docroot": None, "problem": None}
                order.append(name)
            path, number = vhost.group(2), int(vhost.group(3))
            if current["docroot"] is None and current["problem"] is None:
                try:
                    if path not in files:
                        with open(path, encoding="utf-8", errors="replace") as handle:
                            files[path] = handle.read().split("\n")
                except OSError as problem:
                    current["problem"] = "Couldn't read %s, so the site's directory isn't known: %s." % (path, problem)
                    continue
                _, aliases, docroot = _vhost_directives(_vhost_block(files[path], number))
                current["docroot"] = docroot
                current["aliases"].extend(alias_name for alias_name in aliases if alias_name not in current["aliases"])
        elif alias and current is not None:
            if alias.group(1).lower() not in current["aliases"]:
                current["aliases"].append(alias.group(1).lower())
    return [found[name] for name in order]


def _vhosts_from_files(sites_dir, notes):
    """The same, without Apache's help: every *.conf in sites-enabled read for its <VirtualHost> blocks."""
    vhosts = {}
    for path in sorted(glob.glob(os.path.join(sites_dir, "*.conf"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().split("\n")
        except OSError as problem:
            notes.append("Couldn't read %s: %s." % (path, problem))
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip().lower().startswith("<virtualhost"):
                continue
            name, aliases, docroot = _vhost_directives(_vhost_block(lines, number))
            if name is None:
                notes.append("A virtual host in %s (line %d) has no ServerName, so it is left out." % (path, number))
                continue
            site = vhosts.setdefault(name, {"name": name, "aliases": [], "docroot": None, "problem": None})
            site["docroot"] = site["docroot"] or docroot
            site["aliases"].extend(alias for alias in aliases if alias not in site["aliases"])
    return list(vhosts.values())


def sites(run, notes=None, sites_dir=SITES_ENABLED, sizes=None):
    """One dict per site Apache serves: name, aliases, docroot, wordpress, database, db_user, db_password, db_host,
    prefix, size (bytes, or None) and problem (plain words, or None). A site with a problem is not safe to move as
    it is. 'sizes' is database_sizes(), looked up here when not given."""
    notes = notes if notes is not None else []
    try:
        vhosts = _vhosts_from_apache(run("apache2ctl", "-S"), notes)
        if not vhosts:
            raise CommandFailed("it listed no virtual hosts")
    except CommandFailed as failed:
        notes.append("Couldn't get the sites from apache2ctl (%s), so %s was read instead."
                     % (failed.message, sites_dir))
        vhosts = _vhosts_from_files(sites_dir, notes)
    if not vhosts:
        notes.append("No sites were found in Apache's configuration.")
    if sizes is None:
        sizes = database_sizes(run, notes) if vhosts else {}
    for site in vhosts:
        _look_for_wordpress(site, sizes)
    return vhosts


def _look_for_wordpress(site, sizes):
    site.update(wordpress=False, database=None, db_user=None, db_password=None, db_host=None, prefix=None, size=None)
    if site["docroot"] is None:
        site["problem"] = site["problem"] or "Its configuration has no DocumentRoot, so its files can't be found."
        return
    config = os.path.join(site["docroot"], "wp-config.php")
    if not os.path.lexists(config):
        _outside_or_missing_config(site, config)
        return
    site["wordpress"] = True
    found = wp_settings(config)
    if found is None:
        site["problem"] = "%s is there but can't be read, so the database of this site isn't known." % config
        return
    site.update(database=found["DB_NAME"], db_user=found["DB_USER"], db_password=found["DB_PASSWORD"],
                db_host=found["DB_HOST"], prefix=found["prefix"])
    empty = [name for name in ("DB_NAME", "DB_USER", "DB_PASSWORD") if not found[name]]
    if empty:
        site["problem"] = "%s has no value for %s, so the database of this site isn't fully known." % (
            config, ", ".join(empty))
    site["size"] = sizes.get(site["database"])


def _outside_or_missing_config(site, config):
    """A site with no wp-config.php in its directory may still be WordPress. WordPress takes the one a directory up
    when there is no wp-settings.php beside it; and a directory with WordPress's own files has lost its config."""
    docroot = site["docroot"]
    above = os.path.join(os.path.dirname(docroot.rstrip("/")), "wp-config.php")
    if os.path.lexists(above) and not os.path.lexists(os.path.join(os.path.dirname(above), "wp-settings.php")):
        site["wordpress"] = True
        site["problem"] = ("The wp-config.php of this site is %s, outside its web directory. That isn't supported "
                           "yet, so its database isn't known." % above)
    elif os.path.lexists(os.path.join(docroot, "wp-includes", "version.php")):
        site["wordpress"] = True
        site["problem"] = "%s has WordPress's files but no wp-config.php, so its database isn't known." % docroot


CRON_DIR = "/etc/cron.d"
PASSWD = "/etc/passwd"

_CRON_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_CRON_PATH = re.compile(r"""(?<![\w.:/-])/[^\s'"`<>;|&()=,]+""")
_CRON_WORD = re.compile(r"""[^\s'"`<>;|&()]+""")
# php, php7.4, php-cli, ea-php74, lsphp81 and the panel copies of them
_PHP_NAME = re.compile(r"^(?:ea-|ls)?php(?:-?cli)?[0-9.]*(?:-cli)?$")
_CRON_WRAPPERS = ("flock", "env", "nice", "ionice", "timeout", "nohup")
_CRON_FILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")  # cron skips any other name in /etc/cron.d
_OTHER_PROGRAMS = {"wp": "wp-cli", "curl": "curl", "wget": "wget", "bash": "bash", "sh": "sh", "python3": "python3",
                   "python": "python"}


def _cron_users(sites, passwd, notes):
    names = []
    for site in sites:
        try:
            names.append(pwd.getpwuid(os.stat(site["docroot"]).st_uid).pw_name if site.get("docroot") else None)
        except (OSError, KeyError):
            pass
    try:
        with open(passwd) as handle:
            names.extend(line.split(":")[0] for line in handle if line.strip() and not line.startswith("#"))
    except OSError as problem:
        notes.append("Couldn't read %s (%s), so only the owners of the sites' folders were checked for cron jobs."
                     % (passwd, problem.strerror or problem))
    names.append("root")
    seen = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return seen


def _cron_lines(text, with_user):
    """(line, user, command, settings, problem) for each job. NAME=value lines are not jobs but change how the jobs
    below them run (PATH, MAILTO, SHELL, CRON_TZ), so they are handed on with each job. A line that is too short to
    be a job comes back with no command and a problem, so it is told about instead of vanishing. A line starts with
    five time fields or one @word, and in /etc/cron.d a user name follows."""
    settings = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _CRON_ENV.match(line):
            settings.append(line)
            continue
        before = (1 if line.startswith("@") else 5) + (1 if with_user else 0)
        fields = line.split(None, before)
        user = fields[before - 1] if with_user and len(fields) >= before else None
        if len(fields) <= before:
            yield (line, user, None, list(settings),
                   "This line is not a complete cron job (the time, %sor the command is missing), so it can't be "
                   "moved." % ("the user, " if with_user else ""))
            continue
        yield line, user, fields[before], list(settings), None


def _program_in(words):
    """The program a segment of a command starts, skipping settings like PATH=x and wrappers such as flock or nice."""
    index = 0
    while index < len(words):
        word = words[index]
        name = os.path.basename(word)
        if _CRON_ENV.match(word):
            index += 1
        elif name in _CRON_WRAPPERS:
            index += 1
            while index < len(words) and (words[index].startswith("-") or _CRON_ENV.match(words[index])):
                index += 1 + (name in ("nice", "ionice") and words[index] in ("-n", "-c") and index + 1 < len(words))
            # flock takes a lock file, timeout a duration; neither is the program
            if name in ("flock", "timeout") and index < len(words):
                index += 1
        else:
            return word
    return None


def _interpreter(command):
    """What the job runs, as written, or None when it isn't a program this can name. Only the first word of each
    '&&', ';' or '|' part counts (after a wrapper like flock), so 'cd /x && php7.4 y.php' gives the php, while a
    file called php that is only an argument or a redirect target is not taken for a program."""
    command = re.sub(r"\d*[<>]+&?\s*\S*", " ", command)
    for part in re.split(r"&&|\|\||[;|&]", command):
        word = _program_in(_CRON_WORD.findall(part))
        if word is not None:
            name = os.path.basename(word)
            if _PHP_NAME.match(name) or name in _OTHER_PROGRAMS:
                return word
    return None


def _site_of(command, sites):
    best = None
    for path in _CRON_PATH.findall(command):
        path = os.path.normpath(path)
        for site in sites:
            root = (site.get("docroot") or "").rstrip("/")
            if root and (path == root or path.startswith(root + "/")):
                if best is None or len(root) > len(best[0]):
                    best = (root, site["name"])
    return best[1] if best else None


def _cron_job(found, sites):
    line, user, command, settings, problem = found
    interpreter = _interpreter(command) if command is not None else None
    if problem is None and interpreter is None:
        problem = "I can't tell which program this job runs, so it can't be set up for the new server by itself."
    return {"user": user, "line": line, "command": command, "site": _site_of(command or "", sites),
            "interpreter": interpreter, "settings": settings, "problem": problem}


def cron_jobs(run, sites, notes=None, passwd=PASSWD, cron_dir=CRON_DIR):
    """One dict per cron job on the server: user, line (as written), command (the part of the line that is the
    command, so the time fields are never read as paths), settings (the NAME=value lines above it),
    site (the name of the site its paths point
    into, or None), interpreter (as the job writes it, or None) and problem (plain words, or None). Everything
    that couldn't be read goes into notes: a job nobody knows about stops running after the move."""
    notes = notes if notes is not None else []
    jobs = []
    notes.append("/etc/crontab was not read; if it has jobs of its own, they are not in this list.")
    users = _cron_users(sites, passwd, notes)
    for index, user in enumerate(users):
        try:
            output = run("crontab", "-l", "-u", user)
        except CommandFailed as failed:
            if failed.missing:
                notes.append("The crontab command isn't installed, so the personal cron jobs of %s weren't read."
                             % ", ".join(users[index:]))
                break
            if "no crontab for" not in failed.message:
                notes.append("Couldn't read the cron jobs of %s: %s." % (user, failed.message))
            continue
        for found in _cron_lines(output, False):
            jobs.append(_cron_job((found[0], user) + found[2:], sites))
    try:
        names = sorted(os.listdir(cron_dir))
    except FileNotFoundError:
        names = []
    except OSError as problem:
        notes.append("Couldn't read the cron jobs in %s: %s." % (cron_dir, problem.strerror or problem))
        names = []
    ignored = []
    for name in names:
        path = os.path.join(cron_dir, name)
        if not os.path.isfile(path) or name == ".placeholder":
            continue
        if not _CRON_FILE_NAME.match(name):
            ignored.append(name)
            continue
        try:
            with open(path, errors="replace") as handle:
                text = handle.read()
        except OSError as problem:
            notes.append("Couldn't read the cron file %s: %s." % (path, problem.strerror or problem))
            continue
        for found in _cron_lines(text, True):
            jobs.append(_cron_job(found, sites))
    if ignored:
        notes.append("Left out of %s because cron itself ignores a file with such a name: %s."
                     % (cron_dir, ", ".join(ignored)))
    return jobs


class Stopped(Exception):
    """Something stopped the move. The message says what happened and what to do, in words for a person."""

    def __init__(self, message, refused=False):
        super().__init__(message)
        self.message = message
        self.refused = refused  # the new server doesn't accept our key (yet)


def _ssh_directory():
    return os.path.join(os.path.expanduser("~"), ".ssh")


def own_key(directory=None, run=run_command):
    """Makes this tool's own key (ed25519, no passphrase, the private half readable by its owner only) when there is
    none, and returns the public half to paste on the new server. Run again, it gives the same key back."""
    directory = directory or _ssh_directory()
    private = os.path.join(directory, KEY_NAME)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if not os.path.exists(private):
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "migrate", "-f", private)
            os.chmod(private, 0o600)
        if not os.path.exists(private + ".pub"):
            with open(private + ".pub", "w") as handle:
                handle.write(run("ssh-keygen", "-y", "-P", "", "-f", private).strip() + " migrate\n")
        with open(private + ".pub") as handle:
            return handle.read().strip()
    except CommandFailed as problem:
        if problem.missing:
            raise Stopped("I can't make the key this server uses to reach the new one, because ssh-keygen isn't "
                          "installed. Install the OpenSSH client (on Debian or Ubuntu: apt install openssh-client) "
                          "and run me again.")
        raise Stopped("I couldn't make the key this server uses to reach the new one. ssh-keygen said: %s. This "
                      "server's OpenSSH may be too old for this kind of key, or %s may hold a damaged file called "
                      "%s." % (problem.message, directory, KEY_NAME))
    except OSError as problem:
        raise Stopped("I couldn't write the key this server uses to reach the new one in %s: %s. Check that this "
                      "account may write there and try again." % (directory, problem.strerror or "no reason given"))


_UNREACHABLE = ("could not resolve", "no route to host", "connection refused", "timed out", "network is unreachable",
                "name or service not known", "temporary failure in name resolution", "connection closed",
                "connection reset")


class Connection(object):
    """The way to the new server: one ssh process for each request, so nothing can be left hanging between two."""

    def __init__(self, host, user="ansible", key=None):
        self.host = host
        self.user = user
        self.key = key or os.path.join(_ssh_directory(), KEY_NAME)
        self.known_hosts = os.path.join(os.path.dirname(self.key), KNOWN_HOSTS_NAME)
        self.host_check = "accept-new"

    def options(self, host_check=None):
        """The ssh words that choose the key and the known-hosts file, for ssh itself and for rsync's -e."""
        # BatchMode: ssh must never wait for a password, a passphrase or a yes/no that nobody would see.
        return ["-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", "-o", "UserKnownHostsFile=" + self.known_hosts,
                "-o", "StrictHostKeyChecking=" + (host_check or self.host_check)]

    def command(self, *words, host_check=None):
        """The ssh command that runs 'sudo -n' and these words on the new server."""
        return ["ssh"] + self.options(host_check) + ["%s@%s" % (self.user, self.host), "sudo", "-n"] + list(words)

    def _command(self, host_check):
        return self.command("migratectl", host_check=host_check)

    def _once(self, host_check, line, timeout, what):
        # LC_ALL=C: the failures are told apart by ssh's and sudo's English words. Bytes are decoded here, since a
        # banner or a translated message with an odd byte must not become a traceback.
        try:
            process = subprocess.Popen(self._command(host_check), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=dict(os.environ, LC_ALL="C"))
        except FileNotFoundError:
            raise Stopped("ssh isn't installed on this server, so I can't reach the new one. Install the OpenSSH "
                          "client (on Debian or Ubuntu: apt install openssh-client) and run me again.")
        except OSError as problem:
            raise Stopped("I couldn't start ssh to reach the new server: %s." % (problem.strerror or problem))
        try:
            out, err = process.communicate(line.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                # Reaps the killed ssh. With a timeout of its own: a helper from the person's ~/.ssh/config can
                # outlive it and hold the pipes open, and waiting here forever is the thing we just stopped doing.
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise Stopped("The new server didn't finish %s within %d minutes, so I stopped waiting. It may have done "
                          "part of the work. Before trying the same thing again, ask for the status of the new "
                          "server, so nothing is made twice." % (what, timeout // 60))
        return (process.returncode, (out or b"").decode("utf-8", "replace"),
                " ".join((err or b"").decode("utf-8", "replace").split()))

    def ask(self, request, timeout=None):
        """The new server's answer to one request, as a dict. Raises Stopped, and nothing else, when it can't.
        It waits 2 minutes for a quick request and 30 for one that makes something."""
        kind = request.get("request") if isinstance(request, dict) else None
        slow = kind in ("apply", "finish_site")
        timeout = timeout or (1800 if slow else 120)
        what = "making this" if slow else "answering the request '%s'" % kind
        try:
            line = json.dumps(request) + "\n"
        except (TypeError, ValueError):
            raise Stopped("I made a request that can't be sent. That's a mistake in this tool, not in your servers.")
        code, out, err = self._once(self.host_check, line, timeout, what)
        if code != 0 and self.host_check == "accept-new" and re.search(r"accept-new", err, re.I):
            self.host_check = "no"
            print("Note: the ssh on this server is too old to remember the new server's identity, so I accepted it "
                  "without checking. Make sure %s really is your new server: anything typed from now on, "
                  "passwords included, goes to whatever answers there." % self.host)
            code, out, err = self._once(self.host_check, line, timeout, what)
        return self._answer(code, out, err)

    def _answer(self, code, out, err):
        said = " The server said: %s" % err if err else ""
        low = err.lower()
        if code != 0:
            if "host key verification failed" in low or "identification has changed" in low:
                raise Stopped("The new server at %s doesn't look like the one I spoke to before (its identity "
                              "changed). If you rebuilt it on purpose, delete the line for it in %s and run me again; "
                              "if not, stop here and find out why.%s" % (self.host, self.known_hosts, said))
            if "permission denied" in low:
                raise Stopped("The new server doesn't accept this server's key yet.%s" % said, refused=True)
            if any(text in low for text in _UNREACHABLE):
                raise Stopped("I can't reach %s. Check the name or address, that the new server is switched on and "
                              "that nothing blocks ssh (port 22).%s" % (self.host, said))
            if code == 127 or "command not found" in low or ("not found" in low and "migratectl" in low):
                raise Stopped("I reached the new server, but migratectl isn't installed there. Build the new server "
                              "with this repository's playbook first, then run me again.%s" % said)
            if "password is required" in low or "not allowed" in low or "sudoers" in low:
                raise Stopped("I reached the new server, but the %s account may not run migratectl without a "
                              "password. Build the new server with this repository's playbook, then run me "
                              "again.%s" % (self.user, said))
            raise Stopped("The connection to %s failed (ssh stopped with code %d). Check the name, the network and "
                          "that you can log in there with ssh by hand.%s" % (self.host, code, said))
        if not out.strip():
            raise Stopped("The new server answered with nothing. Is migratectl on it the right program?%s" % said)
        answer = None
        lines = [text for text in out.splitlines() if text.strip()]
        try:
            answer = json.loads(lines[-1])  # a login banner may come before the answer
        except (ValueError, RecursionError):
            pass
        if not isinstance(answer, dict):
            raise Stopped("The new server answered with something I can't read, so I stopped. Both servers should "
                          "come from the same release of this repository. If the ansible account on the new server "
                          "prints text when it logs in, remove that.%s" % said)
        return answer


def check(connection, show_key=True):
    """The new server's answer to hello. Raises Stopped when it can't be reached, doesn't accept our key, or speaks
    another version than this file. With show_key=False the person was already shown the key, so it isn't again."""
    try:
        answer = connection.ask({"request": "hello"})
    except Stopped as problem:
        if not problem.refused:
            raise
        if not show_key:
            raise Stopped("%s Add the key shown above, as one line, to %s on the new server (%s@%s), then run me "
                          "again." % (problem.message.strip(), AUTHORIZED_KEYS, connection.user, connection.host))
        try:
            key = own_key(os.path.dirname(connection.key))
        except Stopped:
            key = "(I couldn't read the key; run me again to make it)"
        raise Stopped("%s Add this key, as one line, to %s on the new server (%s@%s), then run me again:\n\n%s\n"
                      % (problem.message.strip(), AUTHORIZED_KEYS, connection.user, connection.host, key))
    if not answer.get("ok"):
        raise Stopped("The new server did not accept the first check: %s" % (answer.get("error") or "no reason given"))
    version = answer.get("version")
    if type(version) is not int:
        raise Stopped("The new server did not say which release of this repository it was built from, so I can't "
                      "tell if it fits this file. Update both servers to the same release and run me again.")
    if version != PROTOCOL_VERSION:
        raise Stopped("The new server was built from a different release of this repository than this file (it "
                      "reports %d, this file is %d). Both halves must come from the same release: copy this file "
                      "again from the release the new server was built with." % (version, PROTOCOL_VERSION))
    return answer


def _typed_password(prompt):
    """A password typed without it showing. getpass asks on /dev/tty, so a redirected standard input is fine; with
    no terminal at all it would print what was typed, so there we leave the account out instead."""
    try:
        terminal = open("/dev/tty")
    except (IOError, OSError):
        print("There is no terminal here to type a password on, so this account is left out. To type the passwords, "
              "run me from a terminal on this server.")
        return ""
    terminal.close()
    try:
        return getpass.getpass(prompt)
    except EOFError:
        return ""


def _tell_note(answer, say, told):
    """Says what the new server said about an item, once. When it could not tell which accounts it already has,
    this note is the only reason the person is given for being asked to type a password for an account that may
    work there already."""
    note = answer.get("note") if isinstance(answer, dict) else None
    if isinstance(note, str) and note.strip() and note not in told:
        told.add(note)
        _say(note, say)


def apply_item(connection, item, ask_password=None, say=print, answers=None):
    """Sends one item to the new server to be made. Returns (answer, skipped). When the answer says some mail
    accounts need a password the new server can't get from the stored one, each is asked for here (nothing shows on
    screen), and the same item is sent again with it. Enter skips an account: it is left out and named in 'skipped'.
    A password goes nowhere but the one request: not into the state file, a command line or a message.
    'answers' are the replies to the new server's questions about this item ({about: "yes" or "no"}); they go along
    with every request."""
    ask_password = ask_password or _typed_password
    item = copy.deepcopy(item)
    skipped, asked, told = [], set(), set()
    request = {"request": "apply", "item": item}
    if answers:
        request["answers"] = dict(answers)
    answer = connection.ask(request)
    _tell_note(answer, say, told)
    while not answer.get("ok") and answer.get("needs_password"):
        wanted = [name for name in answer["needs_password"] if isinstance(name, str)]
        if not wanted or asked.intersection(wanted):
            return answer, skipped  # asked for the same account twice: asking again would not help
        say("The new server can't use the stored password of %d account(s). Type each one's password "
            "(nothing shows while you type), or press Enter to leave that account out." % len(wanted))
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        for address in wanted:
            asked.add(address)
            password = ask_password("Password for %s: " % address)
            accounts = data.get("addresses", [])
            if password:
                for account in accounts:
                    if isinstance(account, dict) and str(account.get("address", "")).lower() == address.lower():
                        account["password"] = password
                        account.pop("password_hash", None)
            else:
                skipped.append(address)
                data["addresses"] = [account for account in accounts if not (
                    isinstance(account, dict) and str(account.get("address", "")).lower() == address.lower())]
                # mailctl refuses a file with filters for an account that isn't in it, and would set spam values
                # for the address; so both go with the account, as in mailctl's own skipping.
                data["sieve"] = [entry for entry in data.get("sieve", []) if not (
                    isinstance(entry, dict) and str(entry.get("address", "")).lower() == address.lower())]
                data["spam"] = [entry for entry in data.get("spam", []) if not (
                    isinstance(entry, dict) and str(entry.get("target", "")).lower() == address.lower())]
        answer = connection.ask(request)
        _tell_note(answer, say, told)
    return answer, skipped


KIND_NAMES = {"mail_domain": "Mail", "site": "Website", "cron": "Cron job"}
_WIDTH = 79  # one less than a terminal's 80, so no line wraps by itself


def _title(item):
    name = item.get("name")
    if not name and item.get("kind") == "cron":
        name = "%s: %s" % (item.get("user"), item.get("line"))
    return str(name or item.get("id") or "?")


def _size(count):
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        return None
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1000 or unit == "GB":
            return "%d %s" % (count, unit) if unit == "bytes" else "%.1f %s" % (count, unit)
        count /= 1000.0


def _plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def cost(item):
    """What moving this item will cost, in words; from what the inventory already carries."""
    kind = item.get("kind")
    if kind == "site":
        if not item.get("database"):
            return "the site's files"
        size = _size(item.get("size"))
        return "files and a database of %s" % size if size else "files and a database"
    if kind == "mail_domain":
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        accounts, forwards = data.get("addresses"), data.get("forwards")
        parts = []
        if isinstance(accounts, list):
            parts.append(_plural(len(accounts), "mailbox", "mailboxes"))
        if isinstance(forwards, list):
            parts.append(_plural(len(forwards), "forward", "forwards"))
        return " and ".join(parts) or "the mail settings"
    if kind == "cron":
        return "one job"
    return ""


MAIL_MERGES = ("Sending it again adds the accounts, forwards, filters and "
               "spam settings that are missing, and leaves what is already "
               "there alone.")


def _verdict(item, status, notes=None):
    """(what the new server said in short, can it move, the longer reason or None). 'notes' is the State: a site
    it remembers as made but not filled is offered again, for its files and database only, and anything it
    remembers as moved is not offered again at all (a site that moved is caught up with --again)."""
    said = status.get(item.get("id")) if isinstance(status, dict) else None
    state = said.get("state") if isinstance(said, dict) else None
    detail = said.get("detail") if isinstance(said, dict) else None
    if notes is not None and notes.entry(item).get("status") == "moved":
        return "already moved from here", False, None
    if state == "exists" and notes is not None and notes.outstanding(item):
        return "made, files still to come", True, None
    if state == "exists" and item.get("kind") == "mail_domain":
        # Importing a mail domain only adds what is missing, which is how a
        # domain that is half there is finished. That is why the design asks
        # no question for it, so it must stay on the list.
        return ("already there, can be sent again", True,
                ("%s %s" % (detail or "", MAIL_MERGES)).strip())
    if state == "exists":
        return "already on the new server", False, detail
    if state == "refused":
        return "can't be moved", False, detail
    if state != "new":
        return "the new server didn't say", False, "The new server gave no answer about this."
    if item.get("problem"):
        return "needs a look first", False, item["problem"]
    return cost(item), True, None


def offered(inventory, status, notes=None):
    """The items that can be moved, in the order the dashboard numbers them."""
    return [item for item in inventory if _verdict(item, status, notes)[1]]


def _fit(text, width, middle=False):
    """Text cut to the width. A name is cut in the middle (middle=True): its end tells two names apart."""
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    if middle:
        head = (width - 3 + 1) // 2
        return text[:head] + "..." + text[len(text) - (width - 3 - head):]
    return text[:width - 3] + "..."


def dashboard(inventory, status, remembered=None):
    """The list as it is printed. 'status' is what the new server said, by item id: {id: {"state", "detail"}}.
    Only an item that can be moved gets a number (the same order as offered()); the others show why not. What
    the new server said about an item that can still be moved is shown too, under a heading of its own: it is
    not a reason against it."""
    rows, why_not, already, number = [], [], [], 0
    for item in inventory:
        info, movable, reason = _verdict(item, status, remembered)
        label = ""
        if movable:
            number += 1
            label = str(number)
        rows.append("%3s  %-8s %-28s %s" % (label, KIND_NAMES.get(item.get("kind"), "Other"),
                                           _fit(_title(item), 28, True), _fit(info, 36)))
        if reason:
            (already if movable else why_not).append("%s: %s" % (_title(item), reason))
    if not rows:
        return "Nothing was found on this server to move."
    out = ["No.  What     Name                         What it takes"]
    out.extend(row.rstrip() for row in rows)
    for title, notes in (("Why some can't be moved:", why_not),
                         ("What the new server has already:", already)):
        if not notes:
            continue
        out.append("")
        out.append(title)
        for note in notes:
            out.extend(textwrap.wrap(note, _WIDTH, initial_indent="  ", subsequent_indent="    ",
                                     break_long_words=True))
    return "\n".join(_fit_line(line) for line in out)


def _fit_line(line):
    return line if len(line) <= _WIDTH else line[:_WIDTH - 3] + "..."


def pick(lines, typed, say=print):
    """The items that what was typed comes down to. 'lines' are the offered items (offered()); a number is the
    place of an item in it, counted from 1. Numbers (1 3), ranges (2-5), commas and 'all' are understood. A number
    that is not on the list is never acted on, and is said so."""
    chosen, unreadable, notes = set(), [], []
    words = str(typed).lower().replace(",", " ").split()
    if not words:
        say("You typed nothing, so nothing is picked.")
    for word in words:
        if word == "all":
            chosen.update(range(1, len(lines) + 1))
            continue
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", word)
        if not match:
            unreadable.append(word)
            continue
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if last < first:
            first, last = last, first
            notes.append("%s is backwards, so it is taken as %d-%d." % (word, first, last))
        low, high = max(first, 1), min(last, len(lines))
        chosen.update(range(low, high + 1))
        if low > high:
            notes.append("%s is not on the list, so nothing is picked for it." % word)
        elif (low, high) != (first, last):
            outside = [str(number) if number == other else "%d-%d" % (number, other)
                       for number, other in ((first, low - 1), (high + 1, last)) if number <= other]
            notes.append("The list has %s, so %s is taken as %s. Not on the list: %s." % (
                _plural(len(lines), "item", "items"), word, "%d-%d" % (low, high) if low != high else str(low),
                ", ".join(outside)))
    if unreadable:
        say("I don't understand: %s. Type numbers like 1 3, a range like 2-5, or all." % ", ".join(unreadable))
    for note in notes:
        say(note)
    return [lines[number - 1] for number in sorted(chosen)]


class Report(object):
    """What a run did, as values. moved holds (item, answer) pairs of what is completely done; failed holds
    (item, message) and skipped (item, why). already holds (item, answer) for what the new server found it had
    done before, so nothing was changed for it. files_to_copy are sites that were made on the new server but
    whose files and database are NOT brought over yet (no move was given, or it failed); such a site is never in
    moved. left_out are the mail accounts that got no password. stopped is the message that ended the run early,
    and not_tried the items after it."""

    def __init__(self):
        self.moved, self.skipped, self.failed = [], [], []
        self.already = []
        self.files_to_copy, self.left_out = [], []
        self.stopped, self.not_tried = None, []
        self.interrupted = False  # the person pressed Ctrl-C


def _apply_with_questions(connection, item, ask, report):
    """(answer, question nobody answered or None). Each question from the new server goes to 'ask', which gives
    "yes" or "no"; any other reply (or none) means nobody answered."""
    answers = {}
    while True:
        answer, left_out = apply_item(connection, item, answers=answers)
        report.left_out.extend(left_out)
        question = answer.get("ask") if not answer.get("ok") else None
        if not isinstance(question, dict):
            return answer, None
        about = question.get("about")
        if about in answers:
            return answer, None
        reply = ask(question.get("question") or "The new server has a question about %s." % _title(item))
        reply = reply.strip().lower() if isinstance(reply, str) else None
        if reply not in ("yes", "no"):
            return answer, question
        answers[about] = reply


MADE_NOT_FILLED = ("The site was made, but its files and database were not "
                   "brought over")
NEEDS_PROGRAM = ("%s isn't installed on this server, and a site's files and "
                 "database can't be brought over without it. Nothing was "
                 "made for %s: install it (on Debian or Ubuntu: apt install "
                 "%s) and run me again.")


def _missing_programs(item, which=None):
    """The programs this server needs to bring a site over that aren't here.
    Looked for before the site is made on the new server: finding out
    afterwards leaves a site made there with nothing in it."""
    if item.get("kind") != "site":
        return []
    which = which or shutil.which
    wanted = ["rsync"] + (["mysqldump"] if item.get("database") else [])
    return [name for name in wanted if not which(name)]


def _say(text, say=print):
    say("\n".join(textwrap.wrap(str(text), _WIDTH)) or "")


def _new_web_root(item, docroots, notes):
    """Where the files of a cron job's site are on the new server: what this
    run was told when the site was made, or what the notes remember of the
    run that made it."""
    name = item.get("site")
    if name in docroots:
        return docroots[name]
    if notes is not None and item.get("site_id"):
        return notes.entry({"id": item["site_id"]}).get("docroot")
    return None


def run(connection, items, ask, move=None, notes=None, status=None, php=None,
        extra_notes=None):
    """Moves the items one by one and returns a Report. 'ask' is called with
    the new server's question and gives "yes" or "no". 'move' is called with
    a site and the new server's answer to its apply, after it was made, to
    bring its files and database. One item going wrong stops that item only;
    the new server not answering ends the run. A site that was made but not
    filled is in files_to_copy, whatever went wrong after it was made.

    'notes' is the State: it is told what was made and what is completely
    moved, and it must be writable, or nothing is moved. 'status' is what the
    new server said in this run: a site is not made a second time only when
    the new server says it is there and the notes say it was never filled.
    'php' is the PHP the new server says it has: a cron job is rewritten to
    run that one, with its paths under the new web root, before it is sent,
    and what changed is put in 'extra_notes' for the report."""
    report = Report()
    if notes is not None and items:
        problem = notes.unwritable()
        if problem:
            report.stopped = problem
            report.not_tried = list(items)
            print(problem)
            return report
    docroots = {}  # where this run put each site's files, for the jobs of that site
    for index, item in enumerate(items):
        print("Moving %d of %d: %s" % (index + 1, len(items), _title(item)))
        made = False  # a site the new server made, whose files are not there yet
        try:
            if item.get("kind") == "cron":
                try:
                    item, changed = cron_for_new_server(
                        item, _new_web_root(item, docroots, notes), php)
                except MoveFailed as problem:
                    report.skipped.append((item, problem.message))
                    _say(problem.message)
                    continue
                for text in changed:
                    if extra_notes is not None and text not in extra_notes:
                        extra_notes.append(text)
            missing = _missing_programs(item)
            if missing:
                why = NEEDS_PROGRAM % (", ".join(missing), _title(item),
                                       " ".join(missing))
                report.skipped.append((item, why))
                _say(why)
                continue
            left = notes.entry(item) if notes is not None else {}
            said = (status or {}).get(item.get("id")) or {}
            if (left.get("status") == "made" and left.get("docroot")
                    and said.get("state") == "exists"):
                # Made in an earlier run: only its files are missing.
                answer = {"ok": True, "docroot": left["docroot"],
                          "database": left.get("database")}
                unanswered = None
            else:
                answer, unanswered = _apply_with_questions(
                    connection, item, ask, report)
            if unanswered is not None:
                report.failed.append((item, "The new server asked a question "
                                      "that got no answer: %s"
                                      % unanswered.get("question")))
            elif answer.get("ok"):
                note = answer.get("note")
                if (extra_notes is not None and isinstance(note, str)
                        and note.strip() and note not in extra_notes):
                    extra_notes.append(note)
                made = item.get("kind") == "site"
                if made and isinstance(answer.get("docroot"), str):
                    docroots[item.get("name")] = answer["docroot"]
                if made and notes is not None:
                    notes.made(item, answer)
                if made and move is not None:
                    move(item, answer)
                    made = False
                if made:
                    report.files_to_copy.append(item)
                else:
                    if notes is not None:
                        notes.moved(item)
                    # The new server says when it had done this before: a
                    # job behind its own marker is not work this run did.
                    if answer.get("already"):
                        report.already.append((item, answer))
                    else:
                        report.moved.append((item, answer))
            elif answer.get("skipped"):
                report.skipped.append((item, answer.get("error")
                                       or "Left alone, as you asked."))
            elif answer.get("needs_password"):
                report.failed.append((item, answer.get("error") or
                                      "A password was needed and not given."))
            else:
                report.failed.append((item, answer.get("error")
                                      or "The new server gave no reason."))
        except Stopped as problem:
            message = problem.message
            if made:
                report.files_to_copy.append(item)
                message = MADE_NOT_FILLED + ": " + message
            report.failed.append((item, message))
            report.stopped = problem.message
            report.not_tried = list(items[index + 1:])
            print("Stopping here, since nothing else would work either: %s"
                  % problem.message)
            break
        except KeyboardInterrupt:
            message = "You stopped the run."
            if made:
                report.files_to_copy.append(item)
                message = MADE_NOT_FILLED + ". " + message
            report.failed.append((item, message))
            report.stopped = "You stopped the run."
            report.interrupted = True
            report.not_tried = list(items[index + 1:])
            print("Stopped, as you asked.")
            break
        except MoveFailed as problem:
            # Its own message says how far it got ("The files of X are
            # across, but …"), so nothing is put in front of it: two
            # sentences that contradict each other are worse than one.
            message = problem.message or "it failed without saying why"
            if made:
                report.files_to_copy.append(item)
                if not problem.told:
                    message = MADE_NOT_FILLED + ": " + message
            report.failed.append((item, message))
        except Exception as problem:  # that item's failure, not the batch's
            message = str(problem) or "it failed without saying why"
            if made:
                report.files_to_copy.append(item)
                message = MADE_NOT_FILLED + ": " + message
            report.failed.append((item, message))
    return report


# ---- The items the dashboard shows -----------------------------------------

def site_user(domain):
    """The user name domainctl itself gives a domain (like serverctl.names.
    user_name_for): its labels without the public suffix, joined. The old
    server's owner of the files is not used: it may be www, a shared account
    or a panel user that would clash with another site here."""
    labels = domain.lower().split(".")
    keep = labels[:-1]
    if len(keep) > 1 and len(keep[-1]) <= 2:
        keep = keep[:-1]
    name = "".join(re.sub(r"[^a-z0-9]", "", label) for label in keep)
    return name[:MAX_USER_NAME].lstrip("-_") or "site"


def _owner_of(path):
    try:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except (OSError, KeyError):
        return None


SAME_USER = ("%s would get the same Linux user on the new server as %s (the "
             "name of both comes down to %s), and one user there means one "
             "home, one web root and one wp-config.php. Move this one on its "
             "own, under a name of its own: migrate.py --host NEW --domain "
             "%s --user NAME")


def site_items(found, owner_of=_owner_of, users=None):
    """One item per site from sites(). The id is the same on every run. The
    database password is not in the item: move_site reads it from the site's
    wp-config.php when it needs it. 'users' is {domain: user} for a site
    whose Linux user was given on the command line."""
    given = users or {}
    items = []
    for site in found:
        name = site["name"].strip().lower()
        docroot = site.get("docroot")
        items.append({
            "id": "site:" + name, "kind": "site", "name": name,
            "user": given.get(name) or site_user(name),
            "old_owner": owner_of(docroot) if docroot else None,
            "aliases": site.get("aliases") or [], "docroot": docroot,
            "database": site.get("database"),
            "db_user": site.get("db_user"), "db_host": site.get("db_host"),
            "size": site.get("size"), "problem": site.get("problem")})
    _one_user_per_site(items, given)
    return items


def _one_user_per_site(items, given=()):
    """Two domains one company owns, like example.nl and example.com, come
    down to the same user name, and one user on the new server means one
    home, one web root and one wp-config.php: the second site would take
    over the first. The first domain in alphabetical order keeps the name,
    so the choice is the same on every run and the cron markers and the
    notes stay with the same site; the others are shown with the reason
    instead of being offered. A name that was asked for on the command line
    keeps it whatever its place in the alphabet."""
    kept = {}
    for item in sorted(items, key=lambda item: (item["name"] not in given,
                                                item["name"])):
        first = kept.setdefault(item["user"], item)
        if first is not item and not item.get("problem"):
            item["problem"] = SAME_USER % (item["name"], first["name"],
                                           item["user"], item["name"])


NO_SITE_JOB = ("This job belongs to no site on this server, so it is not "
               "moved. If the new server needs it, add it by hand.")
SITE_FIRST = ("The site %s is not being moved, so this job isn't either: "
              "its paths would point at files that aren't there.")
NO_OLD_ROOT = ("I don't know where the files of %s are on this server, so "
               "the paths in this job can't be pointed at the new server.")
NO_NEW_ROOT = ("I don't know where the files of %s are on the new server, so "
               "this job's paths can't be pointed at them. Move the site "
               "first, in this same run, and then this job.")
OUTSIDE_SITE = ("This job uses %s, which is not part of the site, so there "
                "is no way to know where it is on the new server. Set this "
                "job up there by hand.")
NO_NEW_PHP = ("The new server didn't say which PHP it has, so %s can't be "
              "replaced by it and this job would not run there.")
# Paths that mean the same on any server, so they travel as they are: the places a job throws its output away,
# and the scratch folders where a job makes its own lock file.
_KEEP_PATHS = ("/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr")
_KEEP_FOLDERS = ("/tmp/", "/var/tmp/")


def _is_php(word):
    """Whether a program is a PHP: php, php7.4, lsphp81, /opt/plesk/php/8.1/bin/php and the panel copies."""
    return bool(word) and _PHP_NAME.match(os.path.basename(word)) is not None


def _cron_parts(item):
    """(the start of the job's line that is not rewritten, its command). The
    time fields are left alone: "*/15" would otherwise read as the path
    /15."""
    line, command = item.get("line") or "", item.get("command")
    if isinstance(command, str) and command and line.endswith(command):
        return line[:len(line) - len(command)], command
    return "", line


def _mapped_path(path, old_root, new_root, interpreter, php):
    """A path of a cron job as it must read on the new server, or None when
    there is no way to know: inside the site it is the same path under the
    new web root, the PHP becomes the one the new server has, and a path
    that means the same everywhere stays as it is."""
    inside = os.path.normpath(path)
    if old_root and (inside == old_root or inside.startswith(old_root + "/")):
        return new_root.rstrip("/") + inside[len(old_root):]
    if path in _KEEP_PATHS or path.startswith(_KEEP_FOLDERS):
        return path
    if interpreter and path == interpreter and _is_php(path):
        return php
    return None


def _rewrite_paths(text, old_root, new_root, interpreter, php):
    """(the text with every path pointing at the new server, the paths it
    has no mapping for, which are left as they were)."""
    loose = []

    def one(found):
        path = found.group(0)
        mapped = _mapped_path(path, old_root, new_root, interpreter, php)
        if mapped is None:
            loose.append(path)
            return path
        return mapped

    return _CRON_PATH.sub(one, text), loose


def _rewrite_settings(settings, old_root, new_root, interpreter, php):
    """(the NAME=value lines as they must be on the new server, what changed
    in plain words). A PATH is the old server's own: the part of it that
    named the old PHP's directory becomes the new one's, paths inside the
    site are moved with the site, and a PATH with nothing left is dropped."""
    old_php = os.path.dirname(interpreter) if _is_php(interpreter) and interpreter.startswith("/") else None
    new_php = os.path.dirname(php) if php else None
    kept, said = [], []
    for line in settings:
        name, sign, value = line.partition("=")
        if not sign:
            kept.append(line)
            continue
        if name.strip().upper() == "PATH":
            places, changed = [], False
            for place in value.split(":"):
                place = place.strip()
                if not place:
                    continue
                if old_php and place.rstrip("/") == old_php:
                    changed = True
                    if not new_php:
                        continue  # nothing to put in its place: it would only name a directory that isn't there
                    place = new_php
                else:
                    moved = _mapped_path(place, old_root, new_root, None, None)
                    if moved is not None and moved != place:
                        place, changed = moved, True
                if place not in places:
                    places.append(place)
            if not places:
                said.append("The PATH this job ran with was left out: "
                            "nothing in it is on the new server.")
                continue
            if changed:
                said.append("The PATH this job runs with on the new server "
                            "is %s." % ":".join(places))
            kept.append("PATH=" + ":".join(places))
            continue
        moved = _rewrite_paths(value, old_root, new_root, None, None)[0]
        if moved != value:
            said.append("The %s this job runs with on the new server is %s."
                        % (name.strip(), moved.strip()))
        kept.append(name + sign + moved)
    return kept, said


def cron_for_new_server(item, docroot, php):
    """The cron job as it must be sent: every path inside the site pointing
    at the new server's web root, and the PHP replaced by the one the new
    server has. Returns (the item to send, what changed in plain words).
    Raises MoveFailed, with the same words the dashboard showed, when there
    is something it can't rewrite; nothing on either server is touched then.
    """
    name = item.get("site") or "its site"
    old_root = (item.get("site_docroot") or "").rstrip("/")
    if not old_root:
        raise MoveFailed(NO_OLD_ROOT % name, told=False)
    if not docroot:
        raise MoveFailed(NO_NEW_ROOT % name, told=False)
    interpreter = item.get("interpreter") or ""
    if _is_php(interpreter) and not php:
        raise MoveFailed(NO_NEW_PHP % interpreter, told=False)
    before, command = _cron_parts(item)
    command, loose = _rewrite_paths(command, old_root, docroot, interpreter,
                                    php)
    if loose:
        raise MoveFailed(OUTSIDE_SITE % ", ".join(sorted(set(loose))),
                         told=False)
    line = before + command
    settings, said = _rewrite_settings(item.get("settings") or [], old_root,
                                       docroot, interpreter, php)
    if line != item.get("line"):
        said.insert(0, "The job of %s runs this on the new server: %s"
                    % (name, line))
    return dict(item, line=line, settings=settings), said


def _cron_problem(item):
    """Why a job of a site can't be set up on the new server as it is, in
    plain words; None when it can. The same paths are looked at as when it
    is sent, but the new server's own places aren't known yet, so each one
    stands in for itself and only what cannot be mapped at all counts."""
    old_root = (item.get("site_docroot") or "").rstrip("/")
    if not old_root:
        return NO_OLD_ROOT % (item.get("site") or "its site")
    interpreter = item.get("interpreter") or ""
    loose = _rewrite_paths(_cron_parts(item)[1], old_root, old_root,
                           interpreter, interpreter)[1]
    if loose:
        return OUTSIDE_SITE % ", ".join(sorted(set(loose)))
    return None


def cron_items(jobs, found=()):
    """One item per cron job from cron_jobs(). The id comes from the OLD
    user and the job's own line, so the new server, which installs a job
    behind a marker with this id, never installs the same job twice. A job
    of a site runs as that site's new user, with its paths and its PHP
    rewritten for the new server when it is sent; one that belongs to no
    site, or that uses something outside the site, is reported and not
    moved. 'found' are the site items of the same run, which say where each
    site's files are and whether the site is being moved at all."""
    sites = {item["name"]: item for item in found if item.get("kind") == "site"}
    items, seen = [], {}
    for job in jobs:
        text = "%s\n%s" % (job["user"], job["line"])
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        seen[digest] = seen.get(digest, 0) + 1  # the same line twice is two
        suffix = "" if seen[digest] == 1 else "-%d" % seen[digest]
        item = dict(job, kind="cron", id="cron:" + digest + suffix,
                    old_user=job["user"])
        site = sites.get(job.get("site"))
        if job.get("site"):
            item["user"] = (site or {}).get("user") or site_user(job["site"])
            item["site_id"] = (site or {}).get("id")
            item["site_docroot"] = (site or {}).get("docroot")
            if not item.get("problem") and site is not None and site.get("problem"):
                item["problem"] = SITE_FIRST % site["name"]
            if not item.get("problem"):
                item["problem"] = _cron_problem(item)
        elif not item.get("problem"):
            item["problem"] = NO_SITE_JOB
        items.append(item)
    return items


# ---- What is remembered between runs ----------------------------------------

class State(object):
    """What this tool did, kept in a file so a second run knows. It never
    holds a password. It must be writable before anything is moved: a site
    made but never filled is only known from here, because the new server
    already calls it 'there'."""

    def __init__(self, path=STATE_PATH, say=print):
        self.path, self.say, self.items = path, say, {}
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, NotADirectoryError):
            return
        except (OSError, ValueError):
            say("I couldn't read my notes in %s, so I start without them. A "
                "site made earlier but never filled will show as already on "
                "the new server." % path)
            return
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.items = {k: v for k, v in data["items"].items()
                          if isinstance(v, dict)}
        else:
            say("My notes in %s are not in a form I know, so I start without "
                "them. A site made earlier but never filled will show as "
                "already on the new server." % path)

    def entry(self, item):
        return self.items.get(item.get("id"), {})

    def outstanding(self, item):
        """True for a site that was made on the new server and not
        completely filled."""
        return self.entry(item).get("status") == "made"

    def made(self, item, answer):
        old = self.entry(item)
        entry = {"status": "made"}
        for field in ("docroot", "database"):
            value = answer.get(field) if isinstance(answer, dict) else None
            ok = isinstance(value, str) and value
            entry[field] = value if ok else old.get(field)
        self.items[item["id"]] = entry
        self.save()

    def moved(self, item):
        self.items[item["id"]] = dict(self.entry(item), status="moved")
        self.save()

    def unwritable(self):
        """None when the notes can be kept, else a plain message."""
        folder = os.path.dirname(self.path) or "."
        probe = os.path.join(folder, ".probe.%d.tmp" % os.getpid())
        try:
            os.makedirs(folder, exist_ok=True)
            with open(probe, "w"):
                pass
            os.unlink(probe)
        except OSError as problem:
            return ("I can't keep notes in %s (%s), and without them a site "
                    "I made but did not fill would look finished next time. "
                    "Nothing was moved. Run me as a user that may write "
                    "there (root), or make that folder writable."
                    % (folder, problem.strerror or problem))
        return None

    def save(self):
        folder = os.path.dirname(self.path) or "."
        temporary = os.path.join(folder, ".state.json.%d.tmp" % os.getpid())
        try:
            os.makedirs(folder, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": self.items}, handle,
                          indent=1, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError) as problem:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise MoveFailed("I couldn't write my notes in %s (%s). Fix that "
                             "before going on." % (folder, getattr(
                                 problem, "strerror", None) or problem))


# ---- Moving a site's files and database -------------------------------------

NO_DELETE_NOTE = ("Files that are gone on the old server were not removed "
                  "from the new one; the copy only adds and replaces.")
VANISHED_NOTE = ("Some files vanished on the old server while they were "
                 "copied, which happens on a site that is in use. The rest "
                 "was copied.")
WRONG_CONFIG = (" The site's wp-config.php on the new server now points at "
                "the OLD server's database, and must not be left that way: "
                "send the site again.")


def _database_problem(name):
    """Why the new server would not make a database under this name, in
    plain words; None when it would. It checks the name itself, but the name
    goes into a command there, and the check that counts is the one in the
    process that sends it: the resume and --again paths send a name without
    applying anything first."""
    if not (isinstance(name, str) and name and len(name) <= MAX_USER_NAME
            and _NEW_NAME.fullmatch(name)):
        return ("a database name is lower case letters, digits, - and _, "
                "starting with a letter or a digit, at most %d characters."
                % MAX_USER_NAME)
    return None


class MoveFailed(Exception):
    """A site's files or database did not come across. The message says
    what, in words for a person. 'told' says the message itself says how far
    it got, so nothing may be put in front of it."""

    def __init__(self, message, told=True):
        super().__init__(message)
        self.message = message
        self.told = told


def run_visible(*args):
    """Runs a command with its own output on the screen, and gives its exit
    code. There is no time limit, unlike every other command here: a big site
    legitimately takes hours, and a limit would kill good work. What the
    person sees of the command's own progress is what tells a slow copy from
    a stuck one."""
    try:
        return subprocess.call(args, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CommandFailed("%s isn't installed" % args[0], missing=True)
    except OSError as problem:
        raise CommandFailed("can't run %s: %s" % (args[0], problem))


def run_piped(first, second, env=None):
    """Runs 'first | second' with the first one's output going straight into
    the second's input, so a big dump never passes through this program's
    memory or a file. Output and messages show on the screen, and there is no
    time limit, for the reason given at run_visible. Gives both exit codes."""
    try:
        one = subprocess.Popen(first, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               env=dict(os.environ, **env) if env else None)
    except FileNotFoundError:
        raise CommandFailed("%s isn't installed" % first[0], missing=True)
    except OSError as problem:
        raise CommandFailed("can't run %s: %s" % (first[0], problem))
    try:
        two = subprocess.Popen(second, stdin=one.stdout)
    except OSError as problem:
        one.kill()
        one.wait()
        raise CommandFailed("can't run %s: %s" % (second[0], problem))
    one.stdout.close()  # so the first gets a broken pipe if the second dies
    code_two = two.wait()
    return one.wait(), code_two


def _dump_command(settings):
    """The mysqldump words for a site, without its password. DB_HOST may be
    host, host:port or host:/socket. --single-transaction and --quick: the
    site is still in use, so no table may be locked, and a big table is
    streamed row by row."""
    words = ["mysqldump", "--single-transaction", "--quick",
             "-u", settings["DB_USER"]]
    host = settings.get("DB_HOST") or ""
    name, _, rest = host.partition(":")
    if rest.startswith("/"):
        words.append("--socket=" + rest)
    elif rest.isdigit():
        words += ["-P", rest]
    if name and name != "localhost":
        words += ["-h", name]
    return words + ["--", settings["DB_NAME"]]


def move_site(connection, site, answer, again=False, notes=None, ask=None,
              extra_notes=None, runner=run_visible, piper=run_piped,
              settings_of=wp_settings):
    """Brings a site's files and database to the new server, then has it
    finish the site. 'answer' is the new server's answer to apply: the files
    go to its 'docroot', never to a path made up here. Returns a dict with
    'ok' and 'steps' (or 'skipped' and 'message'); raises MoveFailed, or
    Stopped when the new server can't be reached. With again=True the files
    are copied again and the database is REPLACED, after 'ask' (given a
    question, says "yes" or "no") was told which database is emptied and
    said yes."""
    name = site["name"]
    docroot = answer.get("docroot") if isinstance(answer, dict) else None
    if not isinstance(docroot, str) or not docroot.startswith("/"):
        raise MoveFailed("The new server did not say where the files of %s "
                         "go, so nothing was copied." % name)
    source = site.get("docroot")
    if not source:
        raise MoveFailed("I don't know where the files of %s are on this "
                         "server." % name)
    database = site.get("database")  # the old wp-config.php's: whether there is a database at all
    made_database = None  # the database on the NEW server, under the name the new server itself gave it
    settings = None
    if database:
        made_database = (answer.get("database")
                         if isinstance(answer, dict) else None) or database
        reason = _database_problem(made_database)
        if reason:
            raise MoveFailed("I can't tell the new server to use %s as the "
                             "database of %s: %s Nothing was copied. Send "
                             "the site again, so the new server says which "
                             "database it made."
                             % (made_database, name, reason))
        settings = settings_of(os.path.join(source, "wp-config.php"))
        if not settings or not (settings.get("DB_NAME")
                                and settings.get("DB_USER")):
            raise MoveFailed("I can't read the database settings of %s from "
                             "its wp-config.php, so nothing was copied."
                             % name)
    if again and database:
        question = ("The database %s on the new server, for the site %s, "
                    "will be emptied and replaced by the one on this "
                    "server. Go on?" % (made_database, name))
        reply = ask(question) if ask is not None else None
        if not isinstance(reply, str) or reply.strip().lower() != "yes":
            return {"ok": False, "skipped": True,
                    "message": "Nothing was done for %s: the database was "
                               "not to be replaced." % name}
    if notes is not None:
        notes.made(site, answer)  # not moved until every step is done
    steps = []
    # Slashes on both ends: rsync then copies what is IN the folder, so the
    # files land in the web root itself and not in a folder inside it.
    # -s: the far side does not split the path, which is right for old and
    # new rsync alike.
    target = "%s@%s:%s/" % (connection.user, connection.host,
                            docroot.rstrip("/"))
    shell = " ".join(shlex.quote(word)
                     for word in ["ssh"] + connection.options())
    try:
        # --info=progress2: one line that keeps being overwritten, instead of
        # --progress, which prints a line per file and buries every real
        # message under fifty thousand of them on a WordPress site. It wants
        # rsync 3.1, which every server new enough to run this file has.
        code = runner("rsync", "-az", "-s", "--info=progress2",
                      "--human-readable", "--omit-dir-times",
                      "--no-owner", "--no-group", "-e", shell,
                      "--rsync-path", "sudo -n rsync",
                      source.rstrip("/") + "/", target)
    except CommandFailed as problem:
        raise MoveFailed("Copying the files of %s did not work: %s"
                         % (name, problem.message))
    if code not in (0, 24):
        raise MoveFailed("Copying the files of %s did not work: rsync "
                         "stopped with code %d (its own message is above)."
                         % (name, code))
    steps.append("Copied the files of %s to %s." % (name, docroot))
    if code == 24:
        steps.append(VANISHED_NOTE)
    if extra_notes is not None and NO_DELETE_NOTE not in extra_notes:
        extra_notes.append(NO_DELETE_NOTE)
    after = WRONG_CONFIG if database else ""
    if database:
        # The password goes in the environment: a command line is shown to
        # everyone on the machine by ps.
        env = {"MYSQL_PWD": settings.get("DB_PASSWORD") or ""}
        # --yes always: standard input is the dump, so a question could never
        # be answered. --replace: the new server made this database already.
        adopt = connection.command("domainctl", "wp", "adopt", site["user"],
                                   "--database", made_database,
                                   "--dump", "-",
                                   "--replace", "--yes")
        try:
            dumped, adopted = piper(_dump_command(settings), adopt, env=env)
        except CommandFailed as problem:
            raise MoveFailed("The files of %s are across, but its database "
                             "was not moved: %s.%s" % (name, problem.message,
                                                       after))
        if adopted == 255 or (dumped < 0 and adopted != 0):
            # ssh gives 255 when the connection fails; and when it dies first,
            # the dump is killed by the broken pipe (a negative code).
            raise MoveFailed("The files of %s are across, but the connection "
                             "to the new server was lost while the database "
                             "was sent (the dump itself did not fail).%s"
                             % (name, after))
        if dumped != 0:
            raise MoveFailed("The files of %s are across, but mysqldump "
                             "stopped with code %d, so the database may be "
                             "only partly there.%s" % (name, dumped, after))
        if adopted != 0:
            raise MoveFailed("The files of %s are across, but the new "
                             "server did not read the database in (code "
                             "%d).%s" % (name, adopted, after))
        steps.append("Read the database %s in and pointed wp-config.php at "
                     "it." % made_database)
    reply = connection.ask({"request": "finish_site", "item": site})
    if not reply.get("ok"):
        # By now the adopt has pointed wp-config.php at the new database, so
        # the warning above would be untrue: say what is really left.
        raise MoveFailed("The files%s of %s are across, but the site was not "
                         "finished off: %s Send it again to finish it."
                         % (" and database" if database else "", name,
                            reply.get("error") or "the new server gave no "
                            "reason."))
    steps.extend(s for s in reply.get("steps", []) if isinstance(s, str))
    if notes is not None:
        notes.moved(site)
    return {"ok": True, "steps": steps}


def mover(connection, notes, ask, extra_notes=None, **fakes):
    """The 'move' that run() calls with a site and the answer to its apply."""
    def move(item, answer):
        return move_site(connection, item, answer, notes=notes, ask=ask,
                         extra_notes=extra_notes, **fakes)
    return move


def do_again(connection, items, notes, ask, extra_notes=None, **fakes):
    """Copies the files of sites already moved once and replaces their
    database, each only after the question for it got a yes. Returns a
    Report: moved, skipped (said no) and failed. The new server's place for
    the files is the one remembered from the run that made the site."""
    report = Report()
    for index, item in enumerate(items):
        entry = notes.entry(item)
        try:
            if not entry.get("docroot"):
                raise MoveFailed("I don't know where %s's files go on the "
                                 "new server: it was not made by this tool "
                                 "here." % item["name"])
            result = move_site(connection, item, entry, again=True,
                               notes=notes, ask=ask, extra_notes=extra_notes,
                               **fakes)
        except Stopped as problem:
            report.failed.append((item, problem.message))
            report.stopped = problem.message
            report.not_tried = list(items[index + 1:])
            break
        except KeyboardInterrupt:
            report.failed.append((item, "You stopped the run."))
            report.stopped = "You stopped the run."
            report.interrupted = True
            report.not_tried = list(items[index + 1:])
            break
        except MoveFailed as problem:
            report.failed.append((item, str(problem)))
            continue
        if result.get("skipped"):
            report.skipped.append((item, result["message"]))
        else:
            report.moved.append((item, result))
    return report


# ---- The report that ends a run ----------------------------------------------

SYSTEM_DATABASES = ("information_schema", "performance_schema", "mysql", "sys")
DNS_BY_HAND = "On the new server, run: mailctl dns show %s"
AFTER_DNS = ("Once the DNS of a site points at the new server, run this on the "
             "new server for each site, to get its certificate:")
SITE_DNS_UNKNOWN = ("Each of the names below needs an A record, and an AAAA "
                    "record where there is IPv6, pointing at the new server. "
                    "It didn't say which addresses it has, so look them up "
                    "there (ip addr). Until a name points at the new server, "
                    "this server keeps answering for it.")
BOTH_RUN_JOBS = ("Until the DNS points at the new server, BOTH servers run "
                 "the same cron jobs. Switch the jobs off on the old server "
                 "when you move the DNS.")


def _wrap(text, indent="  "):
    return textwrap.wrap(text, 79, initial_indent=indent,
                         subsequent_indent=indent) or [indent]


def _section(out, title, lines):
    if lines:
        out.extend(["", title] + lines)


def _bullets(texts):
    lines = []
    for text in texts:
        lines.extend(textwrap.wrap(str(text), 79, initial_indent="  - ",
                                   subsequent_indent="    ") or ["  -"])
    return lines


def _dns_for_sites(addresses):
    """What a moved site's DNS needs, in words: the new server's own
    addresses, as the new server reported them. Nothing here is guessed
    from the name that was typed."""
    found = [text for text in (addresses or [])
             if isinstance(text, str) and text.strip()]
    kinds = [("an A record for %s", [a for a in found if ":" not in a]),
             ("an AAAA record for %s", [a for a in found if ":" in a])]
    parts = [words % ", ".join(these) for words, these in kinds if these]
    if not parts:
        return SITE_DNS_UNKNOWN
    return ("Each of the names below needs %s. Until a name points at the "
            "new server, this server keeps answering for it."
            % " and ".join(parts))


def _what(item):
    """The item's name with what kind of thing it is, like 'Website shop.nl'."""
    return "%s %s" % (KIND_NAMES.get(item.get("kind"), "Item"), _title(item))


def _ask_dns(connection, domain):
    """(the new server's table of records for a domain or None, why not)."""
    try:
        answer = connection.ask({"request": "dns", "domain": domain})
    except Exception as problem:  # a missing DNS section never stops the report
        return None, str(getattr(problem, "message", None) or problem)
    text = answer.get("text") if answer.get("ok") else None
    if isinstance(text, str) and text.strip():
        return text.rstrip(), None
    return None, str(answer.get("error") or "the new server gave no records")


def _imapsync(old, new, address):
    secret = "'TYPE-THE-PASSWORD-HERE'"
    return "\n".join(["imapsync \\"] + [
        "  --host%s %s --user%s %s \\" % (n, shlex.quote(host), n,
                                           shlex.quote(address))
        + "\n  --password%s %s --ssl%s%s" % (n, secret, n, end)
        for n, host, end in ((1, old, " \\"), (2, new, ""))]).splitlines()


def _same_item(one, other):
    return one.get("id") is not None and one.get("id") == other.get("id")


def report(result, connection=None, old_server=None, found_sites=None,
           mail=None, databases=None, items=None, notes=None,
           extra_notes=None, mail_database=None, addresses=None):
    """What a run did and what is still to do by hand, as text of at most
    79 columns. 'connection' is asked for the DNS records; 'found_sites' is
    sites(), 'databases' is database_sizes(), 'items' everything the
    dashboard offered, 'mail' the Mail from mail_inventory(), 'notes'
    what the discovery could not read and 'addresses' the addresses the new
    server says it has, for the sites' DNS records."""
    new_name = getattr(connection, "host", None) or "the new server"
    old_name = old_server or socket.gethostname()
    moved = list(result.moved)
    undone = [item for item in result.files_to_copy
              if not any(_same_item(item, f[0]) for f in result.failed)]
    out = []
    done = []
    for item, answer in moved:
        title = _what(item)
        if item.get("kind") == "site":
            where = answer.get("docroot") if isinstance(answer, dict) else None
            title += (": the Linux user on the new server is %s%s"
                      % (item.get("user") or site_user(item["name"]),
                         ", the files are in %s" % where if where else ""))
        done.append(title)
    _section(out, "Moved:", _bullets(done))
    _section(out, "Was already there, so nothing was changed:",
             _bullets(_what(item) for item, _ in result.already))
    _section(out, "Made on the new server, but its files and database are "
             "not there yet:", _bullets(
                 "%s (Linux user %s)" % (_what(i), i.get("user") or
                                         site_user(i["name"])) for i in undone))

    def cron_by_hand(item):
        return item.get("kind") == "cron" and item.get("problem") == NO_SITE_JOB

    failed = [(i, why) for i, why in result.failed if not cron_by_hand(i)]
    _section(out, "Did not work:", _bullets(
        "%s: %s" % (_what(i), why) for i, why in failed))
    _section(out, "Left alone:", _bullets(
        "%s: %s" % (_what(i), why) for i, why in result.skipped
        if not cron_by_hand(i)))
    if result.stopped:
        _section(out, "The run stopped early:", _bullets([result.stopped]))
    _section(out, "Not tried:", _bullets(_what(i) for i in result.not_tried))
    _section(out, "Mail accounts left out, because they got no password:",
             _bullets(result.left_out))
    _section(out, "Good to know:", _bullets(extra_notes or []))

    domains, accounts = [], []
    for item, _ in moved:
        if item.get("kind") != "mail_domain":
            continue
        domains.append(item.get("name") or "")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        for account in data.get("addresses") or []:
            address = account.get("address") if isinstance(account, dict) \
                else None
            if address and address not in result.left_out:
                accounts.append(address)
    if accounts:
        out.extend([""] + textwrap.wrap(
            "Mail messages are not moved by this tool. Run one of these for "
            "each account, with the password filled in (it is the same on "
            "both servers). Run them from any machine that can reach both "
            "servers; imapsync is on neither of them, so install it where "
            "you run it (on Debian or Ubuntu: apt install imapsync):", 79))
        for address in accounts:
            out.append("")
            out.extend(_imapsync(old_name, new_name, address))
    dns_said = False
    for domain in domains:
        text, why = _ask_dns(connection, domain) if connection else (None, None)
        out.extend(["", "DNS records for %s:" % domain])
        if text:
            if not dns_said:
                dns_said = True
                out.extend(_wrap("These are printed whole on purpose, so "
                                 "they can be copied as they are."))
            out.extend(text.splitlines())
        else:
            out.extend(_wrap(DNS_BY_HAND % domain))
            if why:
                out.extend(_wrap("Why not: %s" % why))

    sites_done = [i for i, _ in moved if i.get("kind") == "site"]
    if sites_done:
        out.extend(["", "DNS records for the sites that moved:"])
        out.extend(_wrap(_dns_for_sites(addresses)))
        out.extend(_bullets(", ".join([i["name"]] + [
            a for a in (i.get("aliases") or []) if isinstance(a, str)])
            for i in sites_done))
        out.extend([""] + textwrap.wrap(AFTER_DNS, 79))
        out.extend("  domainctl add %s" % i["name"] for i in sites_done)

    jobs = []
    for item in list(items or []) + [i for i, _ in result.skipped] + [
            i for i, _ in result.failed]:
        if cron_by_hand(item) and not any(_same_item(item, j) for j in jobs):
            jobs.append(item)
    if any(i.get("kind") == "cron" for i, _ in moved + list(result.already)):
        out.extend([""] + textwrap.wrap(BOTH_RUN_JOBS, 79))
    _section(out, "Cron jobs that belong to no site, so they were not moved:",
             _bullets("%s (user %s)" % (j["line"], j.get("old_user")
                                        or j.get("user")) for j in jobs))

    mail_left = list(getattr(mail, "left_out", None) or [])
    catch_alls = [t for t in mail_left if "catch-all" in t
                  or (t.startswith("The forward from") and "catch" in t)]
    send_as = [t for t in mail_left if "may send as" in t]
    other_forwards = [t for t in mail_left if t.startswith("The forward from")
                      and t not in catch_alls]
    rest = [t for t in mail_left
            if t not in catch_alls and t not in send_as
            and t not in other_forwards]
    hand = []
    _section(hand, "Catch-all addresses and aliases that could not be moved. "
             "Set them up on the new server by hand:",
             _bullets(catch_alls + other_forwards))
    _section(hand, "Permission to send as another address, with no forward "
             "to carry it. Set it on the new server by hand:",
             _bullets(send_as))
    _section(hand, "Other mail settings that were left out:", _bullets(rest))
    others = [s for s in (found_sites or []) if not s.get("wordpress")]
    _section(hand, "Sites that are not WordPress. They are not moved; copy "
             "them by hand:", _bullets(
                 "%s (files in %s)" % (s["name"], s.get("docroot") or
                                       "an unknown place") for s in others))
    known = set(SYSTEM_DATABASES) | {SPAM_DATABASE}
    known.update(d for d in (mail_database,) if d)
    known.update(s["database"] for s in (found_sites or [])
                 if s.get("database"))
    loose = sorted(name for name in (databases or {}) if name not in known)
    _section(hand, "Databases that belong to no site. They are not moved:",
             _bullets(loose))
    _section(hand, "What the server could not read while looking around:",
             _bullets(notes or []))
    if hand:
        out.extend(["", "Still to do by hand", "-------------------"])
        out.extend(hand[1:] if hand[0] == "" else hand)
    if not out:
        return "Nothing was moved.\n"
    if not (result.moved or result.files_to_copy):
        # A page of nothing but hand work would otherwise leave the one thing
        # the person wants to know first unsaid.
        out = ["", "Nothing was moved."] + out
    return "\n".join(["Report", "======"] + out) + "\n"


# ---- Running it ---------------------------------------------------------------

HELP_DESCRIPTION = """Moves the mail, WordPress sites and cron jobs of this server to a new one.

How to use it:
  1. Copy this one file to the OLD server, for example from your own machine:
       scp migrate.py root@OLD-SERVER:
  2. On the old server, run it as root:
       python3 migrate.py --host NEW-SERVER
  3. It prints a key. Paste that line into /home/ansible/.ssh/authorized_keys
     on the new server (once), then run it again.
  4. It shows a list of what it found. Type the numbers to move.

What it needs on the old server: Python 3.6 or newer, the ssh and ssh-keygen
programs and rsync. To find things it also uses the mysql or mariadb client,
mysqldump, doveadm, apache2ctl and crontab, when they are there; what it can't
look at it says so. The new server must be built with this repository.

It writes only these on the old server: its own key pair (migrate_key and
migrate_key.pub) and the known-hosts file beside it (migrate_known_hosts), in
the .ssh folder of the user running it, and its notes in
/var/lib/migrate/state.json."""


def _arguments(argv):
    parser = argparse.ArgumentParser(
        prog="migrate.py", description=HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", metavar="NEW",
                        help="the name or address of the new server")
    parser.add_argument("--domain", metavar="example.nl",
                        help="move only this mail domain and its sites")
    parser.add_argument("--again", action="store_true",
                        help="catch up a site that already moved: copy its "
                        "files again and replace its database")
    parser.add_argument("--user", metavar="NAME",
                        help="the Linux user to give the site on the new "
                        "server, instead of the name taken from its domain; "
                        "goes with --domain, for a site whose name would "
                        "clash with another one's")
    return parser.parse_args(argv)


def _domain_items(mail):
    """One mail_domain item for each domain of a Mail, with its part of the
    file mailctl reads."""
    file = mail.file
    items = []
    for domain in file["domains"]:
        suffix = "@" + domain

        def ends(value):
            return str(value).lower().endswith(suffix)

        data = {
            "version": file["version"], "domains": [domain],
            "addresses": [a for a in file["addresses"]
                          if ends(a.get("address", ""))],
            "forwards": [f for f in file["forwards"]
                         if ends(f.get("source", ""))],
            "spam": [x for x in file["spam"]
                     if x.get("target") == domain or ends(x.get("target", ""))],
            "sieve": [x for x in file["sieve"] if ends(x.get("address", ""))]}
        items.append({"id": "mail:" + domain, "kind": "mail_domain",
                      "name": domain, "data": data})
    return items


def _in_domain(name, domain):
    name = (name or "").lower()
    return name == domain or name.endswith("." + domain)


def _server_spam_lines(mail):
    """The server-wide spam settings of this server, as hand work: the new
    server's own were set there on purpose and are not overwritten."""
    lines = []
    for entry in mail.file.get("spam") or []:
        if entry.get("target") == "server":
            lines.append("The server-wide spam setting %s is %s on this "
                         "server. It was not moved: the new server keeps its "
                         "own. Set it there by hand if you want it."
                         % (entry.get("setting"), entry.get("value")))
    return lines


def _left_out_of(mail, moved):
    """The Mail with the left-out lines that matter after this run: a line
    is dropped only when it names a mail domain of this server that did not
    move. Whatever names no domain is kept. The server-wide spam settings
    are added as hand work."""
    if mail is None:
        return None
    others = [d for d in mail.file.get("domains") or [] if d not in moved]
    patterns = [re.compile(r"(?<![a-z0-9.-])%s(?![a-z0-9-])" % re.escape(d))
                for d in others]
    kept = [text for text in mail.left_out
            if not any(p.search(text.lower()) for p in patterns)]
    return Mail(mail.file, kept + _server_spam_lines(mail), mail.notes)


def _typed(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def _question(text):
    print("\n".join(textwrap.wrap(text, 79)))
    return _typed("Type yes or no: ")


def _look_around(args, notes, run=run_command):
    """(mail, mail_database, sites, databases, items) of this server. What
    could not be read goes into notes."""
    domain = args.domain.lower() if args.domain else None
    wanted_user = getattr(args, "user", None)
    database = mail_database(run, notes)
    mail = mail_inventory(run, database, domain) if database else None
    if mail is not None:
        notes.extend(mail.notes)
    found = sites(run, notes)
    sizes = database_sizes(run, notes)
    jobs = cron_jobs(run, found, notes)
    if domain:
        found = [s for s in found if _in_domain(s["name"], domain)
                 or any(_in_domain(a, domain) for a in s.get("aliases") or [])]
        names = set(s["name"] for s in found)
        jobs = [j for j in jobs if j.get("site") in names]
    items = _domain_items(mail) if mail else []
    minted = site_items(found, users=_user_for(found, domain, wanted_user))
    items += minted + cron_items(jobs, minted)
    return mail, database, found, (None if domain else sizes), items


def _user_for(found, domain, user):
    """{domain: user} for the one site --user is about: the site of the
    domain that was asked for, or the only site in the run."""
    if not user:
        return {}
    names = [site["name"] for site in found]
    wanted = [name for name in names if name == domain] or names
    if len(wanted) != 1:
        raise Stopped("--user is the Linux user of ONE site, and this run "
                      "has %d: %s. Run me again with --domain for the one "
                      "site whose user name you want to choose."
                      % (len(names), ", ".join(names) or "none"))
    return {wanted[0]: user}


def _status_of(connection, items):
    answer = connection.ask({"request": "status", "items": items})
    if not answer.get("ok") or not isinstance(answer.get("items"), dict):
        raise Stopped("The new server could not say what it already has: %s"
                      % (answer.get("error") or "no reason given"))
    return answer["items"]


def _choose(lines, prompt):
    typed = _typed(prompt)
    return pick(lines, typed)


def _to_do_again(items, state, status):
    """The sites a catch-up can be done for: this tool moved them, it knows where their files went on the new
    server, and the new server still has them. Before the DNS is flipped the new server reports a site it made
    itself as 'exists', which is what lets this be the catch-up before the flip that the design asks for."""
    return [item for item in items if item.get("kind") == "site"
            and state.entry(item).get("status") == "moved"
            and state.entry(item).get("docroot")
            and (status.get(item["id"]) or {}).get("state") == "exists"]


def _work(args, connection, state, notes, doing=None, hello=None):
    """Does the whole run after the connection works and returns its Report.
    'doing' is a list whose one place always holds what is being done.
    'hello' is what the new server answered when it was first asked: which
    PHP it has, for the cron jobs."""
    doing = doing if doing is not None else [""]
    doing[0] = "looking around this server"
    mail, database, found, sizes, items = _look_around(args, notes)
    doing[0] = "asking the new server what it already has"
    status = _status_of(connection, items)
    doing[0] = "moving what you picked"
    extra = []
    moved_domains = []
    if args.again:
        chosen = _to_do_again(items, state, status)
        if not chosen:
            print("No site has moved yet, so there is nothing to do again.")
        else:
            print("Sites that already moved:")
            for number, item in enumerate(chosen, 1):
                print("%3d  %s" % (number, _title(item)))
            chosen = _choose(chosen, "Numbers to copy again (like 1 3, 2-5, "
                             "all; Enter for none): ")
        ask = _question
        result = do_again(connection, chosen, state, ask, extra)
    else:
        print(dashboard(items, status, state))
        lines = offered(items, status, state)
        chosen = []
        if lines:
            chosen = _choose(lines, "Numbers to move (like 1 3, 2-5, all; "
                             "Enter for none): ")
        move = mover(connection, state, _question, extra)
        result = run(connection, chosen, _question, move=move, notes=state,
                     status=status, php=(hello or {}).get("php"),
                     extra_notes=extra)
        moved_domains = [i["name"] for i, _ in result.moved
                         if i.get("kind") == "mail_domain"]
        mail = _left_out_of(mail, moved_domains)
    if args.again:
        mail = None
    print()
    doing[0] = "writing the report"
    print(report(result, connection=connection, old_server=socket.gethostname(),
                 found_sites=found, mail=mail, databases=sizes, items=items,
                 notes=notes, extra_notes=extra, mail_database=database,
                 addresses=(hello or {}).get("addresses")))
    return result


def main(argv=None, geteuid=os.geteuid):
    args = _arguments(argv)
    if geteuid() != 0:
        print("Run me as root: I read other users' cron jobs and mailboxes.")
        return 1
    doing = ["starting"]
    try:
        if args.domain and not _DOMAIN_ARGUMENT.fullmatch(args.domain):
            raise Stopped("'%s' isn't a domain name." % args.domain)
        if args.user and not args.domain:
            raise Stopped("--user is the Linux user of one site, so it goes "
                          "with --domain: run me again with --domain "
                          "example.nl --user %s." % args.user)
        if args.user and not (len(args.user) <= MAX_USER_NAME
                              and _NEW_NAME.fullmatch(args.user)):
            raise Stopped("'%s' isn't a user name the new server takes: use "
                          "lower case letters, digits, - and _, starting "
                          "with a letter or a digit, at most %d characters."
                          % (args.user, MAX_USER_NAME))
        host = args.host or _typed("Name or address of the new server: ").strip()
        if not host:
            raise Stopped("I need the name or address of the new server: run "
                          "me again with --host NEW-SERVER.")
        doing[0] = "making the key"
        key = own_key()
        print("The new server must have this key (one line) in\n%s:\n\n%s\n"
              % (AUTHORIZED_KEYS, key))
        doing[0] = "connecting to the new server"
        connection = Connection(host)
        hello = check(connection, show_key=False)
        result = _work(args, connection, State(), [], doing, hello)
        if result.interrupted:
            print("You stopped the run; the report above says what moved.")
            return 1
    except Stopped as problem:
        print(problem.message)
        return 1
    except KeyboardInterrupt:
        print("\nStopped by you while %s. What was already moved stays "
              "moved; run me again to carry on." % doing[0])
        return 1
    except Exception as problem:
        print("\n".join(textwrap.wrap(
            "Stopped by an error while %s: %s: %s. What was already moved "
            "stays moved; run me again." % (doing[0], type(problem).__name__,
                                           problem), 79)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
