#!/usr/bin/env python3
"""Moves what is on this server to a new one. Copy this file to the old server and run it there; with
migratectl.py beside it, it also brings the new server's half up to this release itself.

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
import secrets
import textwrap
import threading
import time

try:
    import crypt  # gone in Python 3.13, so the hashing falls back to openssl
except ImportError:  # pragma: no cover - depends on the version this server has
    crypt = None

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
NGINX_SITES = "/etc/nginx/sites-enabled"
LSWS_ROOT = "/usr/local/lsws"
# Which web server a site was found in. It is said on the screen for a site this tool doesn't move, because
# what has to be done by hand depends on it.
APACHE, NGINX, LITESPEED = "Apache", "nginx", "OpenLiteSpeed"
# Of the talk with migratectl on the new server; both halves must come from the same release. It goes up whenever
# the requests or the answers change, so a mismatch is found at the handshake and not in the middle of a site.
PROTOCOL_VERSION = 17
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
    all: a mysql client whose my.cnf points at a machine that drops packets, or doveadm against a dead
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


# ---- Sites that are nothing but a redirect ----------------------------------

# A domain that only sends visitors somewhere else is a site to move like any other, and the one kind this
# tool can move whole without copying a byte: the new server's sites are all instances of one shared
# OpenLiteSpeed template, which nothing here may change, but that template loads each site's own .htaccess
# (autoLoadHtaccess). So a redirect is read here, written as Apache rules, and put in the new site's web root.
_REDIRECT_CODES = ("301", "302", "303", "307", "308")
_ABSOLUTE = re.compile(r"^(?:https?:)?//", re.I)
_R_FLAG = re.compile(r"\[[^]]*\bR(?:=\d{3})?\b[^]]*\]", re.I)
_PROXY_FLAG = re.compile(r"\[[^]]*\bP\b[^]]*\]")
# The plain-HTTP-to-HTTPS redirect that every virtual host has. It sends visitors to the name they asked for,
# so it says nothing about where a site went, and the new server's own template does it for every site there.
_SAME_HOST = re.compile(r"^https?://%\{(?:HTTP_HOST|SERVER_NAME)\}", re.I)
_NGINX_SAME_HOST = re.compile(r"^https?://\$(?:host|server_name|http_host)\b", re.I)
PROXIES = "%s sends requests on to another server (%s), which is not a redirect: set it up by hand."
NOT_A_REDIRECT = "%s rewrites inside the site (%s), which has no meaning without the site's own files."


def _redirect(to=None, rules=(), by_hand=()):
    return {"to": to, "rules": list(rules), "by_hand": list(by_hand)}


def _whole_site(pattern, target):
    """Where a rule sends the whole site, when that is what it does: a pattern that matches every address and a
    target that is a whole URL. None for a rule about one path, which is worth keeping but says nothing about
    where the domain as a whole has gone."""
    everything = pattern.strip("^$") in ("", "/", "(.*)", ".*", "/(.*)", "/.*")
    return target if everything and _ABSOLUTE.match(target) else None


def _apache_redirects(lines):
    """The redirect of one <VirtualHost>, from its Redirect, RedirectMatch and RewriteRule lines.

    Apache's own syntax is what OpenLiteSpeed reads from a .htaccess, so a rule that is already a redirect goes
    over as it stands. A rule that rewrites inside the site, or proxies, is named instead: neither means
    anything on a server that hasn't got the site.
    """
    found, conditions = _redirect(), []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        words = line.split()
        if len(words) < 2:
            continue
        directive = words[0].lower()
        if directive == "rewritecond":
            conditions.append(line)
            continue
        if directive in ("proxypass", "proxypassreverse"):
            found["by_hand"].append(PROXIES % (words[0], " ".join(words[1:])))
            continue
        if directive in ("redirect", "redirectmatch"):
            rest = words[1:]
            code = rest[0] if rest and (rest[0] in _REDIRECT_CODES or rest[0].lower() == "permanent") else None
            arguments = rest[1:] if code else rest
            if len(arguments) == 2:
                found["rules"].append(line)
                found["to"] = found["to"] or _whole_site(arguments[0], arguments[1])
            conditions = []
            continue
        if directive != "rewriterule":
            conditions = []
            continue
        if len(words) > 2 and _SAME_HOST.match(words[2]):
            conditions = []
            continue  # to the name that was asked for, over https: plumbing, and the new server has its own
        if _PROXY_FLAG.search(line):
            found["by_hand"].append(PROXIES % ("RewriteRule", " ".join(words[1:3])))
        elif _R_FLAG.search(line) or _ABSOLUTE.match(words[2] if len(words) > 2 else ""):
            found["rules"].extend(conditions + [line])
            if not conditions and len(words) > 2:
                found["to"] = found["to"] or _whole_site(words[1], words[2])
        elif len(words) > 2:
            found["by_hand"].append(NOT_A_REDIRECT % ("RewriteRule", " ".join(words[1:3])))
        conditions = []
    return found if found["rules"] or found["by_hand"] else None


def _nginx_redirects(directives):
    """The same for one nginx server block, written out as the Apache rules a .htaccess holds."""
    found = _redirect()
    for directive, arguments in directives:
        if directive == "proxy_pass" and arguments:
            found["by_hand"].append(PROXIES % ("proxy_pass", arguments[0]))
        elif directive == "return" and len(arguments) == 2 and arguments[0] in _REDIRECT_CODES:
            if not _NGINX_SAME_HOST.match(arguments[1]):
                _nginx_rule(found, "^", arguments[1], arguments[0])
        elif directive == "rewrite" and len(arguments) == 3 and arguments[2].lower() in ("permanent", "redirect"):
            if not _NGINX_SAME_HOST.match(arguments[1]):
                _nginx_rule(found, arguments[0], arguments[1],
                            "301" if arguments[2].lower() == "permanent" else "302")
    return found if found["rules"] or found["by_hand"] else None


def _nginx_rule(found, pattern, target, code):
    """One nginx redirect as an Apache one. nginx's own variables have no meaning in a .htaccess, so a target
    that uses any but $request_uri (which is the address as it came in) is named instead of guessed at."""
    rest = re.sub(r"\$request_uri\b", "", target)
    if "$" in re.sub(r"\$[0-9]", "", rest):
        found["by_hand"].append("The nginx redirect to %s uses something this tool can't write as an Apache "
                                "rule. Write it in the new site's .htaccess by hand." % target)
        return
    if rest != target:  # it carried the address as it came in: Apache's own capture does that
        pattern, target = "^(.*)$", rest.rstrip("/") + "/$1"
    found["rules"].append("RewriteRule %s %s [R=%s,L]" % (_as_apache(pattern), target, code))
    found["to"] = found["to"] or _whole_site(pattern, target)


def _as_apache(pattern):
    """An nginx pattern as a .htaccess one: there, the address has no slash in front of it."""
    return re.sub(r"^\^/", "^", pattern)


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
                current = found[name] = {"name": name, "aliases": [], "docroot": None,
                                         "problem": None, "redirect": None}
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
                block = _vhost_block(files[path], number)
                _, aliases, docroot = _vhost_directives(block)
                current["docroot"] = docroot
                current["redirect"] = _both_redirects(current.get("redirect"), _apache_redirects(block))
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
            block = _vhost_block(lines, number)
            name, aliases, docroot = _vhost_directives(block)
            if name is None:
                notes.append("A virtual host in %s (line %d) has no ServerName, so it is left out." % (path, number))
                continue
            site = vhosts.setdefault(name, {"name": name, "aliases": [], "docroot": None, "problem": None,
                                            "redirect": None})
            site["docroot"] = site["docroot"] or docroot
            site["redirect"] = _both_redirects(site.get("redirect"), _apache_redirects(block))
            site["aliases"].extend(alias for alias in aliases if alias not in site["aliases"])
    return list(vhosts.values())


def _read_config(path, notes=None):
    """The text of a configuration file, or None when it isn't there. A file that is there and can't be read is
    worth a note: something is being served from it that nobody here can see."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except IOError as problem:
        if notes is not None and os.path.lexists(path):
            notes.append("Couldn't read %s: %s." % (path, problem))
        return None


def _tagged(vhosts, server):
    """The sites with the name of the web server that serves them."""
    for vhost in vhosts:
        vhost["server"] = server
    return vhosts


def _also(first, second):
    """Both lists of names, each one once, in the order they were found."""
    out = list(first or [])
    out.extend(name for name in (second or []) if name not in out)
    return out


def _both_redirects(first, second):
    """The redirect of one name that was found twice, in two files or in two web servers: the rules of both,
    each of them once."""
    if not first or not second:
        return first or second
    return _redirect(first.get("to") or second.get("to"),
                     first["rules"] + [rule for rule in second["rules"] if rule not in first["rules"]],
                     first["by_hand"] + [one for one in second["by_hand"] if one not in first["by_hand"]])


def _merged_vhosts(found):
    """One entry per name, out of what every web server on this machine serves. A name two of them answer to is
    one site: the one that says where its files are wins, since that is what moving it needs."""
    merged = {}
    for vhost in found:
        name = str(vhost.get("name") or "").strip().lower()
        if not name:
            continue
        vhost["name"] = name
        there = merged.get(name)
        if there is None:
            merged[name] = vhost
        elif there.get("docroot") is None and vhost.get("docroot") is not None:
            vhost["aliases"] = _also(there.get("aliases"), vhost.get("aliases"))
            vhost["redirect"] = _both_redirects(there.get("redirect"), vhost.get("redirect"))
            merged[name] = vhost
        else:
            there["aliases"] = _also(there.get("aliases"), vhost.get("aliases"))
            there["redirect"] = _both_redirects(there.get("redirect"), vhost.get("redirect"))
    return list(merged.values())


def _apache_vhosts(run, sites_dir, notes):
    """What Apache serves: its own view of itself where it answers, and its files where it doesn't. A machine
    without Apache says nothing: another web server has the sites."""
    try:
        vhosts = _vhosts_from_apache(run("apache2ctl", "-S"), notes)
        if not vhosts:
            raise CommandFailed("it listed no virtual hosts")
        return _tagged(vhosts, APACHE)
    except CommandFailed as failed:
        if not os.path.isdir(sites_dir):
            return []
        notes.append("Couldn't get the sites from apache2ctl (%s), so %s was read instead."
                     % (failed.message, sites_dir))
    return _tagged(_vhosts_from_files(sites_dir, notes), APACHE)


def _nginx_vhosts(run, sites_dir, notes):
    """What nginx serves. 'nginx -T' prints its whole configuration, with every file it includes, so the sites
    are all in it however they are split over files."""
    try:
        text = run("nginx", "-T")
    except CommandFailed as failed:
        if not os.path.isdir(sites_dir):
            return []
        notes.append("Couldn't get the sites from nginx -T (%s), so %s was read instead."
                     % (failed.message, sites_dir))
        text = _config_files(sites_dir, notes)
    return _tagged(_vhosts_from_nginx(text), NGINX)


def _config_files(directory, notes):
    """Every file in a directory of configuration, one after the other. The files in nginx's sites-enabled are
    links with no suffix of their own, so they are taken as they come."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as problem:
        notes.append("Couldn't read %s: %s." % (directory, problem))
        return ""
    texts = [_read_config(os.path.join(directory, name), notes) for name in names
             if os.path.isfile(os.path.join(directory, name))]
    return "\n".join(text for text in texts if text)


# A word, or one of the three characters that end or open a part of nginx's configuration.
_NGINX_TOKEN = re.compile(r"[{};]|[^\s{};]+")
# Names that stand for "whatever was asked for", so they are nobody's site.
_NGINX_ANY = ("_", "localhost", "*")


def _vhosts_from_nginx(text):
    """[{name, aliases, docroot, problem}] from the server blocks of nginx's configuration. A block's own root
    is the site's; one set in a location inside it is taken when the block itself has none."""
    found = []
    for directives in _nginx_servers(text or ""):
        names, docroot = [], None
        for directive, arguments in directives:
            if directive == "server_name":
                names.extend(name.lower() for name in arguments
                             if name.lower() not in _NGINX_ANY and name.lower() not in names)
            elif directive == "root" and docroot is None and arguments:
                docroot = os.path.normpath(arguments[0])
        if names:
            found.append({"name": names[0], "aliases": names[1:], "docroot": docroot, "problem": None,
                          "redirect": _nginx_redirects(directives)})
    return found


def _nginx_servers(text):
    """The directives of each 'server' block, as (directive, arguments), the ones in the blocks inside it too."""
    out, words, depth, inside, current = [], [], 0, None, None
    for token in _NGINX_TOKEN.findall(re.sub(r"#[^\n]*", "", text)):
        if token == ";":
            if words and current is not None:
                current.append((words[0].lower(), words[1:]))
            words = []
        elif token == "{":
            depth += 1
            if current is None and words and words[0].lower() == "server":
                inside, current = depth, []
            words = []
        elif token == "}":
            if current is not None and depth == inside:
                out.append(current)
                inside, current = None, None
            depth -= 1
            words = []
        else:
            words.append(token)
    return out


_OLS_BLOCK = re.compile(r"^\s*([A-Za-z_]+)\s+(\S+?)\s*\{\s*$")
# A value that runs over several lines, like rewrite rules: "rules <<<END", up to a line holding just "END".
_OLS_HEREDOC = re.compile(r"<<<\s*(\S+)\s*$")


def _ols_vhosts(root, notes):
    """What OpenLiteSpeed serves. A virtualhost block says where its own configuration file is, that file says
    where the site's files are, and the listeners' 'map' lines say which domain names answer to it: the block's
    own name is a label, which may be a domain or something like 'Example'."""
    text = _read_config(os.path.join(root, "conf", "httpd_config.conf"), notes)
    if text is None:
        return []
    blocks = _ols_blocks(text)
    vhosts, order = {}, []
    for kind, name, body in blocks:
        if kind != "virtualhost" or name.lower() in vhosts:
            continue
        docroot, own = _ols_docroot(root, name, _ols_values(body), notes)
        vhosts[name.lower()] = {"name": name.lower(), "aliases": [], "docroot": docroot, "problem": None,
                                "redirect": _ols_redirects(own)}
        order.append(name.lower())
    for kind, name, body in blocks:
        if kind != "listener":
            continue
        for directive, arguments in _ols_lines(body):
            if directive == "map" and len(arguments) > 1 and arguments[0].lower() in vhosts:
                _ols_mapped(vhosts[arguments[0].lower()], " ".join(arguments[1:]))
    return _tagged([vhosts[name] for name in order if _valid_domain(vhosts[name]["name"])], LITESPEED)


def _ols_mapped(vhost, text):
    """The domain names a listener sends to this virtual host. The first of them is the site's name when its
    block is called something that is no domain at all."""
    for domain in [word.strip().lower().lstrip("*.") for word in re.split(r"[,\s]+", text) if word.strip()]:
        if not _valid_domain(domain) or domain == vhost["name"]:
            continue
        if not _valid_domain(vhost["name"]):
            vhost["name"] = domain
        elif domain not in vhost["aliases"]:
            vhost["aliases"].append(domain)


def _ols_docroot(root, name, values, notes):
    """(where a virtual host's files are, its own configuration): that file says where they are, and until it
    is read all that is known is the directory the host lives in."""
    vh_root = _ols_path(values.get("vhroot"), root, None, name)
    config = _ols_path(values.get("configfile"), root, vh_root, name)
    if not config:
        return vh_root, ""
    text = _read_config(config, notes)
    if text is None:
        notes.append("Couldn't read %s, so where the files of %s are isn't known." % (config, name))
        return vh_root, ""
    return _ols_path(_ols_values(text).get("docroot"), root, vh_root, name) or vh_root, text


def _ols_redirects(text):
    """The redirect of one OpenLiteSpeed virtual host: the rules of its 'rewrite' block, which are written in
    Apache's own syntax, in lines of their own or in a heredoc."""
    lines = []
    for kind, _name, body in _ols_blocks(text or ""):
        if kind != "rewrite":
            continue
        lines.extend(body.split("\n"))
        for heredoc in re.finditer(r"<<<\s*(\S+)\s*\n(.*?)\n\s*\1\s*$", text or "", re.S | re.M):
            lines.extend(heredoc.group(2).split("\n"))
    return _apache_redirects(lines)


def _ols_path(value, root, vh_root, name):
    """A path out of the configuration, with the names OpenLiteSpeed writes in them filled in."""
    if not value:
        return None
    for mark, filling in (("$SERVER_ROOT", root), ("$VH_ROOT", vh_root), ("$VH_NAME", name)):
        if filling:
            value = value.replace(mark, str(filling))
    value = value.strip().strip('"')
    # A path that doesn't start at the root of the machine starts at OpenLiteSpeed's own directory, which is
    # how its configuration writes the files it installs itself.
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(root, value))


def _ols_blocks(text):
    """Every 'kind name { ... }' in the configuration, as (kind, name, what is inside it)."""
    lines, index, out = text.split("\n"), 0, []
    while index < len(lines):
        start = _OLS_BLOCK.match(lines[index])
        index += 1
        if not start:
            continue
        body, depth = [], 1
        while index < len(lines) and depth:
            here = _OLS_HEREDOC.search(lines[index])
            if here:
                while index < len(lines) and lines[index].strip() != here.group(1):
                    index += 1
            else:
                depth += lines[index].count("{") - lines[index].count("}")
                if depth:
                    body.append(lines[index])
            index += 1
        out.append((start.group(1).lower(), start.group(2), "\n".join(body)))
    return out


def _ols_lines(text):
    """The 'name value' lines of a block, as (name in lower case, the words of the value)."""
    out = []
    for line in text.split("\n"):
        line = line.split("#", 1)[0].strip()
        if line and not line.endswith("{") and line != "}":
            words = line.split()
            out.append((words[0].lower(), words[1:]))
    return out


def _ols_values(text):
    """{name: value} of a block, the first of each name, as OpenLiteSpeed itself takes them."""
    values = {}
    for name, arguments in _ols_lines(text):
        if arguments and name not in values:
            values[name] = " ".join(arguments)
    return values


def sites(run, notes=None, sites_dir=SITES_ENABLED, sizes=None, nginx_dir=None, lsws_root=None, signage=None):
    """One dict per site this server serves: name, aliases, docroot, server (which web server has it),
    wordpress, database, db_user, db_password, db_host, prefix, size (bytes, or None) and problem (plain words,
    or None). A site with a problem is not safe to move as it is. 'sizes' is database_sizes(), looked up here
    when not given.

    Every web server the machine has is looked at, not only Apache: a site served by nginx or OpenLiteSpeed is
    just as much a site to move, and one that is missed here is missed everywhere after.
    """
    notes = notes if notes is not None else []
    # Where the other two keep their configuration, unless a caller says otherwise, read here and not in the
    # signature: a test points them at a place of its own.
    nginx_dir = NGINX_SITES if nginx_dir is None else nginx_dir
    lsws_root = LSWS_ROOT if lsws_root is None else lsws_root
    vhosts = _merged_vhosts(_apache_vhosts(run, sites_dir, notes)
                            + _nginx_vhosts(run, nginx_dir, notes)
                            + _ols_vhosts(lsws_root, notes))
    if not vhosts:
        notes.append("No sites were found in the configuration of Apache, nginx or OpenLiteSpeed.")
    if sizes is None:
        sizes = database_sizes(run, notes) if vhosts else {}
    running = _signage_behind(run, sites_dir, notes) if signage is None else dict(signage)
    for site in vhosts:
        _look_for_wordpress(site, sizes)
        _look_for_signage(site, running)
        # A name that only sends visitors elsewhere has no files to find, so files that aren't there are not a
        # problem with it: the rules are the whole site, and they are what moves.
        if redirect_only(site) and site["problem"] in (None, NO_DOCROOT):
            site["problem"] = None
        elif not site["wordpress"] and (site.get("redirect") or {}).get("rules"):
            # A site that is copied by hand leaves its rules behind: they are in the web server's own
            # configuration, which nobody copies with the files. Said, so they are not lost with it.
            site["redirect"]["by_hand"].insert(
                0, REDIRECT_IN_CONFIG % "; ".join(site["redirect"]["rules"]))
    return vhosts


def _signage_behind(run, sites_dir, notes):
    """{name: the piSignage server behind it}. A machine without piSignage, or without the ss that finds what
    listens here, simply has none: the sites are read either way."""
    try:
        return dict((one["name"], one) for one in signage_servers(run, sites_dir, notes=notes))
    except (CommandFailed, OSError):
        return {}


def _look_for_signage(site, running):
    """A name that Apache only hands on to a piSignage server on this machine. It is not a website with files
    to copy, and the proxy that puts it in front of the app is what makes it one: naming that as something a
    person has to set up by hand says the opposite of the truth."""
    found = running.get(site["name"])
    site["signage"] = found
    if not found:
        return
    redirect = site.get("redirect") or {}
    port = ":%d" % found["port"]
    left = [line for line in redirect.get("by_hand") or [] if port not in line]
    if redirect:
        redirect["by_hand"] = left
        site["redirect"] = redirect if redirect.get("rules") or left else None
    if site.get("problem") == NO_DOCROOT:
        site["problem"] = None  # it has no web root because it has no files: the app answers for it


NO_DOCROOT = "Its configuration has no DocumentRoot, so its files can't be found."


# What a web root holds when there is no site in it: a directory left behind with a placeholder page in it.
_NO_SITE_IN_IT = ("index.html", "index.htm", "cgi-bin")
REDIRECT_IN_CONFIG = ("its redirect is in the web server's own configuration here, not in the site's files, so "
                      "copying the files doesn't bring it: %s")


def _serves_files(docroot):
    """Whether a web root holds anything a server would serve. A directory that isn't there, is empty, or holds
    nothing but a placeholder page is no site; one this tool can't read is taken for a site, since the careful
    answer is the one that has a person look at it."""
    if not docroot:
        return False
    try:
        if not os.path.isdir(docroot):
            return False
        return bool([name for name in os.listdir(docroot)
                     if not name.startswith(".") and name.lower() not in _NO_SITE_IN_IT])
    except OSError:
        return True


def redirect_only(site):
    """Whether this name serves nothing of its own, so its rules are the whole site and it moves without one
    byte being copied.

    Rules on their own don't make a redirect: a site that is not WordPress may have files and a rule about one
    path among them, and taking that for the whole site would move the rule and quietly leave the site behind.
    So either a rule catches every address, which means nothing in the web root is ever served, or there is
    nothing in the web root to serve.
    """
    redirect = site.get("redirect") or {}
    if site.get("wordpress") or not redirect.get("rules"):
        return False
    return bool(redirect.get("to")) or not _serves_files(site.get("docroot"))


def _look_for_wordpress(site, sizes):
    site.update(wordpress=False, database=None, db_user=None, db_password=None, db_host=None, prefix=None, size=None)
    if site["docroot"] is None:
        site["problem"] = site["problem"] or NO_DOCROOT
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


# How long each request may take, in seconds. Asking for the status of a whole server is many little commands
# there, one for each item; making something gets half an hour, as before.
ASK_TIMEOUTS = {"apply": 1800, "finish_site": 1800, "status": 600, "bayes_swap": 600,
                "point_dns": 1200, "repair": 1200,  # up to 15 minutes there for the DNS and the certificate
                "mail_sizes": 600, "compare_mail": 1800,  # counting a big mailbox over IMAP takes minutes
                "adopt_database": 1800,  # reading a big dump in takes as long as making a whole mail domain
                "webmail_place": 60, "restore_webmail": 3600}  # a few thousand contacts, account by account
COMPARE_TIMEOUT = ASK_TIMEOUTS["compare_mail"]
ADOPT_TIMEOUT = ASK_TIMEOUTS["adopt_database"]
MIGRATECTL = "/usr/local/bin/migratectl"  # where the basic role installs it on the new server


def own_migratectl(path=None):
    """(text, sha256) of the migratectl.py beside this file, when it is there and speaks this file's version: the
    new server's half of this same release, which this file puts there itself. None when it isn't."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "migratectl.py")
    try:
        with open(path, "rb") as file:
            data = file.read()
        text = data.decode("utf-8")
    except (OSError, ValueError):
        return None
    found = re.search(r"^PROTOCOL_VERSION = ([0-9]+)", text, re.M)
    if not found or int(found.group(1)) != PROTOCOL_VERSION:
        return None
    return text, hashlib.sha256(data).hexdigest()


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

    def _command(self, host_check, words=("migratectl",)):
        return self.command(*words, host_check=host_check)

    def _once(self, host_check, line, timeout, what, words=("migratectl",)):
        # LC_ALL=C: the failures are told apart by ssh's and sudo's English words. Bytes are decoded here, since a
        # banner or a translated message with an odd byte must not become a traceback.
        try:
            process = subprocess.Popen(self._command(host_check, words), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=dict(os.environ, LC_ALL="C"))
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
        It waits 2 minutes for a quick request, 10 for the status of everything and 30 for one that makes
        something."""
        kind = request.get("request") if isinstance(request, dict) else None
        slow = kind in ("apply", "finish_site")
        timeout = timeout or ASK_TIMEOUTS.get(kind, 120)
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

    def install(self, text):
        """Puts 'text' on the new server as its migratectl. It is written beside the one there and moved over it,
        so a connection lost halfway leaves that one whole."""
        script = "cat > {0}.new && chmod 0755 {0}.new && mv {0}.new {0}".format(MIGRATECTL)
        code, out, err = self._once(self.host_check, text, 120, "taking its new migratectl",
                                    words=("sh", "-c", shlex.quote(script)))
        if code == 255:
            self._answer(code, out, err)  # ssh's own failures, in the words ask() uses for them
        if code != 0:
            raise Stopped("I couldn't put this release's migratectl on the new server (code %d).%s"
                          % (code, " The server said: %s" % err if err else ""))

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
    own = own_migratectl()
    # The new server's half is brought up to this file's release here, so it never has to be copied there by
    # hand: when it is older, and when it is this release but not the same file. One that is newer is left alone.
    if own and (version < PROTOCOL_VERSION
                or (version == PROTOCOL_VERSION and answer.get("sha256") not in (None, own[1]))):
        connection.install(own[0])
        print("The new server had another migratectl; I put the one beside me in its place.")
        answer = connection.ask({"request": "hello"})
        version = answer.get("version") if answer.get("ok") else None
    if version != PROTOCOL_VERSION:
        raise Stopped("The new server was built from a different release of this repository than this file (it "
                      "reports %s, this file is %d). Both halves must come from the same release: put "
                      "migratectl.py from this release beside me and I bring the new server's half up to date "
                      "myself, or copy this file again from the release the new server was built with."
                      % (version, PROTOCOL_VERSION))
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
    answer = _making(connection, request)
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
        answer = _making(connection, request)
        _tell_note(answer, say, told)
    return answer, skipped


def _making(connection, request):
    """The new server's answer to an apply, with a spinner while it makes it: a big mail domain takes minutes."""
    return _step("Making it on %s" % _short_name(getattr(connection, "host", None) or "the new server"),
                 lambda: connection.ask(request),
                 lambda said: "done" if said.get("ok") else "a question" if said.get("ask")
                 else "it needs passwords" if said.get("needs_password") else "failed")


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
        if item.get("redirect"):
            return "a redirect, and no files"
        if not item.get("database"):
            return "the site's files"
        size = _size(item.get("size"))
        return "files and a database of %s" % size if size else "files and a database"
    if kind == "mail_domain":
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        accounts, forwards = data.get("addresses"), data.get("forwards")
        parts = []
        if isinstance(accounts, list) and accounts:
            parts.append(_plural(len(accounts), "account", "accounts"))
        if isinstance(forwards, list) and forwards:
            parts.append(_plural(len(forwards), "forward", "forwards"))
        return " and ".join(parts) or "the mail settings"
    if kind == "cron":
        return "one job"
    return ""


MAIL_MERGES = ("Sending it again adds the accounts, forwards, filters and "
               "spam settings that are missing, and leaves what is already "
               "there alone.")
GONE_THERE = ("This moved from here before, but the new server doesn't have "
              "it any more. Moving it again puts it back.")

def _not_asked(item, moved_before):
    """The verdict for an item the new server was not asked about. A cron job is only asked about when a
    customer is looked into or moved, because answering about it means reading a crontab there, and that is not
    what starting up should cost. Nothing is made up here: what is used is the item's own problem, which this
    server can see, and what the notes remember moving from here, which counts as done until the new server is
    asked about it."""
    if item.get("problem"):
        return "needs a look first", False, item["problem"]
    if moved_before:
        return "already moved from here", False, None
    return cost(item), True, None


def _verdict(item, status, notes=None):
    """(what the new server said in short, can it move, the longer reason or None). 'notes' is the State: a site
    it remembers as made but not filled is offered again, for its files and database only, and anything it
    remembers as moved is kept off the list for as long as the new server still has it (a site that moved and is
    still there is caught up with --again). What the notes remember never outranks what the new server says: an
    item that was deleted there is offered again, and said to be. An item 'status' says nothing about at all was
    not asked about, which is not the same as the new server not answering: see _not_asked."""
    said = status.get(item.get("id")) if isinstance(status, dict) else None
    state = said.get("state") if isinstance(said, dict) else None
    detail = said.get("detail") if isinstance(said, dict) else None
    moved_before = notes is not None and notes.entry(item).get("status") == "moved"
    if not isinstance(said, dict):
        return _not_asked(item, moved_before)
    if moved_before and state in ("exists", "refused"):
        return "already moved from here", False, None
    if state == "exists" and notes is not None and notes.outstanding(item):
        return "made, files still to come", True, None
    if state == "exists" and item.get("kind") == "mail_domain":
        # Importing a mail domain only adds what is missing, which is how a
        # domain that is half there is finished. That is why the design asks
        # no question for it, so it must stay on the list.
        return "already there, can be sent again", True, MAIL_MERGES
    if state == "exists":
        return "already on the new server", False, detail
    if state == "refused":
        return "can't be moved", False, detail
    if state != "new":
        return "the new server didn't say", False, "The new server gave no answer about this."
    if item.get("problem"):
        return "needs a look first", False, item["problem"]
    if moved_before:
        return cost(item), True, GONE_THERE
    return cost(item), True, None


# ---- Colour, and a loader ---------------------------------------------------

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def use_colour(stream=None):
    """Colour only on a terminal, and never when NO_COLOR is set. Colour is
    never what carries a meaning: every line reads the same without it."""
    if "NO_COLOR" in os.environ:
        return False
    try:
        return bool((stream or sys.stdout).isatty())
    except (AttributeError, ValueError):
        return False


def _wanted_colour(args=None, stream=None):
    """Whether what is printed gets colour: not with --no-colour, not with
    NO_COLOR, and not when the output is piped into something else."""
    if getattr(args, "no_colour", False):
        return False
    return use_colour(stream)


def _paint(text, style, on):
    if not on or not text or style is None:
        return text
    return "\x1b[%sm%s\x1b[0m" % (_CODES[style], text)


# The look of mailctl and domainctl (serverctl.ui, which is Rich), written out again: this file has nothing but
# the standard library. main() sets both from the terminal it prints to. 'fancy' is whether that terminal takes
# UTF-8: Python 3.6 under a C locale can't print a check mark at all, so then the marks are plain letters.
LOOK = {"colour": False, "fancy": False}
_MARKS = {"ok": ("✓", "+", "green"), "warn": ("!", "!", "yellow"), "fail": ("✗", "x", "red")}
_SPINNERS = ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "|/-\\")


def set_look(colour, stream=None):
    LOOK["colour"] = bool(colour)
    LOOK["fancy"] = "utf" in (getattr(stream or sys.stdout, "encoding", None) or "").lower()


def _mark(kind):
    fancy, plain, style = _MARKS[kind]
    return _paint(fancy if LOOK["fancy"] else plain, style, LOOK["colour"])


def _marked(kind, text, say=print):
    """One message the way mailctl prints it: its mark, then the text, wrapped in under itself."""
    lines = textwrap.wrap(str(text), _WIDTH - 2) or [""]
    say("\n".join(["%s %s" % (_mark(kind), lines[0])] + ["  " + line for line in lines[1:]]))


def success(text, say=print):
    _marked("ok", text, say)


def warn(text, say=print):
    _marked("warn", text, say)


def fail(text, say=print):
    _marked("fail", text, say)


def note(text, say=print):
    say("\n".join(_paint(line, "dim", LOOK["colour"]) for line in textwrap.wrap(str(text), _WIDTH)))


def heading(text, say=print):
    say("")
    say(_paint(_fit(str(text), _WIDTH), "bold", LOOK["colour"]))


def _rule(width):
    """The heavy line under the head of a table, as in mailctl's tables."""
    return ("━" if LOOK["fancy"] else "-") * width


def _step(label, work, summary=None, out=None):
    """Runs work() and prints 'label ... what came of it' as ONE finished
    line. On a terminal a spinner turns after the dots while it runs, with the
    time it has taken once that is more than a few seconds; when output is
    not a terminal there is no spinner and no carriage return, just the
    finished line. Whatever happens in work(), the line is finished ('failed',
    or 'stopped' for Ctrl-C) before the error goes on."""
    out = out if out is not None else sys.stdout
    try:
        tty = bool(out.isatty())
    except (AttributeError, ValueError):
        tty = False
    start = "%s ... " % label
    stop, spinner = threading.Event(), None
    frames = _SPINNERS[0 if LOOK["fancy"] else 1]
    if tty:
        def spin():
            frame = 0
            while not stop.is_set():
                seconds = frame // 10
                taken = " %d:%02d" % (seconds // 60, seconds % 60) if seconds >= 5 else ""
                try:
                    out.write("\r%s%s%s" % (start, _paint(frames[frame % len(frames)], "yellow", LOOK["colour"]),
                                            _paint(taken, "dim", LOOK["colour"])))
                    out.flush()
                except (OSError, ValueError):
                    return
                frame += 1
                stop.wait(0.1)
        spinner = threading.Thread(target=spin)
        spinner.daemon = True
        spinner.start()

    def finish(text):
        if spinner is not None:
            stop.set()
            spinner.join()
        style = {"failed": "red", "stopped": "yellow"}.get(text, "green")
        out.write(("\r%s%s\x1b[K\n" if tty else "%s%s\n") % (start, _paint(text, style, LOOK["colour"])))
        out.flush()

    try:
        result = work()
        text = summary(result) if summary else "done"
    except BaseException as problem:
        finish("stopped" if isinstance(problem, KeyboardInterrupt) else "failed")
        raise
    finish(text)
    return result


# ---- The dashboard -----------------------------------------------------------

SECTIONS = (("mail_domain", "MAIL"), ("site", "WEBSITES"), ("cron", "CRON JOBS"))
_INDENT = 7  # the width of the number column and the gap after it
_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_AT_WORDS = {"@hourly": "every hour", "@daily": "daily at 00:00", "@midnight": "daily at 00:00",
             "@weekly": "every Sunday at 00:00", "@monthly": "monthly, on the 1st at 00:00",
             "@yearly": "every year on 1 January", "@annually": "every year on 1 January",
             "@reboot": "when the server starts"}


def _ordered(inventory):
    """The inventory in the order the dashboard shows it: a section after a section, as it came inside one."""
    rank = dict((kind, number) for number, (kind, _) in enumerate(SECTIONS))
    return sorted((item for item in inventory if item.get("kind") != "aside"),
                  key=lambda item: rank.get(item.get("kind"), len(rank)))


def offered(inventory, status, notes=None):
    """The items that can be moved, in the order the dashboard numbers them."""
    return [item for item in _ordered(inventory) if _verdict(item, status, notes)[1]]


def _fit(text, width, middle=False):
    """Text cut to the width. A name is cut in the middle (middle=True): its end tells two names apart."""
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    if middle:
        head = (width - 3 + 1) // 2
        return text[:head] + "..." + text[len(text) - (width - 3 - head):]
    return text[:width - 3] + "..."


def _fit_left(text, width):
    """Text cut from the LEFT, so the end of a command (the script) is what stays."""
    text = " ".join(str(text).split())
    return text if len(text) <= width else "..." + text[len(text) - (width - 3):]


def _clock(minute, hour):
    if re.fullmatch(r"[0-9]{1,2}", minute) and re.fullmatch(r"[0-9]{1,2}", hour) \
            and int(minute) < 60 and int(hour) < 24:
        return "%02d:%02d" % (int(hour), int(minute))
    return None


def schedule_in_words(fields):
    """The five time fields of a cron line (or its one @word) as a short phrase; the raw fields when it cannot
    be phrased plainly."""
    raw = " ".join(fields)
    if len(fields) == 1:
        return _AT_WORDS.get(fields[0].lower(), raw)
    if len(fields) != 5:
        return raw
    minute, hour, day, month, weekday = fields
    step = re.fullmatch(r"\*/([1-9][0-9]*)", minute)
    every = re.fullmatch(r"\*/([1-9][0-9]*)", hour)
    if (day, month, weekday) == ("*", "*", "*"):
        if (minute, hour) == ("*", "*"):
            return "every minute"
        if step and hour == "*":
            return "every %s minutes" % step.group(1) if step.group(1) != "1" else "every minute"
        if every and minute == "0":
            return "every %s hours" % every.group(1) if every.group(1) != "1" else "every hour"
        if hour == "*" and minute == "0":
            return "every hour"
        if _clock(minute, hour):
            return "daily at %s" % _clock(minute, hour)
    elif (day, month) == ("*", "*") and re.fullmatch(r"[0-7]", weekday) and _clock(minute, hour):
        return "every %s at %s" % (_DAYS[int(weekday) % 7], _clock(minute, hour))
    elif month == "*" and weekday == "*" and re.fullmatch(r"[0-9]{1,2}", day) \
            and 1 <= int(day) <= 31 and _clock(minute, hour):
        return "on day %d of each month at %s" % (int(day), _clock(minute, hour))
    return raw


def _cron_parts_of(item):
    """(the time fields, the command) of a cron item, from its line."""
    words = str(item.get("line") or "").split()
    count = 1 if words and words[0].startswith("@") else 5
    return words[:count], " ".join(words[count:])


def _there_text(item, info, reason, said, short):
    """(what the new server has, in a few words, its colour) for the column that is headed with its name."""
    if not isinstance(said, dict):
        return "not asked yet", "dim"  # a cron job: only asked about inside a customer
    state = said.get("state") if isinstance(said, dict) else None
    detail = said.get("detail") if isinstance(said, dict) else None
    if state == "exists" and item.get("kind") == "mail_domain" and info == "already there, can be sent again":
        counts = re.search(r"with ([0-9]+) accounts and ([0-9]+) forwards", str(said.get("detail") or ""))
        text = (_counts(int(counts.group(1)), int(counts.group(2))).replace(" and ", ", ")
                if counts else "already there")
        return text + " - can be topped up", "green"
    if state == "new":
        if reason == GONE_THERE:
            return "not there any more", "dim"
        if item.get("kind") == "cron":
            if not item.get("site"):
                return "not checked", "dim"
            return "not in %s's crontab" % (item.get("user") or item.get("old_user") or "its user"), "dim"
        return "not there yet", "dim"
    if info in ("already moved from here", "already on the new server"):
        return info, "green"
    return info, ("yellow" if info in TROUBLE else None)


TROUBLE = ("needs a look first", "can't be moved", "the new server didn't say")


def _here_text(item):
    kind = item.get("kind")
    if kind == "mail_domain":
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        accounts = data.get("addresses")
        return _plural(len(accounts), "account", "accounts") if isinstance(accounts, list) else "mail"
    if kind == "site":
        return "WordPress" if item.get("wordpress", True) else "not WordPress"
    return schedule_in_words(_cron_parts_of(item)[0])


def _copy_text(item):
    if item.get("kind") != "site":
        return ""
    if not item.get("database"):
        return "files"
    size = _size(item.get("size"))
    return "%s + files" % size if size else "database + files"


def _table(kind, group, numbers, status, remembered, short, colour):
    """The lines of one section. 'group' is [(item, verdict)], 'numbers' {id(item): its number or None}."""
    if kind == "cron":
        heads = ["For", "Runs", "On " + short]
    elif kind == "site":
        heads = ["Domain", "Here", "On " + short, "To copy"]
    else:
        heads = ["Domain", "Here", "On " + short]
    rows = []
    for item, (info, movable, reason) in group:
        said = status.get(item.get("id")) if isinstance(status, dict) else None
        there, style = _there_text(item, info, reason, said, short)
        first = _title(item) if kind != "cron" else str(item.get("site") or item.get("old_user")
                                                        or item.get("user") or "?")
        cells = [(first, None), (_here_text(item), None), (there, style)]
        if kind == "site":
            cells.append((_copy_text(item) if movable else "-", None if movable else "dim"))
        rows.append((item, numbers.get(id(item)), cells, reason, movable, info))
    shared = {}
    for item, number, cells, reason, movable, info in rows:
        if reason:
            shared.setdefault(reason, []).append(cells[0][0])
    shared = dict((reason, names) for reason, names in shared.items() if len(names) > 1)
    fields = len(heads)
    widths = [max([len(heads[i])] + [len(" ".join(str(row[2][i][0]).split())) for row in rows])
              for i in range(fields)]
    room = _WIDTH - _INDENT - 2 * (fields - 1)
    total = sum(widths)
    for i in [0, fields - 1] + list(range(1, fields - 1)):
        cut = min(total - room, widths[i] - (24 if i == 0 else len(heads[i])))
        if cut > 0:
            widths[i] -= cut
            total -= cut
    out = ["%s%s" % (" " * 2 + _paint("No.", "bold", colour) + "  ",
                     "  ".join(_paint(h, "bold", colour) + " " * (w - len(h))
                               for h, w in zip(heads, widths)).rstrip())]
    for item, number, cells, reason, movable, info in rows:
        label = "%4s" % (number if number else "-")
        line = _paint(label, "bold" if number else "dim", colour) + "   "
        extra = []
        parts = []
        for i, (text, style) in enumerate(cells):
            text = " ".join(str(text).split())
            shown = _fit(text, widths[i], middle=(i == 0)) if len(text) > widths[i] else text
            if i and shown != text:
                extra.append(text)
            pad = "" if i == fields - 1 else " " * (widths[i] - len(shown))
            parts.append(_paint(shown, style, colour) + pad)
        out.append(line + "  ".join(parts))
        if kind == "cron":
            command = _cron_parts_of(item)[1]
            if command:
                out.append(" " * _INDENT + _fit_left(command, _WIDTH - _INDENT))
        notes = ([(reason, "yellow" if info in TROUBLE else None)]
                 if reason and reason not in shared else []) \
            + [(text, None) for text in extra]
        for text, style in notes:
            for piece in textwrap.wrap(text, _WIDTH - _INDENT, break_long_words=True):
                out.append(" " * _INDENT + _paint(piece, style, colour))
    for reason, names in sorted(shared.items(), key=lambda pair: -len(pair[1])):
        out.append("")
        for piece in textwrap.wrap(reason, _WIDTH - 2, break_long_words=True):
            out.append("  " + _paint(piece, "yellow", colour))
        for piece in textwrap.wrap("Applies to: " + ", ".join(names),
                                   _WIDTH - 2, break_long_words=True):
            out.append("  " + piece)
    return out


def _short_name(name):
    if not name:
        return "the new server"
    if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", name):
        return name
    return name.split(".")[0]


def _loose_job(item, colour):
    """One line for a job that belongs to no site."""
    fields, command = _cron_parts_of(item)
    head = "  %s: %s  " % (item.get("old_user") or item.get("user") or "?", schedule_in_words(fields))
    return _fit_line(head + _fit_left(command, max(_WIDTH - len(head), 10)))


def items_board(inventory, status, remembered=None, colour=False, old_server=None, new_server=None,
                addresses=None, detail=False, numbers=None, heading=True):
    """Every item on its own line: a section for each kind, with the columns that kind needs. This is what
    `migrate.py list` prints; the screen the tool is worked from is dashboard(), which has one row for a whole
    customer. 'status' is what the new server said about each item, by id: {id: {"state", "detail"}}. A reason
    that several rows share is said once, with their names. A cron job that belongs to no site gets no row:
    they are counted in one line (and listed, one line each, with 'detail'). The old server's own webmail and
    autodiscover addresses ('aside' items) are one line with a count. 'numbers' is {id(item): the number to
    type for it}, which is its customer's; an item that is in none gets a dash, and without 'numbers' at all
    the items are numbered one by one. 'heading' starts it with the two servers' names."""
    asides = [item for item in inventory if item.get("kind") == "aside"]
    inventory = [item for item in inventory if item.get("kind") != "aside"]
    if not inventory and not asides:
        return "Nothing was found on this server to move."
    short = _fit(_short_name(new_server), 16)
    ordered = _ordered(inventory)
    given, numbers, count = numbers, {}, 0
    verdicts = [(item, _verdict(item, status, remembered)) for item in ordered]
    for item, (_, movable, _) in verdicts:
        if movable:
            count += 1
            numbers[id(item)] = given.get(id(item)) if given is not None else count
    there = _fit(new_server or "(not known)", 50) + (" (%s)" % ", ".join(addresses) if addresses else "")
    out = []
    if heading:
        out.extend([_paint("Old server", "bold", colour) + "  "
                    + _fit(old_server or socket.gethostname(), 60, True),
                    _paint("New server", "bold", colour) + "  " + there])
    kinds = [kind for kind, _ in SECTIONS] + [None]
    total = len(inventory)
    for kind in kinds:
        group = [pair for pair in verdicts if (pair[0].get("kind") == kind if kind else
                                               pair[0].get("kind") not in dict(SECTIONS))]
        loose = []
        if kind == "cron":
            loose = [pair[0] for pair in group if not pair[0].get("site")]
            group = [pair for pair in group if pair[0].get("site")]
            total -= len(loose)
        if not group and not loose:
            continue
        out.extend(["", _paint(dict(SECTIONS).get(kind, "OTHER"), "bold", colour)])
        if group:
            out.extend(_table(kind, group, numbers, status, remembered, short, colour))
        if loose:
            if group:
                out.append("")
            out.extend("  " + piece for piece in textwrap.wrap(
                "%s no site here, so %s only reported, and the new server has its own%s."
                % (_plural(len(loose), "more job belongs to" if group else "job belongs to",
                           "more jobs belong to" if group else "jobs belong to"),
                   "it is" if len(loose) == 1 else "they are",
                   "" if detail else ". migrate.py list shows them"), _WIDTH - 2))
            if detail:
                out.extend(_loose_job(item, colour) for item in loose)
    if asides:
        out.extend([""] + textwrap.wrap(
            "Not sites: %s (vhosts) of this server's own webmail, autodiscover and autoconfig are left out; the new "
            "server has its own." % _plural(len(asides), "name", "names"), _WIDTH))
    out.append("")
    out.append("%s here, %d of them to do." % (_plural(total, "item", "items"), count) if total
               else "Nothing here to do.")
    while out and not out[0]:
        out.pop(0)
    return "\n".join(_fit_line(line) for line in out)


def _fit_line(line):
    """A line cut to the width; colour codes do not count towards it. What it is indented by stays in front of
    it: a line that loses that falls out of the column it was written in, and looks like another one."""
    plain = re.sub(r"\x1b\[[0-9;]*[mK]", "", line)
    if len(plain) <= _WIDTH:
        return line
    rest = plain.lstrip()
    indent = plain[:len(plain) - len(rest)]
    if not rest or len(indent) > _WIDTH // 2:
        return _fit(plain, _WIDTH)
    return indent + _fit(rest, _WIDTH - len(indent))


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
        numbers = _number_words(word, len(lines), notes)
        if numbers is None:
            unreadable.append(word)
        else:
            chosen.update(numbers)
    if unreadable:
        say("I don't understand: %s. Type numbers like 1 3, a range like 2-5, or all." % ", ".join(unreadable))
    for note in notes:
        say(note)
    return [lines[number - 1] for number in sorted(chosen)]


# ---- One customer, one number ------------------------------------------------

# The parts of a customer, in the order a move has to do them: the mail domain makes the accounts, a site
# brings its database, and a job needs the files of its site.
PARTS = ("mail", "site", "cron")
PART_OF = {"mail_domain": "mail", "site": "site", "cron": "cron"}
PART_HEADS = {"mail": "Mail", "site": "Website", "cron": "Cron"}
# What may be typed behind a number to do one part only. 'all' behind a number is every part of it.
PART_WORDS = {"mail": "mail", "domain": "mail", "accounts": "mail", "site": "site", "website": "site",
              "cron": "cron", "job": "cron", "jobs": "cron"}
# The only words a column of the screen ever says. Four of them, and each means the same in every column.
TO_DO, PART, DONE, NONE = "to do", "part", "done", "-"
# Something that is here and this tool can't move, like a website that isn't WordPress: it is still work, so it
# says so in the row instead of being counted in a sentence under the table. The reason stands on the
# customer's own screen.
BY_HAND = "by hand"
# A name that is an application and not a website: a piSignage server. It is not work by hand, since g on the
# customer's screen moves it, and it is not a site to copy either, so it says what it is.
SIGNAGE = "signage"
# A site that is not WordPress and not a redirect: its files and one database can be copied, but the line that
# holds that database's password is somewhere in its own code, so a person finishes it. It is work this tool
# does half of, which is neither "to do" nor "by hand".
FILES = "copy it"
# The DNS row has one word of its own: where a name points is asked of the internet, and the internet is the
# one thing here that can be out of reach.
CANT_TELL = "can't tell"
WORD_STYLES = {DONE: "green", PART: "yellow", TO_DO: "cyan", NONE: "dim", "refused": "red",
               CANT_TELL: "dim", BY_HAND: "yellow", SIGNAGE: "cyan", FILES: "cyan"}
ALSO = "Also, only reported: %s. See: migrate.py list"


def _mail_here(item):
    """(how many accounts, how many forwards) a mail domain has on this server."""
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    accounts, forwards = data.get("addresses"), data.get("forwards")
    return (len(accounts) if isinstance(accounts, list) else 0,
            len(forwards) if isinstance(forwards, list) else 0)


def _mail_there(detail):
    """(how many accounts, how many forwards) the new server said it has, or None when it didn't say."""
    counts = re.search(r"with ([0-9]+) accounts and ([0-9]+) forwards", str(detail or ""))
    return (int(counts.group(1)), int(counts.group(2))) if counts else None


def _item_word(item, status, notes):
    """Where one item stands, in one of the three words the screen says: 'done', 'part' or 'to do'. None means
    this tool never moves it: it is reported and left alone, and the customer's own screen says why. What the
    new server says outranks the notes: something that moved from here and is not there any more is to do
    again."""
    said = status.get(item.get("id")) if isinstance(status, dict) else None
    state = said.get("state") if isinstance(said, dict) else None
    detail = str(said.get("detail") or "") if isinstance(said, dict) else ""
    entry = notes.entry(item) if notes is not None else {}
    moved = entry.get("status") == "moved"
    kind = item.get("kind")
    if kind == "mail_domain":
        if state == "exists":
            there, here = _mail_there(detail), _mail_here(item)
            return DONE if there and sum(there) >= sum(here) else PART
        return DONE if moved and state is None else TO_DO
    if item.get("signage"):
        # An application behind a name, not a website: the only thing that moves it is g on the customer's
        # screen, so it never reads as a site waiting to be copied, nor as work nobody can do.
        return DONE if state == "exists" else SIGNAGE
    if kind == "site" and item.get("other"):
        # Files and a database this tool copies, and settings only a person can write: f does its half.
        return DONE if entry.get("status") == "moved" and state == "exists" else FILES
    if state == "refused":
        return None
    if item.get("problem"):
        # It is here and it isn't moving: say so where it stands, with the reason under it. A cron job of the
        # server itself belongs to no customer, so it stays out of the rows and is counted below them.
        return None if _cron_by_hand(item) else BY_HAND
    if kind == "site" and entry.get("status") == "made" and state in (None, "exists"):
        return PART  # made on the new server, its files and its database still to come
    if state == "exists":
        return DONE
    return DONE if moved and state is None else TO_DO


class Customer(object):
    """One customer's domain, which is one row of the screen: every item of that domain by part, where each
    part stands, and what the one number next to it does next. A domain this tool never moves anything of is
    no row at all: it is counted in the sentence under the table."""

    def __init__(self, name):
        self.name = name
        self.number = None
        self.items = dict((part, []) for part in PARTS)
        self.words = dict((part, []) for part in PARTS)

    def add(self, part, item, word):
        self.items[part].append(item)
        self.words[part].append(word)

    def sort(self):
        for part in PARTS:
            pairs = sorted(zip(self.items[part], self.words[part]), key=lambda pair: _title(pair[0]))
            self.items[part] = [item for item, _ in pairs]
            self.words[part] = [word for _, word in pairs]

    def word(self, part):
        """Where a whole part stands: '-' when there is nothing of it to move, 'by hand' when everything of it
        that is here is work for a person, else done, part or to do."""
        words = [word for word in self.words[part] if word is not None]
        if not words:
            return NONE
        if all(word == DONE for word in words):
            return DONE
        if all(word == SIGNAGE for word in words):
            return SIGNAGE
        if all(word == FILES for word in words):
            return FILES
        if all(word in (BY_HAND, SIGNAGE, FILES) for word in words):
            return BY_HAND
        if any(word in (DONE, PART) for word in words):
            return PART
        return TO_DO

    def every(self):
        """Every item of this customer, in the order the parts are moved."""
        return [item for part in PARTS for item in self.items[part]]

    def by_hand(self, part=None):
        """Whether something of this customer is here that only a person can move."""
        parts = (part,) if part else PARTS
        return any(word == BY_HAND for name in parts for word in self.words[name])

    def to_move(self, part=None):
        """What is left to move: the whole customer, in the order that works, or one part of it."""
        out = []
        for name in ((part,) if part else PARTS):
            out.extend(item for item, word in zip(self.items[name], self.words[name])
                       if word in (TO_DO, PART))
        return out

    def is_customer(self):
        """Whether this domain is a customer to move at all: something of it moved or can move."""
        return any(word is not None for part in PARTS for word in self.words[part])

    def accounts(self):
        """Every mail address of this customer on this server, in the order they were found."""
        out = []
        for item in self.items["mail"]:
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            for account in data.get("addresses") or []:
                address = str(account.get("address") or "").strip().lower() \
                    if isinstance(account, dict) else ""
                if address and address not in out:
                    out.append(address)
        return out


def _about(item):
    """The domain an item is about: a cron job's is its site's, everything else's is its own name."""
    return item.get("site") if item.get("kind") == "cron" else item.get("name")


def _base_names(inventory):
    """The names a row can be about: the mail domains and the sites this server has."""
    return set(str(item.get("name") or "").strip().lower() for item in inventory
               if item.get("kind") in ("mail_domain", "site") and item.get("name"))


def _row_name(name, bases):
    """Which row a name belongs in: the shortest of 'bases' it is part of, so shop.example.nl lands in the row
    of example.nl, which is the one customer."""
    name = str(name or "").strip().lower()
    if not name:
        return None
    for base in sorted(bases, key=lambda text: (len(text), text)):
        if _in_domain(name, base):
            return base
    return name


def _rows_of(inventory, status, notes=None):
    """Every domain the inventory touches, customer or not, sorted by name."""
    bases, found = _base_names(inventory), {}
    for item in inventory:
        part = PART_OF.get(item.get("kind"))
        name = _row_name(_about(item), bases) if part else None
        if not name:
            continue
        row = found.get(name)
        if row is None:
            row = found[name] = Customer(name)
        row.add(part, item, _item_word(item, status, notes))
    for row in found.values():
        row.sort()
    return [found[name] for name in sorted(found)]


def customers(inventory, status, notes=None):
    """The customers to move, sorted by domain and numbered. One number stands for one customer and does its
    next step. A domain this tool never moves anything of (a site that is not WordPress, a name with nothing
    behind it) is not among them; left_over() counts those."""
    rows = [row for row in _rows_of(inventory, status, notes) if row.is_customer()]
    for number, row in enumerate(rows, 1):
        row.number = number
    return rows


def left_over(inventory, status, notes=None, databases=None):
    """What is only reported, as counted phrases: no row of its own anywhere on the screen."""
    quiet = [row for row in _rows_of(inventory, status, notes) if not row.is_customer()]
    counted = (
        (len(quiet), "name with nothing to move", "names with nothing to move"),
        (len([item for item in inventory if _cron_by_hand(item)]),
         "cron job of this server itself", "cron jobs of this server itself"),
        (len([item for item in inventory if item.get("kind") == "aside"]),
         "webmail name", "webmail names"),
        (len(databases or ()),
         "database that belongs to no site", "databases that belong to no site"))
    return [_plural(count, one, many) for count, one, many in counted if count]


# What the world is still sent to this server for, per customer, from the look around at the start. The board
# would otherwise call a customer whose mail and website are both across "nothing to move" while every visitor
# and every message still comes here, which is the one thing left to do and the one nobody sees.
_STILL_HERE = {}


def still_here(name):
    """What a customer still needs that is not an item to move, in the few words the board has room for."""
    return _STILL_HERE.get(name)


def forget_still_here(name=None):
    """After a DNS move or a handover, so the board is drawn from what is true now."""
    _STILL_HERE.pop(name, None) if name else _STILL_HERE.clear()


MOVE_SIGNAGE = "move the piSignage server"
COPY_FILES = "copy its files"
COPY_MESSAGES = "copy the messages"


def next_step(row):
    """(what typing this customer's number moves, in the order that works; what that is, in plain words). It is
    one step: the first part that is not done yet. With nothing done at all there is nothing to go in order
    after, so then one number is the whole customer."""
    left = [part for part in PARTS if row.to_move(part)]
    if not left:
        # Before the DNS of the mail: the world would be sent to a server that hasn't got the messages, and
        # what is still here would stay here.
        if messages_behind(row.name):
            return [], COPY_MESSAGES
        if any(word == FILES for word in row.words["site"]):
            return [], COPY_FILES
        if any(word == SIGNAGE for word in row.words["site"]):
            # Before its DNS, not after: moving the name first would send its players at a server that hasn't
            # got the thing they talk to.
            return [], MOVE_SIGNAGE
        return [], (still_here(row.name) or (BY_HAND if row.by_hand() else "nothing to move"))
    started = any(row.word(part) in (DONE, PART) for part in PARTS)
    if not started and len(left) > 1:
        return row.to_move(), "move everything"
    part = left[0]
    return row.to_move(part), _step_words(row, part)


def only_redirects(row):
    """Whether this customer's website is nothing but a redirect, which is a different piece of work: there is
    nothing to copy, only rules to write on the new server."""
    sites = row.items["site"]
    return bool(sites) and all(item.get("redirect") for item in sites)


def _step_words(row, part):
    """What doing one part of one customer comes to, in words that fit a column."""
    begun = row.word(part) == PART
    if part == "mail":
        return "finish the mail" if begun else "move the mail"
    if part == "site":
        if only_redirects(row):
            return "set up the redirect"
        return "finish the website" if begun else "copy the website"
    if begun:
        return "move the rest"
    jobs = len(row.to_move("cron"))
    return "move the cron job" if jobs == 1 else "move the %d cron jobs" % jobs


# ---- The screen --------------------------------------------------------------

# The customer column never gets narrower than this: the name is what every other column is about and what
# the eye finds a row by, so the words at the end of the row give way first. A longer name is cut in the
# middle, where two of one customer's domains are most alike.
_LEAST_NAME = 24
_ROOM = 6  # the width of the number column and the gap after it


def _widths(heads, cells, least):
    """How wide each column gets: what it needs, and when that is more than there is room for, the first column
    gives way first, down to 'least', and then the ones at the end, down to their heading."""
    fields = len(heads)
    widths = [max([len(heads[i])] + [len(row[i]) for row in cells]) for i in range(fields)]
    floors = [least] + [len(head) for head in heads[1:]]
    room, total = _WIDTH - _ROOM - 2 * (fields - 1), sum(widths)
    for i in [0] + list(range(fields - 1, 0, -1)):
        if total <= room:
            break
        cut = min(total - room, max(widths[i] - floors[i], 0))
        widths[i] -= cut
        total -= cut
    return widths


def _board_word(row, part):
    """The word for one part in the board's row. The Mail column is about the accounts, and on a customer's own
    screen the messages have a row of their own under them; the board has no room for that row, so a customer
    whose accounts are across and whose mail is not says 'part' here rather than 'done'."""
    word = row.word(part)
    if part == "mail" and word == DONE and messages_behind(row.name):
        return PART
    return word


def _table_of(rows, colour):
    """The table itself: a column for each part that has something to say, and the next step at the end."""
    steps = [next_step(row)[1] for row in rows]
    parts = [part for part in PARTS if any(row.word(part) != NONE for row in rows)]
    heads = ["Customer"] + [PART_HEADS[part] for part in parts] + ["Next step"]
    cells = [[row.name] + [_board_word(row, part) for part in parts] + [step]
             for row, step in zip(rows, steps)]
    widths = _widths(heads, cells, _LEAST_NAME)
    out = ["  " + _paint("#", "bold", colour) + "   "
           + "  ".join(_paint(head, "bold", colour) + " " * (width - len(head))
                       for head, width in zip(heads, widths)).rstrip(),
           " " + _rule(_ROOM - 1 + sum(widths) + 2 * (len(widths) - 1))]
    for row, line in zip(rows, cells):
        # A customer with nothing left steps back, so the rest stands out. One waiting on a pair of hands
        # hasn't: it is still work, even if this tool isn't the one that does it.
        finished = _nothing_left(row)
        shown = []
        for i, text in enumerate(line):
            fitted = _fit(text, widths[i], middle=(i == 0))
            style = WORD_STYLES.get(text) if 0 < i < len(heads) - 1 else ("dim" if finished else None)
            shown.append(_paint(fitted, style, colour)
                         + ("" if i == len(heads) - 1 else " " * (widths[i] - len(fitted))))
        number = _paint("%3s" % row.number, "dim" if finished else "bold", colour)
        out.append(number + "   " + "  ".join(shown).rstrip())
    return out


def _nothing_left(row):
    """Whether this customer is really finished: nothing to move, nothing a person has to do, nothing the world
    is still sent here for, and no piSignage server waiting to be brought across."""
    return (not next_step(row)[0] and not row.by_hand() and not still_here(row.name)
            and row.word("site") not in (SIGNAGE, FILES) and not messages_behind(row.name))


def _two_servers(old_server, new_server, addresses, colour):
    """The one line that names both servers, and the new one's addresses under it."""
    old = "%s  %s" % (_paint("Old server", "bold", colour), _fit(old_server or socket.gethostname(), 20, True))
    new = "%s  %s" % (_paint("New server", "bold", colour), _fit(new_server or "(not known)", 26, True))
    out = [_fit_line("%s      %s" % (old, new))]
    if addresses:
        out.append("%s  %s" % (" " * 10, _fit(", ".join(addresses), _WIDTH - 12)))
    return out


def dashboard(inventory, status, remembered=None, colour=False, old_server=None, new_server=None,
              addresses=None, databases=None, rows=None):
    """The screen this tool is worked from: one row for each customer, one number for each row, and the next
    step that number does. Every column says one of four words and nothing else: 'to do', 'part', 'done' or
    '-'. A column with nothing to say for any customer is left out. Everything this tool never moves is counted
    in one sentence under the table, and what one customer holds is what `i N` prints (inside()). 'rows' are
    the customers when they were worked out already, 'databases' the databases that belong to no site."""
    if not inventory:
        return "Nothing was found on this server to move."
    rows = customers(inventory, status, remembered) if rows is None else rows
    out = _two_servers(old_server, new_server, addresses, colour) + [""]
    if rows:
        out.extend(_table_of(rows, colour))
    else:
        out.append("  Nothing here is a customer to move.")
    also = left_over(inventory, status, remembered, databases)
    if also:
        out.append("")
        out.extend(_paint("  " + piece, "dim", colour)
                   for piece in textwrap.wrap(ALSO % ", ".join(also), _WIDTH - 2))
    done = len([row for row in rows if _nothing_left(row)])
    out.extend(["", "%s, %s, %s." % (_paint(_plural(len(rows), "customer", "customers"), "bold", colour),
                                     _paint("%d done" % done, "green", colour),
                                     _paint("%d to go" % (len(rows) - done), "cyan", colour))])
    return "\n".join(_fit_line(line) for line in out)


def _domain_numbers(rows):
    """{id(item): the number to type for it}, which is the number of the customer it belongs to."""
    return dict((id(item), row.number) for row in rows for item in row.to_move())


# ---- Inside one customer -----------------------------------------------------

_LABEL = 11  # the width of the part's name at the start of its sentence


def _said_about(item, status):
    said = status.get(item.get("id")) if isinstance(status, dict) else None
    return said.get("detail") if isinstance(said, dict) else None


def _mail_sentence(row, status, short):
    if not row.items["mail"]:
        return "No mail for this domain."
    accounts = forwards = 0
    for item in row.items["mail"]:
        here = _mail_here(item)
        accounts, forwards = accounts + here[0], forwards + here[1]
    what = _plural(accounts, "account", "accounts")
    if forwards:
        what += " and %s" % _plural(forwards, "forward", "forwards")
    with_what = ", with its password, filters and spam settings" if (accounts, forwards) == (1, 0) \
        else ", with their passwords, filters and spam settings"
    word = row.word("mail")
    if word == DONE:
        return "%s%s. On %s already." % (what, with_what, short)
    if word == PART:
        there = _mail_there(_said_about(row.items["mail"][0], status))
        has = ("%s and %s" % (_plural(there[0], "account", "accounts"),
                              _plural(there[1], "forward", "forwards")) if there else "part of it")
        return ("%s. On %s with %s so far; moving the mail again adds what is missing and changes nothing "
                "that is there." % (what, short, has))
    return "%s%s. Not on %s yet." % (what, with_what, short)


def _site_sentence(row, status, short):
    sites = row.items["site"]
    if not sites:
        return "No website for this domain."
    said = []
    for item, word in zip(sites, row.words["site"]):
        name = ("%s: " % item.get("name")) if len(sites) > 1 else ""
        if item.get("signage"):
            said.append("%sA piSignage server on port %d, not a website with files: %s"
                        % (name, item["signage"],
                           "on %s already; g brings over what changed here since." % short if word == DONE
                           else "not on %s yet. Type g to move its media, its data and its database." % short))
            continue
        if word is None or word == BY_HAND:
            said.append(name + (item.get("problem") or "This site is not moved."))
            continue
        size = _size(item.get("size"))
        if item.get("redirect"):
            what = "A %s, and nothing to copy" % _goes_to(item)
        else:
            what = ("WordPress, %s plus its files" % size if size else
                    "WordPress, its files and its database" if item.get("database") else
                    "WordPress, its files")
        if word == DONE:
            said.append("%s%s. On %s already." % (name, what, short))
        elif word == PART:
            said.append("%s%s. Made on %s; its files and its database are still to come."
                        % (name, what, short))
        else:
            said.append("%s%s. Not on %s yet." % (name, what, short))
    return " ".join(said)


def _cron_sentence(row, status, short):
    jobs = row.items["cron"]
    if not jobs:
        return "No jobs for this site."
    said = []
    for item, word in zip(jobs, row.words["cron"]):
        when = schedule_in_words(_cron_parts_of(item)[0])
        command = _fit_left(_cron_parts_of(item)[1], 40)
        if word is None or word == BY_HAND:
            said.append("%s: %s. %s" % (when, command, item.get("problem") or "This job is not moved."))
        elif word == DONE:
            said.append("%s: %s. On %s already." % (when, command, short))
        else:
            said.append("%s: %s. Not on %s yet." % (when, command, short))
    return " ".join(said)


NOTHING_TO_MOVE = ("Nothing left of this customer to move. What it may still need is in the list that t "
                   "prints: its messages, its redirects, the mail this server still delivers itself, and "
                   "where its names point.")


def inside(row, status, remembered=None, colour=False, new_server=None, mail=None):
    """What `i N` prints: one sentence for each part of one customer, in plain words, and what its number does
    next. No table and no numbers of its own: the only number on the screen is the customer's."""
    short = _fit(_short_name(new_server), 16)
    out = [_paint(_fit(row.name, _WIDTH, True), "bold", colour), ""]
    said = [("Mail", _mail_sentence(row, status, short)),
            ("Website", _site_sentence(row, status, short)),
            ("Cron", _cron_sentence(row, status, short))]
    if mail is not None:
        said.insert(1, ("Messages", _messages_sentence(row, mail, short)))
    for label, text in said:
        head = "  " + _paint(label, "bold", colour) + " " * (_LABEL - len(label))
        lines = textwrap.wrap(text, _WIDTH - 2 - _LABEL) or [""]
        out.append(head + lines[0])
        out.extend(" " * (2 + _LABEL) + piece for piece in lines[1:])
    items, step = next_step(row)
    out.append("")
    if items:
        out.append("Next step: %s. Type %d to do that." % (step, row.number))
    elif step == MOVE_SIGNAGE:
        out.append("Next step: %s. Open %d and type g." % (step, row.number))
    else:
        out.extend(textwrap.wrap(NOTHING_TO_MOVE, _WIDTH))
    return "\n".join(_fit_line(line) for line in out)


CUSTOMER_PROMPT = "What shall I do for %s?"
CUSTOMER_ACTIONS = ((1, "mail", "move the mail"), (2, "site", "copy the website and its database"),
                    (3, "cron", "install the cron jobs"))


def _state_of(item, status):
    """What the new server said about one item: new, exists, refused, or None when it said nothing."""
    said = status.get(item.get("id")) if isinstance(status, dict) else None
    return said.get("state") if isinstance(said, dict) else None


def _goes_to(item):
    """Where a redirect sends its visitors, in the few words a cell has room for."""
    redirect = item.get("redirect") or {}
    where = redirect.get("to")
    return "redirect to %s" % _short_url(where) if where else \
        "redirect, %s" % _plural(len(redirect.get("rules") or []), "rule", "rules")


def _short_url(url):
    """A URL without the parts a reader fills in: what is left is the name it goes to."""
    return re.sub(r"^https?://", "", str(url or "")).rstrip("/") or str(url or "")


def _here_words(row, part):
    """The short cell: what this server has of that part."""
    if not row.items[part]:
        return "-"
    if part == "mail":
        accounts = forwards = 0
        for item in row.items[part]:
            one = _mail_here(item)
            accounts, forwards = accounts + one[0], forwards + one[1]
        what = _plural(accounts, "account", "accounts")
        return what + (", %s" % _plural(forwards, "forward", "forwards") if forwards else "")
    if part == "site":
        sizes = [item.get("size") for item in row.items[part] if item.get("size")]
        goes = [_goes_to(item) for item in row.items[part] if item.get("redirect")]
        if goes and len(goes) == len(row.items[part]):
            return goes[0] if len(goes) == 1 else "%d redirects" % len(goes)
        signage = [item.get("signage") for item in row.items[part] if item.get("signage")]
        if len(signage) == len(row.items[part]) and signage:
            # The row is called piSignage already, so the cell says the one thing it doesn't: which port.
            return "port %d" % signage[0] if len(signage) == 1 else \
                "%d servers, ports %s" % (len(signage), ", ".join(str(one) for one in sorted(signage)))
        if not any(item.get("wordpress", True) for item in row.items[part]):
            # Which web server has it, when it isn't the usual one: a site that has to be looked at by hand
            # starts with its configuration, and that is somewhere else for each of them.
            served = [item.get("server") for item in row.items[part]
                      if item.get("server") and item.get("server") != APACHE]
            # These are files and a database, not "no WordPress": what f copies is what the cell names.
            what = "files" if all(item.get("other") for item in row.items[part]) else "not WordPress"
            return "%s (%s)" % (what, served[0]) if served else what
        return "WordPress, %s" % _size(sizes[0]) if sizes else "WordPress"
    return _plural(len(row.items[part]), "job", "jobs")


def _there_words(row, part, status, short):
    """The short cell: what the new server says it has of that part."""
    if not row.items[part]:
        return "-"
    states = [_state_of(item, status) for item in row.items[part]]
    if all(state == "exists" for state in states):
        return "there already"
    if any(state == "refused" for state in states):
        return "refused"
    if any(state == "exists" for state in states):
        return "part of it"
    return "not there yet"


def _part_cells(row, part, status, short):
    """The three short cells of one part: what is here, what the new server has, and where that leaves it."""
    here = _here_words(row, part)
    there = _there_words(row, part, status, short)
    return [_part_head(row, part), here, there, row.word(part)]


def _part_head(row, part):
    """What the row is called. The website row of a customer whose only name is in front of a piSignage server
    is not about a website, and saying it is was the whole reason that row read wrong."""
    if part == "site" and _only_signage(row):
        return "piSignage"
    return PART_HEADS[part]


KEPT_HERE_NOTE = ("The world sends the mail of %s to the new server now, but Postfix on this server still has "
                  "the domain in its own tables: a message sent from a mailbox here lands in the old mailbox "
                  "here instead of going along, and nobody sees it go missing. Number 7 stops that.")


def kept_here(dns, run=None):
    """The mail domains the world already sends to the new server while Postfix on this server would still
    deliver them itself. Those are the ones where mail quietly stays behind."""
    return [name for name in (dns or {}).get("mail_there") or [] if handed_over(name, run=run) is False]


def _site_reason(row, item):
    """Why a part of this customer is not moved, or, for a piSignage server that is across, what is true of it
    now: the long "this is not a website, type g" belongs on the screen of one that is still here."""
    if item.get("signage"):
        return SIGNAGE_THERE % item["name"] if row.word("site") == DONE else item.get("problem")
    if item.get("other") and row.word("site") == DONE:
        return OTHER_THERE % item["name"]
    return item.get("problem")


def customer_screen(row, status, remembered=None, colour=False, new_server=None, dns=None, mail=None,
                    kept=()):
    """What a number opens: a small table of where each part of one customer stands, then what can be done for
    it. Nothing on it happens by itself; the numbers are things to do, not things to pick from a list. 'dns' is
    where the names of this customer point now (dns_seen), and 'mail' is how much the mailboxes hold on each
    server (mail_sizes); each is a row of the table like the parts. 'kept' are the domains whose mail is
    answered by the new server while this one still delivers it itself (kept_here), which is a warning."""
    short = _fit(_short_name(new_server), 18)
    out = [_paint(_fit(row.name, _WIDTH, True), "bold", colour), ""]
    heads = ["Part", "On this server", "On %s" % short, "Status"]
    cells = [_part_cells(row, part, status, short) for part in PARTS]
    if mail is not None:
        # Under the Mail row: the same mailboxes, their messages, added up and then one by one.
        cells[1:1] = [_messages_cells(*mail)] + _mailbox_rows(row, *mail)
    if dns is not None:
        cells.append(_dns_cells(dns))
    widths = _widths(heads, cells, 10)
    out.append("  " + "  ".join(_paint(head, "bold", colour) + " " * (width - len(head))
                                for head, width in zip(heads, widths)).rstrip())
    out.append("  " + _rule(sum(widths) + 2 * (len(widths) - 1)))
    for line in cells:
        shown = []
        for i, text in enumerate(line):
            fitted = _fit(text, widths[i])
            # A mailbox under the Messages row is marked, and dim with it, so the parts still stand out.
            style = {0: "dim" if text.startswith(UNDER) else "bold",
                     2: "red" if text == "refused" else None, 3: WORD_STYLES.get(text)}.get(i)
            shown.append(_paint(fitted, style, colour) + " " * (widths[i] - len(fitted)))
        out.append("  " + "  ".join(shown).rstrip())
    # Why a part is not moved, in the new server's own words or this server's: never only a word in a cell.
    reasons = [_site_reason(row, item) for item in row.items["site"] + row.items["cron"]]
    reasons = [reason for reason in reasons if reason]
    reasons += [item.get("leftover") for item in row.items["site"] if item.get("leftover")]
    reasons += [_said_about(item, status) for item in row.every()
                if _state_of(item, status) == "refused" and _said_about(item, status)]
    reasons += _messages_notes(*mail) if mail is not None else []
    reasons += _dns_notes(dns) if dns is not None else []
    reasons += [KEPT_HERE_NOTE % name for name in kept]
    for reason in reasons:
        out.append("")
        lines = textwrap.wrap(reason, _WIDTH - 4) or [""]
        out.append("  %s %s" % (_paint("!", "yellow", colour), lines[0]))
        out.extend("    " + piece for piece in lines[1:])
    out.append("")
    menu = customer_menu(row)
    at = _note_column(menu)
    for key, what, note in menu:
        idle = note in ("nothing to do", "no website", "no mail", "move the mail first", STUCK_NOTE)
        line = "  %s  %s" % (_paint(key, "dim" if idle else "bold", colour), _paint(what, "dim", colour) if idle
                             else what)
        if note:
            line += " " * max(2, at - 4 - len(key) - len(what)) + _paint(note, "dim", colour)
        out.append(line)
    return "\n".join(_fit_line(line) for line in out)


MENU_NOTE_AT = 49  # the column the notes beside the numbers stand in, so they stand under each other


def _note_column(menu):
    """Which column the notes of a menu stand in: their own, and further left when the longest of them would
    run off the edge there. A note the column is too close for starts two spaces behind its own words instead.
    A line that runs off the edge is cut, and the cut takes the gap in front of the note with it: the note
    ends up against the words it belongs beside, and the whole line out of line with the rest."""
    notes = [note for _, _, note in menu if note]
    if not notes:
        return MENU_NOTE_AT
    return min(MENU_NOTE_AT, _WIDTH - max(len(note) for note in notes))


DNS_WARNING = ("This moves the DNS of %s to the new server at TransIP and then finishes the site there: it "
               "waits until the name resolves at the new server and asks Let's Encrypt for its certificate. "
               "Visitors go to the new server as soon as the old record has expired, so do it when the site "
               "and its database are across. The TransIP login has to be saved on the new server already "
               "(domainctl dns credentials there); there is no terminal here to type it in.")


SIGNAGE_THERE = ("The piSignage server of %s is on the new server already. Nothing of it is copied with the "
                 "websites; g brings over what changed here since, and its DNS when you say yes.")
OTHER_FIRST = ("The files of %s are not on the new server yet. Moving its DNS now would send its visitors to "
               "a server that hasn't got the site: type f here first, and pick its database.")
SIGNAGE_FIRST = ("The piSignage server of %s is not on the new server yet. Moving its DNS now would send its "
                 "screens and its players at a server that hasn't got it: type g here first, and say yes to "
                 "the DNS at the end of that.")
SITE_NOT_THERE = ("%s is not on the new server: this tool doesn't move it, and the reason is on the screen "
                  "above. Moving its DNS would send its visitors to a server that hasn't got the site, so "
                  "leave it alone until you have copied it there by hand.")


def _point_dns(connection, row, doing=None, ask=None, say=print):
    """Has the new server move the DNS of this customer's site and finish it. The one thing this tool does
    that the whole internet sees, so it says what it will do and asks first."""
    doing = doing if doing is not None else [""]
    ask = ask or _question
    sites = [item for item in row.items["site"] if item.get("name")]
    if not sites:
        _say("%s has no website here, so there is no DNS of a site to move." % row.name, say)
        return []
    done = []
    words = dict((id(one), word) for one, word in zip(row.items["site"], row.words["site"]))
    for item in sites:
        name = item["name"]
        heading("Moving the DNS of %s" % name, say)
        if item.get("signage"):
            # Its own reason: the name is in front of an application, so what has to be there before the DNS
            # moves is the piSignage server, not a web root full of files.
            if words.get(id(item)) != DONE:
                fail(SIGNAGE_FIRST % name, say)
        elif item.get("other"):
            # Its own reason as well: the files and the database are what has to be there first, and f is
            # what puts them there. Once they are across, nothing is in the way of the DNS.
            if words.get(id(item)) != DONE:
                fail(OTHER_FIRST % name, say)
        elif item.get("problem"):
            # One customer's names are asked about one at a time, and a site this tool leaves alone is still
            # one of them: its DNS moving is the one way this tool could take a site off the internet.
            fail(SITE_NOT_THERE % name, say)
        warn(DNS_WARNING % name, say)
        say("")
        if not _is_yes(ask("Move the DNS of %s to the new server now?" % name)):
            note("The DNS of %s was left alone." % name, say)
            continue
        doing[0] = "moving the DNS of %s" % name
        answer = _step("Moving the DNS and waiting for the certificate (up to 15 minutes)",
                       lambda: connection.ask({"request": "point_dns", "domain": name}),
                       lambda said: "done" if said.get("ok") else "failed")
        if answer.get("ok"):
            done.append(name)
            success("The DNS of %s now points at the new server, and the site is finished there." % name, say)
        else:
            fail("The DNS of %s was not moved: %s" % (name, answer.get("error") or "the new server gave no "
                                                      "reason"), say)
        # What the new server said, either way: it is where the reason stands when something didn't work.
        _say_relayed(answer.get("text"), say)
    return done


# How a line of the new server's own output begins when it is a result: mailctl and domainctl write these,
# and the plain ones where the terminal can't print the others.
_MARKED = ("\u2713 ", "\u2717 ", "! ", "+ ", "x ")


def _relayed(text):
    """The few lines worth showing of what the new server printed, as (what is still not right, the lines).

    A check there lists the records that would put it right, which fills the screen with a catalogue nobody
    types in by hand from here; and five checks failing for one reason is five sentences saying it. So the
    records go, the checks come down to their names, and what is left is what was done and what to do now.
    """
    problems, lines = [], []
    for raw in (text or "").rstrip().splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if len(raw) - len(raw.lstrip()) >= 4:
            continue  # a record under a check, or the line that introduces them
        if raw.startswith("  ") and stripped[:2] in _MARKED:
            if stripped[:2] not in ("\u2713 ", "+ "):
                words = stripped[2:].split()
                problems.append(words[0] if words else "")
            continue
        lines.append(stripped)
    return [name for name in problems if name], lines


def _say_relayed(text, say):
    """What the new server said, in a few lines, wrapped so no line runs off the screen. Returns what is still
    not right there, so the line after it doesn't call that a success."""
    problems, lines = _relayed(text)
    if problems:
        note("Still not right: %s." % ", ".join(problems), say)
    for line in lines:
        if line[:2] in ("\u2713 ", "+ "):
            # Something that worked, listing what it did: one line of it is plenty, and the rest is a list of
            # names nobody reads from here.
            say("  " + _fit(line, _WIDTH - 2))
            continue
        # Commands and names are never broken in half: a command cut over two lines can't be copied.
        pieces = textwrap.wrap(line, _WIDTH - 2, break_long_words=False, break_on_hyphens=False) or [""]
        for number, piece in enumerate(pieces):
            say("  " + ("" if number == 0 else "  ") + piece)
    if any("mailctl " in line or "domainctl " in line for line in lines):
        note("Those commands run on the new server, not here.", say)
    return problems


REPAIR_REASONS = 3  # the first few are the ones worth reading; the rest are the same thing again


def _repair_reasons(text):
    """Why a repair couldn't put something right, out of what it printed: the lines it wrote on its own, with
    the sentence under each that says what to do about it. The list of checks it makes first is indented, and
    stays out: what is wrong is said by the check afterwards, in one line."""
    out, after = [], False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or raw.startswith(" "):
            after = False
            continue
        if line[:2] in ("! ", "\u2717 ", "x "):
            out.append(line[2:])
            after = True
        elif line[:2] in ("\u2713 ", "+ "):
            after = False  # something that did work: the sentence after it belongs to that, not to a reason
        elif after and len(out) <= REPAIR_REASONS:
            out[-1] += " " + line  # the hint under it, which is where the thing to do stands
        else:
            after = False
    return out[:REPAIR_REASONS]


def _repair(connection, row, doing=None, say=print):
    """Runs `mailctl repair DOMAIN --yes` on the new server for every mail domain of this customer: its DNS
    records at TransIP, the certificate, webmail and autodiscover, every step without a question. Typing its
    number is the question."""
    doing = doing if doing is not None else [""]
    domains = [item["name"] for item in row.items["mail"] if item.get("name")]
    if not domains:
        note("%s has no mail here, so there is nothing to put right." % row.name, say)
        return []
    done = []
    for name in domains:
        heading("Putting the mail of %s right on the new server" % name, say)
        doing[0] = "running mailctl repair %s on the new server" % name
        answer = _step("mailctl repair %s --yes" % name,
                       lambda: connection.ask({"request": "repair", "domain": name}),
                       lambda said: "done" if said.get("ok") else "failed")
        if not answer.get("ok"):
            fail(answer.get("error") or "mailctl repair %s didn't work, and the new server gave no reason." % name,
                 say)
            _say_relayed(answer.get("text"), say)
            continue
        done.append(name)
        if answer.get("right"):
            # The new server checked the domain again once the repair was over, and it needs nothing. The
            # repair's own account of what it found and put right runs to fifty lines and says nothing more.
            success("The mail of %s is set up on the new server; it needs nothing more." % name, say)
            continue
        if answer.get("right") is None:
            warn("mailctl repair %s ran on the new server, but it couldn't say how the domain stands now. "
                 "See there with: mailctl doctor %s" % (name, name), say)
            continue
        # Why a step didn't happen comes first: the check afterwards says what is wrong, and only the repair
        # itself knows what stopped it being put right.
        reasons = _repair_reasons(answer.get("repair"))
        for reason in reasons:
            fail(reason, say)
        # What the new server's own check says is left, which is not what the repair found before it worked.
        _say_relayed(answer.get("text"), say)
        warn("mailctl repair %s did what it could. What is still not right is above it." % name, say)
    return done


# ---- Handing a mail domain over to the new server ---------------------------

# Postfix here keeps delivering a domain it has in its own tables, whatever the DNS says, so a message sent
# from a mailbox on this server to a moved domain never leaves the building: it lands in the old mailbox and
# the customer, reading their mail on the new server, sees nothing. One line per moved domain puts a stop to
# it. Every other mail server in the world looks the domain up and finds the new server by itself.
TRANSPORT_FILE = "/etc/postfix/migrate-transport"
TRANSPORT_HEADER = ("# Written by migrate: the domains this server no longer keeps itself. Each is sent on to\n"
                    "# the new server instead. To have this server keep one again, take its line away, run\n"
                    "# 'postmap %s' and then 'postfix reload'.\n" % TRANSPORT_FILE)

HANDOVER_WARNING = ("Postfix on this server still has %(domain)s in its own tables, so mail sent from a "
                    "mailbox here to that domain is dropped in the old mailbox here instead of going to the "
                    "new server. This tells Postfix here to send everything for %(domain)s to %(new)s. "
                    "Nothing is deleted: the mail already in the mailboxes here stays where it is, and the "
                    "one line that does it is in %(file)s.")


def _mx_hosts(text):
    """The mail hosts in the answer to an MX lookup, as both dig and host print it."""
    out = []
    for line in (text or "").splitlines():
        words = line.split()
        if not words:
            continue
        name = words[-1].rstrip(".")
        if "." in name and re.match(r"^[A-Za-z0-9.-]+$", name):
            out.append(name)
    return out


def _addresses_of(name, look=None):
    look = look or socket.getaddrinfo
    try:
        found = look(name, None)
    except (socket.gaierror, OSError):
        return set()
    return set(str(entry[4][0]) for entry in found)


# Asked of the domain's own name servers, not of this server's resolver: an old server often keeps its
# customers' zones itself, or the old answer for as long as it was told to, and then sees the mail as still its own
# long after the DNS was moved.
PUBLIC_RESOLVER = "1.1.1.1"
_HOST_SAYS = {"MX": "mail is handled by", "NS": "name server", "A": "has address", "AAAA": "has IPv6 address"}


def _lookup(run, kind, name, server):
    """The answers one name server gives to one question, with dig, or with host when there is no dig. None when
    neither is installed; an empty list when the server gave no answer."""
    try:
        text = run("dig", "+short", "+time=5", "+tries=1", kind, name, "@" + server)
        return [line.split()[-1].rstrip(".") for line in text.splitlines()
                if line.strip() and not line.startswith(";")]
    except CommandFailed as problem:
        if not problem.missing:
            return []
    try:
        text = run("host", "-W", "5", "-t", kind, name, server)
    except CommandFailed as problem:
        return None if problem.missing else []
    return [line.split()[-1].rstrip(".") for line in text.splitlines() if _HOST_SAYS[kind] in line]


def mail_points_at(domain, addresses, ours=(), run=None, look=None, servers=None):
    """Which server the mail of this domain is delivered to now, as its own name servers say: 'there' for the
    new server, 'here' for this one, 'away' for anywhere else. Returns that and the mail host it names, and
    None when there is no way to look up an MX record here, which is not the same as 'away'.

    With 'servers' given, those name servers are asked instead of the domain's own: the quick look over every
    customer asks the public resolver only, which is one lookup instead of three.
    """
    run = run or run_command
    servers = _lookup(run, "NS", domain, PUBLIC_RESOLVER) if servers is None else list(servers)
    if servers is None:
        return None, ""
    wanted = set(str(one) for one in (addresses or []))
    mine = set(str(one) for one in ours)
    for server in servers + [PUBLIC_RESOLVER]:
        hosts = _mx_hosts("\n".join(_lookup(run, "MX", domain, server) or []))
        if not hosts:
            continue
        here = None
        for host in hosts:
            found = set(_lookup(run, "A", host, server) or []) | set(_lookup(run, "AAAA", host, server) or [])
            if not found:
                found = _addresses_of(host, look)
            if found & wanted:
                return "there", host
            if found & mine and here is None:
                here = ("here", host)
        return here or ("away", hosts[0])
    return None, ""


def mail_answered_there(domain, addresses, run=None, look=None):
    """Whether the mail of this domain is delivered to the new server now, as the domain's own name servers say.
    None when there is no way to look up an MX record here, which is not the same as no."""
    where = mail_points_at(domain, addresses, run=run, look=look)[0]
    return None if where is None else where == "there"


def hand_mail_over(domain, target, run=None, path=None):
    """Postfix here sends the domain on to the new server from now on. Returns the maps it reads for a
    transport, our own first."""
    run = run or run_command
    path = path or TRANSPORT_FILE
    lines = []
    if os.path.exists(path):
        with open(path) as handle:
            lines = [line for line in handle.read().splitlines()
                     if line.strip() and not line.startswith("#") and line.split()[0] != domain]
    lines.append("%s\tsmtp:[%s]" % (domain, target))
    with open(path, "w") as handle:
        handle.write(TRANSPORT_HEADER + "\n".join(lines) + "\n")
    os.chmod(path, 0o644)
    run("postmap", path)
    maps = _transport_maps(run, path)
    run("postfix", "reload")
    return maps


def _transport_maps(run, path):
    """Postfix reads our file, before any other map it reads already: what is in ours is only the domains this
    server has handed over, and for those ours is the answer that counts."""
    now = " ".join((run("postconf", "-h", "transport_maps") or "").split())
    ours = "hash:%s" % path
    maps = [one for one in re.split(r"[,\s]+", now) if one and one != ours]
    wanted = " ".join([ours] + maps)
    if wanted != now:
        run("postconf", "-e", "transport_maps = %s" % wanted)
    return wanted


def _answers_for(domain, maps, run=None):
    """Which of Postfix's transport maps answers for the domain, and with what. The first one that answers is
    the one Postfix goes by, so it is the one to look at."""
    run = run or run_command
    for one in (maps or "").split():
        try:
            found = (run("postmap", "-q", domain, one) or "").strip()
        except CommandFailed:
            continue
        if found:
            return one, found
    return None, None


def handed_over(domain, run=None):
    """Whether Postfix on this server sends this domain's mail on instead of putting it in a mailbox here.

    None when Postfix can't be asked, which is not the same as no: nothing is claimed about a server this tool
    can't read.
    """
    run = run or run_command
    try:
        maps = " ".join((run("postconf", "-h", "transport_maps") or "").split())
    except CommandFailed:
        return None
    if not maps:
        return False
    return bool(_answers_for(domain, maps, run=run)[1])


def _hand_over(connection, row, hello=None, doing=None, ask=None, say=print, run=None):
    """The number on a customer's screen: hands over every mail domain of that customer. Only for a customer
    whose mail is all on the new server; the domains it is still half of would bounce there."""
    domains = [item["name"] for item in row.items["mail"] if item.get("name")]
    if not domains:
        _say("%s has no mail domain on this server, so there is nothing to hand over." % row.name, say)
        return []
    if row.word("mail") != DONE:
        _say("The mail of %s isn't all on the new server yet, so the new server can't take it over. Move the "
             "mail first." % row.name, say)
        return []
    return _hand_over_domains(connection, domains, hello, doing=doing, ask=ask, say=say, run=run)


LATER_NOTE = ("Mail sent from a mailbox on this server to %s still lands in the old mailbox here, because "
              "this server has the domain in its own tables. Hand it over with the number for it on %s's "
              "screen once its DNS has been moved.")


def _offer_handover(connection, domains, hello=None, doing=None, ask=None, say=print, run=None):
    """Offered the moment a mail domain is across, which is when the question comes up. A domain whose mail is
    still delivered here, or one this server can't look up, is only named: nothing is asked that this tool
    can't see the answer to, and the number on the customer's screen does it later."""
    run = run or run_command
    ready = []
    for name in domains:
        answered = mail_answered_there(name, (hello or {}).get("addresses"), run=run)
        if answered is True:
            ready.append(name)
            continue
        if answered is None:
            _say("I can't look up the MX of %s here, so I can't tell whether the new server answers for its "
                 "mail yet." % name, say)
        _say(LATER_NOTE % (name, name), say)
    if not ready:
        return []
    return _hand_over_domains(connection, ready, hello, doing=doing, ask=ask, say=say, run=run)


def _hand_over_domains(connection, domains, hello=None, doing=None, ask=None, say=print, run=None):
    """Stops this server keeping these domains itself, each after the question for it. A domain whose mail is
    still delivered here is left alone: until the DNS is moved, this is where it is read."""
    doing = doing if doing is not None else [""]
    ask = ask or _question
    run = run or run_command
    target = _new_name(connection, hello)
    addresses = (hello or {}).get("addresses")
    done = []
    for name in domains:
        _say(HANDOVER_WARNING % {"domain": name, "new": target, "file": TRANSPORT_FILE}, say)
        answered = mail_answered_there(name, addresses, run=run)
        if answered is False:
            _say("Not yet: the mail of %s is still delivered to this server, so this is where it is read. "
                 "Move its DNS first: 8 on this customer's screen runs 'mailctl repair %s' on the new "
                 "server." % (name, name), say)
            continue
        if answered is None:
            _say("I can't look up the MX of %s here, so I can't tell whether the new server answers for it "
                 "yet. Only say yes if its DNS has been moved." % name, say)
        if not _is_yes(ask("Send the mail of %s to the new server from now on?" % name)):
            _say("%s is still kept on this server." % name, say)
            continue
        doing[0] = "handing the mail of %s over" % name
        try:
            maps = hand_mail_over(name, target, run=run)
        except (CommandFailed, OSError, IOError) as problem:
            _say("Postfix here wouldn't have it: %s" % problem, say)
            continue
        where, found = _answers_for(name, maps, run=run)
        if found and where != "hash:%s" % TRANSPORT_FILE:
            _say("Careful: Postfix reads %s for %s before our own file, and that one says %s. The mail goes "
                 "there, not to the new server." % (where, name, found), say)
            continue
        done.append(name)
        _say("Postfix here sends everything for %s to %s from now on. Send a message from a mailbox on this "
             "server to an address at %s to see it arrive there." % (name, target, name), say)
    return done


# ---- Where a customer's names point now --------------------------------------

# A customer's screen says where each part of it stands on the two servers, but that is only half the move:
# the world keeps coming here until the DNS says otherwise, and that half is invisible from both servers. So
# it is asked of the customer's own name servers, for the website's name and for the mail of each of its
# domains, and what they answer is a row of the table like any other.
WEBSITE, MAIL = "website", "mail"
# A name in front of a piSignage server is not a website, and the DNS row is about what the world comes to this
# server for: calling that "website" says the one thing the screen has just said it isn't.
SIGNAGE_WHAT = "piSignage"
DNS_WHATS = (WEBSITE, SIGNAGE_WHAT, MAIL)
DNS_SEEN_FOR = 120  # seconds an answer is remembered; the screen is drawn again after every number typed
_DNS_SEEN = {}


def _our_addresses():
    """The addresses this server is reached at from the outside, which is what its customers' records point at
    while they are still here. Nothing is sent: the socket only says which address it would leave from."""
    out = set()
    for family, host in ((socket.AF_INET, PUBLIC_RESOLVER), (socket.AF_INET6, "2606:4700:4700::1111")):
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:  # pragma: no cover - a server without IPv6 at all
            continue
        try:
            sock.connect((host, 53))
            out.add(str(sock.getsockname()[0]))
        except OSError:  # pragma: no cover - no route that way
            pass
        finally:
            sock.close()
    return out


def _address_answers(name, servers, run):
    """The addresses a name has, asked of the domain's own name servers and then of the public one. None when
    there is no way to ask from this server at all."""
    out = set()
    for server in list(servers) + [PUBLIC_RESOLVER]:
        for kind in ("A", "AAAA"):
            found = _lookup(run, kind, name, server)
            if found is None:
                return None
            out.update(found)
        if out:
            break
    return out


def site_points_at(name, servers, addresses, ours=(), run=None):
    """Which server visitors of this name are sent to now: 'there' for the new server, 'here' for this one,
    'away' for anywhere else. Returns that and what the DNS answered, and None when nothing can be looked up."""
    run = run or run_command
    found = _address_answers(name, servers, run)
    if found is None:
        return None, ""
    said = ", ".join(sorted(found))
    if found & set(str(one) for one in (addresses or [])):
        return "there", said
    if found & set(str(one) for one in ours):
        return "here", said
    return "away", said


QUICK_NOTES = {("site", "here"): "point the site's DNS", ("mail", "here"): "point the mail's DNS",
               ("site", "away"): "its DNS points away", ("mail", "away"): "its mail goes elsewhere",
               ("signage", "here"): "point the piSignage DNS",
               ("signage", "away"): "its DNS points away"}
BOTH_HERE = "point the DNS here"
HAND_OVER = "hand the mail over"


def quick_look(rows, addresses, ours=None, run=None, say=None):
    """Fills in what each customer still needs that is no item to move, for the board: where the world is sent
    for it, and whether this server still delivers mail it has handed over.

    One lookup per name, of the public resolver, where a customer's own screen asks that customer's own name
    servers for every name it has. This is a line in a table and it is asked for every customer at once, so it
    is the cheap version; what it says is checked properly the moment a customer is opened.
    """
    run = run or run_command
    ours = _our_addresses() if ours is None else set(str(one) for one in ours)
    wanted = set(str(one) for one in (addresses or []))
    for row in rows:
        _STILL_HERE.pop(row.name, None)
        left = []
        # A name in front of a piSignage server has its own line: "the site's DNS" is about a website, and the
        # whole point of the row is that this one isn't.
        part = "signage" if _only_signage(row) else "site"
        for name in [item.get("name") for item in row.items["site"] if item.get("name")][:1]:
            where = _quick_site(name, wanted, ours, run)
            if where in ("here", "away"):
                left.append((part, where))
        for name in [item.get("name") for item in row.items["mail"] if item.get("name")][:1]:
            where = mail_points_at(name, addresses, ours, run=run, servers=[])[0]
            if where in ("here", "away"):
                left.append(("mail", where))
            elif where == "there" and handed_over(name, run=run) is False:
                left.append(("mail", "kept"))
        note = _quick_note(left)
        if note:
            _STILL_HERE[row.name] = note
    return _STILL_HERE


def _quick_site(name, wanted, ours, run):
    """Where a website's name is sent now, from one lookup: its A records at the public resolver."""
    found = _lookup(run, "A", name, PUBLIC_RESOLVER)
    if not found:
        return None  # no answer, or none this server can get: the customer's own screen asks properly
    if set(found) & wanted:
        return "there"
    return "here" if set(found) & ours else "away"


def _quick_note(left):
    """The one line for the board out of what was found, the thing to do first in front."""
    if not left:
        return None
    if ("mail", "kept") in left and len(left) == 1:
        return HAND_OVER
    if ("mail", "here") in left and (("site", "here") in left or ("signage", "here") in left):
        return BOTH_HERE
    return QUICK_NOTES.get(left[0], BOTH_HERE)


def dns_now(row, addresses, ours=None, run=None, look=None):
    """Where the names of one customer send the world now: its website's name and the mail of each of its
    domains, each asked of the domain's own name servers. Says which of 'website' and 'mail' the new server is
    found for, which this server is still found for, which goes somewhere else entirely, and which could not be
    looked up at all."""
    run = run or run_command
    ours = _our_addresses() if ours is None else set(str(one) for one in ours)
    found = {"here": [], "there": [], "away": [], "unknown": [], "mail_there": []}
    servers = _lookup(run, "NS", row.name, PUBLIC_RESOLVER) or []
    for item in row.items["site"]:
        if item.get("name"):
            _put_dns(found, SIGNAGE_WHAT if item.get("signage") else WEBSITE, item["name"],
                     *site_points_at(item["name"], servers, addresses, ours, run=run))
    for item in row.items["mail"]:
        if not item.get("name"):
            continue
        where, said = mail_points_at(item["name"], addresses, ours, run=run, look=look)
        _put_dns(found, MAIL, item["name"], where, said)
        if where == "there":
            # By name, which the row above has no room for: what this server does with that domain's mail
            # itself is only worth looking at for a domain the world already sends to the new server.
            found["mail_there"].append(item["name"])
    return found


def _put_dns(found, what, name, where, said):
    """One answer in its place. A name that points at neither server, or that can't be looked up, keeps its own
    name with it: the row above has no room to say which of the customer's names it was."""
    if where is None:
        found["unknown"].append((what, name))
    elif where == "away":
        found["away"].append((what, name, said))
    elif what not in found[where]:
        found[where].append(what)


def _dns_cells(found):
    """The four cells of the DNS row: what the world is still sent here for, what it is sent to the new server
    for, and the one word for where that leaves the move."""
    here = [what for what in DNS_WHATS if what in found["here"]]
    there = [what for what in DNS_WHATS if what in found["there"]]
    rest = here + found["away"] + found["unknown"]
    if not (there or rest):
        word = NONE
    elif not (there or here or found["away"]):
        word = CANT_TELL
    elif there and not rest:
        word = DONE
    elif there:
        word = PART
    else:
        word = TO_DO
    return ["DNS", ", ".join(here) or "-", ", ".join(there) or "-", word]


def _dns_notes(found):
    """What a cell of the DNS row has no room for: a name that points at neither server, and one that this
    server can't look up at all."""
    out = []
    for what, name, said in found["away"]:
        where = {WEBSITE: "The website of %s is served from",
                 SIGNAGE_WHAT: "The piSignage server of %s is reached at",
                 MAIL: "The mail of %s is delivered to"}[what]
        out.append((where % name) + " %s, which is neither this server nor the new one."
                   % (said or "nothing at all"))
    for what, name in found["unknown"]:
        out.append("Where the %s of %s is sent can't be looked up from this server, so the DNS row leaves it "
                   "out." % (what, name))
    return out


def dns_remembered(name, now=None):
    """Whether the answer about this customer is still good, so the screen under it can be drawn without
    waiting for the internet."""
    when, found = _DNS_SEEN.get(name, (0, None))
    return found is not None and (time.time() if now is None else now) - when <= DNS_SEEN_FOR


def dns_seen(row, hello, run=None, look=None, now=None):
    """Where this customer's names point, looked up once and then remembered for a couple of minutes: the
    screen is drawn again after every number that is typed, and a handful of lookups each time makes it crawl.
    What this tool moves itself is forgotten at once, so the screen under it is the new one."""
    moment = time.time() if now is None else now
    when, found = _DNS_SEEN.get(row.name, (0, None))
    if found is None or moment - when > DNS_SEEN_FOR:
        found = dns_now(row, (hello or {}).get("addresses"), run=run, look=look)
        _DNS_SEEN[row.name] = (moment, found)
    return found


def forget_dns(name):
    """After something that changes where a name points, so the next screen asks again."""
    _DNS_SEEN.pop(name, None)


# ---- How much mail the mailboxes hold, here and on the new server ------------

MAIL_FOLDERS = ("cur", "new")  # where a maildir keeps its messages; tmp holds half-delivered ones
MESSAGE_ROWS = 12  # mailboxes with a row of their own; the rest are one row together, so the menu stays in view
SIZES_SEEN_FOR = 6 * 3600  # seconds a measurement is remembered. Walking a mailbox of tens of gigabytes
# means a stat of every message in it, and over every customer that is minutes of waiting with nothing on the
# screen; a number typed after reading the screen must not start that again, and neither must starting the
# tool. Anything that copies or counts messages forgets that customer's measurement itself, so what is drawn
# after it is the new one, and "r" forgets the lot.
SIZES_FILE = "/var/tmp/migrate-mailbox-sizes.json"  # where the measurements wait for the next run
_SIZES_SEEN = {}


def _read_sizes(path=None):
    """The measurements an earlier run wrote down, if they are still worth anything. Nothing here is worth
    stopping for: a file that can't be read or understood only means measuring again."""
    try:
        with open(path or SIZES_FILE) as handle:
            written = json.load(handle)
    except (OSError, ValueError):
        return {}
    moment, found = time.time(), {}
    for name, kept in (written.items() if isinstance(written, dict) else []):
        try:
            when, here, there = kept["when"], kept["here"], kept["there"]
        except (TypeError, KeyError):
            continue
        if isinstance(when, (int, float)) and isinstance(here, dict) and moment - when <= SIZES_SEEN_FOR:
            found[name] = (when, (here, there if isinstance(there, dict) else None))
    return found


def _write_sizes(path=None):
    """Writes the measurements down for the next run. Only root reads them: they say what every customer's
    mail weighs, which is nobody else's business."""
    written = dict((name, {"when": when, "here": found[0], "there": found[1]})
                   for name, (when, found) in _SIZES_SEEN.items() if found)
    where = path or SIZES_FILE
    try:
        with open(os.open(where, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(written, handle)
    except (OSError, ValueError, TypeError):
        pass  # the measuring is worth keeping, the writing down is not worth a word on the screen


def mailbox_size(address, mail_root=None):
    """{bytes, messages} of one account's mailbox on this server, or None when it has no mailbox here.

    The messages themselves are counted, wherever the maildir sits inside the account's own folder, and not
    dovecot's indexes beside them: the new server builds its own, so a mailbox that is wholly copied across
    would still look a different size from here if they were counted in.
    """
    mail_root = VMAIL if mail_root is None else mail_root
    local_part, _, domain_part = address.rpartition("@")
    folder = os.path.join(mail_root, domain_part, local_part)
    if not os.path.isdir(folder):
        return None
    total = messages = 0
    newest = None
    for place, _folders, files in os.walk(folder):
        if os.path.basename(place) not in MAIL_FOLDERS:
            continue
        for name in files:
            try:
                about = os.lstat(os.path.join(place, name))
            except OSError:
                continue  # a message delivered or moved while this was walking it
            total += about.st_size
            messages += 1
            if newest is None or about.st_mtime > newest:
                newest = about.st_mtime
    # When the last message landed here, so a copy that said "everything is across" can be told from one that
    # said it before today's mail came in.
    return {"bytes": total, "messages": messages, "newest": newest}


def mailboxes_here(addresses, mail_root=None):
    """{address: {bytes, messages}} for those of these accounts that have a mailbox on this server."""
    found = {}
    for address in addresses:
        one = mailbox_size(address, mail_root)
        if one is not None:
            found[address] = one
    return found


def mailboxes_there(connection, addresses):
    """{address: {bytes, messages}} for the mailboxes the new server says it has. None when it couldn't say:
    a comparison nobody can make is better left off the screen than guessed at."""
    try:
        answer = connection.ask({"request": "mail_sizes", "addresses": list(addresses)})
    except Stopped:
        return None
    found = answer.get("mailboxes") if answer.get("ok") else None
    if not isinstance(found, dict):
        return None
    return dict((address, one) for address, one in found.items()
                if isinstance(one, dict) and one.get("here"))


def mail_sizes(row, connection, now=None, measure=None, ask=None):
    """(here, there) for one customer's mailboxes, measured once and then remembered for a minute: the screen
    is drawn again after every number that is typed, and walking a mailbox of tens of gigabytes each time
    makes it crawl. A copy this tool starts itself is forgotten at once, so the screen under it is the new
    one."""
    moment = time.time() if now is None else now
    when, found = _SIZES_SEEN.get(row.name, (0, None))
    if found is None or moment - when > SIZES_SEEN_FOR:
        addresses = row.accounts()
        here = (measure or mailboxes_here)(addresses)
        there = (ask or mailboxes_there)(connection, addresses) if addresses else {}
        found = (here, there)
        _SIZES_SEEN[row.name] = (moment, found)
        _write_sizes()
    return found


def sizes_remembered(name):
    """Whether the measurement of this customer's mailboxes is still good, so the screen under it can be drawn
    without walking them again."""
    when, found = _SIZES_SEEN.get(name, (0, None))
    return found is not None and time.time() - when <= SIZES_SEEN_FOR


def messages_behind(name):
    """How many messages of this customer are still only here, out of the measurement that was taken. None
    when nothing has been measured for it: the board then says nothing about its messages rather than
    guessing. The messages are not items to move, so without this a customer whose accounts are all on the new
    server reads as done while gigabytes of its mail are still here."""
    when, found = _SIZES_SEEN.get(name, (0, None))
    if found is None or time.time() - when > SIZES_SEEN_FOR:
        return None
    here, there = found
    if there is None:
        return None
    return _messages_left(here, there)


def forget_sizes(name):
    """After something that copies messages, so the next screen measures again."""
    _SIZES_SEEN.pop(name, None)
    _write_sizes()


def forget_all_sizes():
    """Everything measured, forgotten: what "r" is for when the numbers on the board look old."""
    _SIZES_SEEN.clear()
    _write_sizes()


def _across(mine, theirs):
    """Whether what a mailbox holds here is on the new server as well.

    imapsync's own word comes first where there is one: it is the program that does the copying, it opened both
    mailboxes, and it says how many messages it found here that the new server hasn't got. The messages in the
    maildirs are counted beside that, and never weighed. What a mailbox weighs on one server says nothing
    about the other: a server may keep its mail compressed where this one keeps it plain, and then the same
    mailbox weighs half as much there for ever -- on one account here the new server serves 10.6 GB of
    messages out of 5.4 GB of files. A mailbox judged on its bytes would read "half there" however often it
    was copied. A mailbox that is empty here is across as soon as the account is there: there is nothing to
    copy into it.
    """
    if not theirs:
        return False
    if _synced(mine, theirs):
        return True
    return (theirs.get("messages") or 0) >= (mine.get("messages") or 0)


def _synced(mine, theirs, now=None):
    """Whether imapsync's last run copied this mailbox whole and nothing has landed here since. Both servers
    say how long ago, never when: their clocks are their own, and a difference between them would make mail
    that came in after the copy look as if it came in before it."""
    sync = (theirs or {}).get("sync") or {}
    if not sync.get("good") or sync.get("left") or sync.get("ago") is None:
        return False
    newest = (mine or {}).get("newest")
    if not newest:
        return True  # nothing here to have arrived since
    return (time.time() if now is None else now) - newest > sync["ago"]


def _messages_left(here, there):
    """How many messages of these mailboxes are not on the new server yet. imapsync's count where it has one:
    it is the one that counts the messages a mail program sees."""
    return sum(_left_of(mine, theirs) for _address, mine, theirs in _behind(here, there))


def _left_of(mine, theirs):
    sync = (theirs or {}).get("sync") or {}
    if sync.get("left") is not None:
        return sync["left"]
    return max((mine.get("messages") or 0) - ((theirs or {}).get("messages") or 0), 0)


def _copied_whole(here, there):
    """The mailboxes imapsync says it has copied whole, out of ones whose files here outnumber the files
    there. The two never have to agree: dovecot doesn't serve every file in a maildir as a message, and an old
    folder it no longer lists is still a pile of files on the disk."""
    return [address for address, mine in sorted(here.items())
            if _synced(mine, (there or {}).get(address))
            and (mine.get("messages") or 0) > ((there or {}).get(address) or {}).get("messages", 0)]


def _behind(here, there):
    """The mailboxes whose mail is not on the new server yet, each as (address, what is here, what is there)."""
    return [(address, mine, there.get(address)) for address, mine in sorted(here.items())
            if not _across(mine, there.get(address))]


def _held(found):
    """How much mail a set of mailboxes holds, in words."""
    if found is None:
        return CANT_TELL
    if not found:
        return "-"
    return _size(sum((one.get("bytes") or 0) for one in found.values()))


def _messages_word(here, there):
    """Where the copying of the messages stands, in one of the words the screen says."""
    if not here:
        return NONE
    if there is None:
        return CANT_TELL
    if not _behind(here, there):
        return DONE
    return PART if any((one.get("bytes") or 0) for one in there.values()) else TO_DO


def _messages_cells(here, there):
    """The four cells of the Messages row: how much mail the mailboxes hold here, how much of it is on the new
    server, and the one word for where that leaves the copying. The accounts and their settings are the Mail
    row above it; this row is the messages themselves, which imapsync copies. Under it stands a row for each
    mailbox of its own."""
    return ["Messages", _held(here), _held(there), _messages_word(here, there)]


def _in_row(address, domain):
    """How a mailbox is named in the table: without the customer's own domain, which is the screen's title."""
    suffix = "@" + domain
    return address[:-len(suffix)] if address.endswith(suffix) else address


UNDER = ("\u00b7", "-")  # what marks a mailbox as standing under the Messages row, and the plain mark for a
# terminal that can't print the first. A cell is fitted to its column with the spaces taken out of it, so an
# indent of spaces alone would not survive to the screen.


def _under(name):
    return "%s %s" % (UNDER[0] if LOOK["fancy"] else UNDER[1], name)


def _mailbox_cells(name, mine, theirs, told=True):
    """The row of one mailbox, under the Messages row it is part of."""
    here = {name: mine}
    there = None if not told else ({name: theirs} if theirs else {})
    return [_under(name), _held(here), _held(there), _messages_word(here, there)]


def _mailbox_rows(row, here, there):
    """A row for each of this customer's mailboxes, so the sizes can be read account by account and not only
    added up. The ones that are not across stand first, so what is left to copy is on the screen even for a
    customer with more mailboxes than the screen has room for; the rest are one row together."""
    told = there is not None
    order = sorted(here, key=lambda one: (_across(here[one], (there or {}).get(one)), one))
    rows = [_mailbox_cells(_in_row(address, row.name), here[address], (there or {}).get(address), told)
            for address in order[:MESSAGE_ROWS]]
    rest = order[MESSAGE_ROWS:]
    if rest:
        mine = dict((address, here[address]) for address in rest)
        theirs = None if not told else dict((address, there[address]) for address in rest if address in there)
        rows.append([_under("and %d more" % len(rest)), _held(mine), _held(theirs),
                     _messages_word(mine, theirs)])
    return rows


def _messages_notes(here, there):
    """What the rows have no room for: that the new server couldn't say what its own mailboxes hold."""
    if here and there is None:
        return ["How much mail the mailboxes on the new server hold couldn't be asked, so the rows under "
                "Messages can't say whether the messages are across."]
    if here and there is not None:
        left = _messages_left(here, there)
        if left:
            return ["%s here against %s on the new server: %s not across yet. Number 9 brings over what is "
                    "missing without copying anything twice."
                    % (_plural(_counted(here), "message", "messages"), _counted(there),
                       _plural(left, "message is", "messages are"))]
        whole = _copied_whole(here, there)
        if whole:
            return [SIZES_DIFFER % _plural(len(whole), "mailbox", "mailboxes")]
    return []


SIZES_DIFFER = ("The maildirs here hold more files than the new server's do, and nothing is missing: imapsync "
                "opened both and found every message of %s on the new server. A maildir keeps files dovecot "
                "doesn't serve as messages, an old folder it no longer lists among them, so the two sides "
                "never add up to the same number. c counts them again, folder by folder.")


def _counted(found):
    """How many messages a set of mailboxes holds."""
    return sum((one.get("messages") or 0) for one in (found or {}).values())


def _messages_sentence(row, mail, short):
    """The same thing in a sentence, for the screen that has no table."""
    here, there = mail
    if not row.accounts():
        return "No accounts here, so there are no messages to copy."
    if not here:
        return ("The accounts here have no mailbox under %s on this server, so there is nothing to copy."
                % VMAIL)
    held = _held(here)
    if there is None:
        return ("%s here. How much of it %s has couldn't be asked." % (held, short))
    word = _messages_word(here, there)
    if word == DONE:
        return ("%s in %s here, and all of them on %s: the messages are across."
                % (held, _plural(_counted(here), "message", "messages"), short))
    if word == PART:
        return ("%s here, %s on %s so far, %s still to come. Copying them again brings over what is missing "
                "and copies no message twice."
                % (held, _held(there), short, _plural(_messages_left(here, there), "message", "messages")))
    return "%s here, and none of it on %s yet." % (held, short)


STUCK_NOTE = "can't be moved; see above"


def _nothing_note(row, part):
    """Why a part offers nothing. "Nothing to do" is only true when this server has nothing of it: a cron job or
    a site that is here but can't be moved reads as if the tool hadn't seen it at all, while the reason why it
    can't stands a few lines above the menu."""
    return STUCK_NOTE if row.items[part] else "nothing to do"


def why_nothing(row, key):
    """What to say when a number that does nothing is typed."""
    notes = dict((number, note) for number, _, note in customer_menu(row))
    if notes.get(key) == STUCK_NOTE:
        return "Nothing of that can be moved. Why not is in the note above the menu."
    site_key = str(dict((part, number) for number, part, _ in CUSTOMER_ACTIONS)["site"])
    if _only_signage(row) and key == site_key:
        return "That is a piSignage server, not a website with files: type g to move it."
    if other_sites(row) and key == site_key:
        return "That site is not WordPress, so this doesn't move it: type f, and pick its database."
    return "There is nothing to do for that."


def customer_menu(row):
    """The lines of the menu, as (key, what it does, the note beside it). A part that is already on the new
    server is offered again instead: a website whose files changed since it moved is copied over the old one,
    and a mail domain is sent again to add whatever is missing."""
    out = []
    for number, part, what in CUSTOMER_ACTIONS:
        if part == "site" and _only_signage(row):
            continue  # there is no website to copy; g a few lines down is what moves this name
        if part == "site" and other_sites(row) and len(other_sites(row)) == len(row.items["site"]):
            continue  # f copies these: number 2 is the WordPress way and would only say it can't
        if part == "site" and only_redirects(row):
            what = "set up the redirect on the new server"
        if row.to_move(part):
            out.append((str(number), what, "" if row.word(part) != PART else "it is half there"))
        elif part == "site" and row.items["site"] and row.word("site") == DONE:
            out.append((str(number), "copy the website again", "replaces its database"))
        elif part == "mail" and row.items["mail"] and row.word("mail") == DONE:
            out.append((str(number), "send the mail again", "adds only what is missing"))
        else:
            out.append((str(number), what, _nothing_note(row, part)))
    out.append(("4", "everything that is left", "" if row.to_move() else "nothing to do"))
    out.append(("5", "give an account its own password on the new server", ""))
    out.append(("6", "change the DNS at TransIP and finish the site",
                "" if row.items["site"] else "no website"))
    out.append(("7", "stop keeping this domain's mail here",
                "" if row.word("mail") == DONE else
                ("no mail" if not row.items["mail"] else "move the mail first")))
    out.append(("8", "put the mail right on the new server", "mailctl repair --yes"
                if row.items["mail"] else "no mail"))
    out.append(("9", "copy the mail messages across", "" if row.word("mail") == DONE else
                ("no mail" if not row.items["mail"] else "move the mail first")))
    out.append(("c", "count the messages on both servers", "" if row.word("mail") == DONE else
                ("no mail" if not row.items["mail"] else "move the mail first")))
    out.append(("k", COPY_WEBMAIL, "" if webmail_of(row) else "none in webmail"))
    if other_sites(row):
        out.append(("f", "copy this site's files and a database you pick", ""))
    if signage_of(row):
        out.append(("g", "move the piSignage server: its media, data and database", ""))
    out.append(("b", "back to the list", ""))
    out.append(("q", "leave", ""))
    return out


def customer_action(row, word):
    """What was typed on a customer's own screen: (what to do with, what it is called, whether it is a copy
    over one that is already there), or (None, None, False) when it means nothing."""
    for number, part, what in CUSTOMER_ACTIONS:
        if word != str(number):
            continue
        left = row.to_move(part)
        if left:
            return left, what, False
        if part == "site" and row.word("site") == DONE:
            return row.items["site"], "copy the website again", True
        if part == "mail" and row.word("mail") == DONE:
            return row.items["mail"], "send the mail again", False
        return [], what, False
    if word == "4":
        return row.to_move(), "everything that is left", False
    if word == "5":
        return [], "passwords", False
    if word == "6":
        return row.items["site"], "dns", False
    if word == "7":
        return row.items["mail"], "handover", False
    if word == "8":
        return row.items["mail"], "repair", False
    if word == "9":
        return row.items["mail"], "copy_mail", False
    if word == "c":
        return row.items["mail"], "compare_mail", False
    if word == "k":
        return row.items["mail"], "webmail", False
    if word == "f" and other_sites(row):
        return other_sites(row), "other", False
    if word == "g" and signage_of(row):
        return signage_of(row), "signage", False
    return None, None, False


def signage_of(row):
    """The sites of this customer that are a piSignage server and not a website."""
    return [item for item in row.items["site"] if item.get("signage")]


def _only_signage(row):
    """Whether every name this customer has here is in front of a piSignage server, so nothing about it is a
    website: not the row of the table, not the line of the menu, and not where its DNS sends the world."""
    return bool(signage_of(row)) and len(signage_of(row)) == len(row.items["site"])


# ---- The password of an account that already exists --------------------------

CHECKED_HERE = ("The account's own password is asked for here, checked against what this server has stored for "
                "it, and then stored on the new server the way that server stores its own. Nothing changes on "
                "this server, and no new password is made up: the customer keeps the one they have.")
WRONG_PASSWORD = ("That is not the password this server has for %s. Nothing was changed. Try again, or press "
                  "Enter to leave it.")


def _sql_text(value):
    """One value for the mysql client, which has no way to take them separately."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _stored_hash(address, database, run=None):
    """The hash this server keeps for that account, label and all, or None when it has none."""
    run = run or run_command
    found = run("mysql", "--batch", "--skip-column-names", database,
                stdin="SELECT password FROM virtual_users WHERE email=%s;\n" % _sql_text(address))
    value = (found or "").strip()
    return value or None


def password_fits(password, stored, run=None):
    """Whether that really is the account's password, checked against the hash this server already has: the
    hash carries its own salt, so hashing the password with it again has to give the same hash back."""
    if not stored:
        return False
    bare = stored[stored.index("}") + 1:] if stored.startswith("{") and "}" in stored else stored
    if crypt is not None:
        try:
            return crypt.crypt(password, bare) == bare
        except (ValueError, OSError):
            return False
    run = run or run_command
    try:
        # doveadm reads the password to check on its standard input, so it is in no command line.
        run("doveadm", "pw", "-t", stored, stdin=password + "\n")
        return True
    except CommandFailed:
        return False


def ask_password(address, stored, say=print, ask=None):
    """Asks for one account's own password until it is the one this server has stored for it, and gives it
    back. None when Enter was pressed. It is checked here first, so a typing mistake is never sent anywhere."""
    ask = ask or _typed_password
    while True:
        password = ask("Password of %s (Enter to leave it): " % address)
        if not password:
            return None
        if password_fits(password, stored):
            return password
        _say(WRONG_PASSWORD % address, say)


def give_password(connection, address, stored, say=print, ask=None):
    """Asks for one account's own password, checks it here, and has the new server store it. True when the new
    server took it. Enter leaves the account alone."""
    password = ask_password(address, stored, say=say, ask=ask)
    return False if password is None else store_password(connection, address, password, say=say)


def store_password(connection, address, password, say=print, quiet=False):
    """Has the new server store a password that was checked here already. True when it took it. With quiet the
    line saying so is left out: the caller is about to say it in words of its own."""
    answer = connection.ask({"request": "password", "address": address, "password": password})
    if answer.get("ok"):
        if not quiet:
            success("%s opens with that password on the new server now." % address, say)
        return True
    warn("The new server would not take the password of %s: %s"
         % (address, answer.get("error") or "it gave no reason"), say)
    return False


# No l, I, O, 0 or 1: this gets read out over the telephone.
PASSWORD_LETTERS = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_GROUPS = 4  # of four letters, which is long enough for any mail server and still readable aloud
NEW_HASH = "SHA512-CRYPT"  # what a password this tool makes is hashed with here

NEW_PASSWORD_WARNING = ("I can give %s a brand new password and set it on both servers, so the same one opens "
                        "the mailbox on each of them. The password it has now stops working, here and on the "
                        "new server, so the customer needs the new one: it is printed here once and kept "
                        "nowhere.")


def new_password():
    """A password nobody has to remember but somebody has to read out: four groups of four."""
    return "-".join("".join(secrets.choice(PASSWORD_LETTERS) for _ in range(4))
                    for _ in range(PASSWORD_GROUPS))


def _hash_for(password, stored, run=None):
    """A hash of this password for this server's own table, in the shape the account's hash has now: with the
    scheme in front when the one there has one, since Dovecot goes by that, and bare when it hasn't, since then
    Dovecot goes by its own setting and a hash of another scheme would never open. None when the one there is
    bare and of a scheme this tool can't write, where guessing would lock the account out."""
    run = run or run_command
    if crypt is not None:
        made = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    else:
        made = (run("openssl", "passwd", "-6", "-stdin", stdin=password + "\n") or "").strip()
    if not made or not made.startswith("$6$"):
        raise CommandFailed("I couldn't hash a password on this server.")
    if str(stored or "").startswith("{"):
        return "{%s}%s" % (NEW_HASH, made)
    return made if str(stored or "").startswith("$6$") else None


def _store_hash_here(address, password_hash, database, run=None):
    """Writes one account's password hash into this server's own table."""
    run = run or run_command
    run("mysql", "--batch", database,
        stdin="UPDATE virtual_users SET password=%s WHERE email=%s;\n"
              % (_sql_text(password_hash), _sql_text(address)))


def new_password_on_both(connection, address, database, say=print, ask=None, run=None):
    """Gives one account a password this tool makes up and sets it on both servers, for when the one the
    customer has can't be used: the new server has rules of its own, and a password it won't take is a mailbox
    imapsync can't open there. Returns the password, or None when nothing was changed.

    This server is changed first, since its old hash is in hand to put back; the new server is told after, and
    a refusal there undoes this one.
    """
    ask = ask or _question
    run = run or run_command
    warn(NEW_PASSWORD_WARNING % address, say)
    if not _is_yes(ask("Give %s a new password on both servers?" % address)):
        note("%s keeps the password it has." % address, say)
        return None
    try:
        stored = _stored_hash(address, database, run=run) if database else None
    except CommandFailed as problem:
        fail("I can't read what this server has for %s (%s), so I won't change it."
             % (address, problem.message), say)
        return None
    if not stored:
        fail("This server has no password stored for %s, so there is nothing to change here." % address, say)
        return None
    password = new_password()
    try:
        made = _hash_for(password, stored, run=run)
    except CommandFailed as problem:
        fail(problem.message, say)
        return None
    if made is None:
        fail("The password of %s is stored on this server in a way I can't write, so I won't touch it. Give "
             "the account a new password here yourself, then try again." % address, say)
        return None
    try:
        _store_hash_here(address, made, database, run=run)
    except CommandFailed as problem:
        fail("This server wouldn't take a new password for %s: %s" % (address, problem.message), say)
        return None
    if not store_password(connection, address, password, say=say, quiet=True):
        try:
            _store_hash_here(address, stored, database, run=run)
            note("The password of %s on this server is as it was." % address, say)
        except CommandFailed as problem:
            fail("The new server refused it, and putting the old password back here didn't work either (%s). "
                 "The password of %s on this server is now: %s" % (problem.message, address, password), say)
        return None
    heading("The new password of %s" % address, say)
    say("  " + _paint(password, "bold", LOOK["colour"]))
    note("It opens the mailbox on both servers. Write it down and give it to the customer: it is kept "
         "nowhere else.", say)
    return password


def set_passwords(connection, row, say=print, database=None, ask=None, old_server=None, new_server=None):
    """Goes through one customer's accounts, asking for each one's own password, checking it against what this
    server has stored, and giving it to the new server. The password is the customer's own, so it is never
    printed back and never written down. Returns the addresses that now open on both servers."""
    if not row.accounts():
        _say("%s has no mail accounts on this server, so there is no password to give." % row.name, say)
        return []
    _say(CHECKED_HERE, say)
    done = []
    for address in row.accounts():
        try:
            stored = _stored_hash(address, database) if database else None
        except CommandFailed as problem:
            _say("I can't read what this server has for %s (%s), so I can't check a password for it."
                 % (address, problem.message), say)
            continue
        if not stored:
            _say("This server has no password stored for %s, so there is nothing to check against." % address,
                 say)
            continue
        if give_password(connection, address, stored, say=say):
            done.append(address)
    if done and old_server and new_server:
        say("")
        _say("These open with the same password on both servers now, so their mail can be copied. The "
             "number for copying the mail on this customer's screen does it and says how far it is; by hand "
             "it is:", say)
        for address in done:
            say("")
            for line in _imapsync_line(old_server, new_server, address):
                say(line)
    return done


# The same folder mapping the new server's own copy uses, so a copy run by hand lands the mail in the same
# folders. --automap puts Sent and Drafts and Trash in the folders the new server calls them; the expression
# after it does the same for those folders with INBOX. in front of their names, which this server has for
# every mailbox that was moved here from a server before. Without it the sent mail of such a mailbox is
# copied into Sent and into INBOX.Sent both, and takes twice the room on the new server for ever.
MAP_FOLDERS = ("--automap --regextrans2 "
               "'s,^INBOX\\.(Sent|Sent Items|Sent Messages|Verzonden items|Verzonden Items|Drafts|Concepten|"
               "Trash|Deleted Items|Verwijderde items|Junk|Junk E-mail|Spam|Ongewenste e-mail)$,$1,i'")


def _imapsync_line(old, new, address, password=None):
    """The command to copy one account's mail across, ready to paste. The password is the account's own, which
    the customer has and this tool never keeps, so it is left to fill in."""
    secret = shlex.quote(password) if password else "'THE-PASSWORD-OF-THIS-ACCOUNT'"
    return "\n".join(["imapsync \\"] + [
        "  --host%s %s --user%s %s \\" % (n, shlex.quote(host), n, shlex.quote(address))
        + "\n  --password%s %s --ssl%s \\" % (n, secret, n)
        for n, host in ((1, old), (2, new))] + ["  " + MAP_FOLDERS]).splitlines()


# ---- The contacts and calendars that webmail keeps ---------------------------

# Webmail keeps an account's address books and calendars in a database of its own, one table for each of them,
# and its settings -- the signature and the filters -- in a row beside them. None of that is in the mailbox,
# so copying the mail leaves every one of them behind, and the customer opens webmail on the new server to an
# empty address book. sogo-tool writes them out here and reads them back in there, which is SOGo's own way of
# doing it: it survives the two servers running different versions of SOGo, where copying its tables across
# by hand would quietly break the first time the newer one keeps them differently.
SOGO_TOOL = "/usr/sbin/sogo-tool"
SOGO_FOLDERS = "sogo.sogo_folder_info"  # one row for each address book and calendar, with whose it is
WEBMAIL_DUMPS = "/var/tmp/migrate-webmail"  # where a backup waits here until it is copied over
WEBMAIL_TIMEOUT = ASK_TIMEOUTS["restore_webmail"]
WEBMAIL_WHAT = "contacts and calendars"
COPY_WEBMAIL = "copy the contacts and calendars"
WEBMAIL_WARNING = ("Webmail keeps an account's address books and calendars outside its mailbox, so the mail "
                   "being across doesn't bring them. sogo-tool writes them out here, the files go over, and "
                   "sogo-tool there reads them back in: the account's settings first, which hold its "
                   "signature and its filters, and then every folder it has. Nothing here is changed, and a "
                   "folder that is already there is added to and never emptied, so doing this twice costs "
                   "time and nothing else.")
WEBMAIL_NONE = ("%s has no address books or calendars in webmail on this server, so there is nothing of them "
                "to copy. Only what somebody made in webmail itself is kept there; what a mail program keeps "
                "on the person's own computer was never on this server at all.")
WEBMAIL_NO_TOOL = ("There is no sogo-tool at %s on this server, so its webmail's contacts and calendars "
                   "can't be written out. A server without webmail has none to copy.")
_WEBMAIL_FOLDERS = {}


def webmail_folders(run=run_command, notes=None):
    """{address: how many address books and calendars} webmail holds on this server. Empty for a server that
    never ran webmail, which is no trouble at all: there is then nothing of it to move."""
    try:
        rows = query(run, "SELECT c_path2, COUNT(*) FROM %s GROUP BY c_path2" % SOGO_FOLDERS)
    except CommandFailed as problem:
        if notes is not None and not problem.missing:
            notes.append("What webmail keeps beside the mail could not be read: %s." % problem.message)
        return {}
    found = {}
    for row in rows:
        if len(row) < 2 or "@" not in row[0]:
            continue
        try:
            found[row[0].strip().lower()] = int(row[1])
        except ValueError:
            continue
    return found


def remember_webmail(folders):
    """What was found, for the screens to read without asking the database again."""
    _WEBMAIL_FOLDERS.clear()
    _WEBMAIL_FOLDERS.update(folders or {})


def webmail_of(row):
    """{address: how many folders} of this customer's accounts that have something in webmail here."""
    return dict((address, _WEBMAIL_FOLDERS[address]) for address in row.accounts()
                if _WEBMAIL_FOLDERS.get(address))


def back_up_webmail(addresses, run=run_command, root=None):
    """Writes each account's address books and calendars into a file of its own, named after the account.
    Returns (the directory they are in, {address: what went wrong}) -- an account sogo-tool couldn't write
    out stops nothing: the others still go over, and the screen says which didn't."""
    where = root or WEBMAIL_DUMPS
    os.makedirs(where, mode=0o700, exist_ok=True)
    os.chmod(where, 0o700)
    trouble = {}
    for address in addresses:
        try:
            run(SOGO_TOOL, "backup", where, address, timeout=WEBMAIL_TIMEOUT)
        except CommandFailed as problem:
            trouble[address] = problem.message
            continue
        if not os.path.isfile(os.path.join(where, address)):
            trouble[address] = "sogo-tool wrote nothing out for it"
    return where, trouble


def copy_webmail(connection, row, ask=None, say=print, run=run_command, runner=None, backup=None):
    """The 'k' on a customer's screen: brings this customer's address books and calendars across. Returns the
    addresses that are now on the new server."""
    ask, runner = ask or _question, runner or run_visible  # rsync is further down this file than this is
    accounts = webmail_of(row)
    heading("Copying the %s of %s to the new server" % (WEBMAIL_WHAT, row.name), say)
    say("")
    if not accounts:
        note(WEBMAIL_NONE % row.name, say)
        return []
    if not os.access(SOGO_TOOL, os.X_OK):
        fail(WEBMAIL_NO_TOOL % SOGO_TOOL, say)
        return []
    note(WEBMAIL_WARNING, say)
    say("")
    for address in sorted(accounts):
        _say("%s: %s" % (address, _plural(accounts[address], "address book or calendar",
                                          "address books and calendars")), say)
    say("")
    if not _is_yes(ask("Copy the %s of %s to the new server now?" % (WEBMAIL_WHAT, row.name))):
        note("The %s of %s were left alone." % (WEBMAIL_WHAT, row.name), say)
        return []
    try:
        answer = connection.ask({"request": "webmail_place"}, timeout=ASK_TIMEOUTS["webmail_place"])
    except Stopped as problem:
        warn(problem.message, say)
        return []
    if not answer.get("ok"):
        fail(answer.get("error") or "The new server wouldn't say where the backups go.", say)
        return []
    there = answer.get("place")
    try:
        where, trouble = (backup or back_up_webmail)(sorted(accounts), run=run)
    except OSError as problem:
        fail("I couldn't write the %s out: %s" % (WEBMAIL_WHAT, problem), say)
        return []
    for address in sorted(trouble):
        warn("The %s of %s were not written out: %s" % (WEBMAIL_WHAT, address, trouble[address]), say)
    going = [address for address in sorted(accounts) if address not in trouble]
    if not going:
        fail("Nothing was written out, so nothing was copied.", say)
        return []
    try:
        copy_files(connection, where, there, "the %s of %s" % (WEBMAIL_WHAT, row.name), runner)
    except MoveFailed as problem:
        fail(problem.message, say)
        return []
    try:
        read = connection.ask({"request": "restore_webmail", "addresses": going}, timeout=WEBMAIL_TIMEOUT)
    except Stopped as problem:
        warn(problem.message, say)
        return []
    for line in read.get("trouble") or []:
        warn(line, say)
    done = read.get("restored") or []
    for address in done:
        success("The %s of %s are on the new server." % (WEBMAIL_WHAT, address), say)
    if not done:
        fail(read.get("error") or "The new server read nothing in.", say)
    return done


# ---- Copying the messages of a customer's mailboxes over ---------------------

COPY_WARNING = ("imapsync runs on the new server and reads the mailbox here over IMAP on port 993, like a "
                "mail program does: nothing here is deleted or changed, and no message is copied twice, so a "
                "copy that was stopped and started again brings over what is new since.")
COPY_NEEDS_PASSWORD = ("It opens the mailbox on both servers, so it needs the account's own password: asked "
                       "for here, checked against what this server has stored for it, and given to the new "
                       "server as well.")
COPY_WATCH_EVERY = 10  # seconds between asking the new server how far the copies are


def _this_server(look=None):
    """The name or address the new server reaches this one at, for imapsync there to open the mailbox here:
    this server's own address from the outside, and its name when there is none to be found."""
    addresses = sorted(look() if look else _our_addresses(), key=lambda one: (":" in one, one))
    return addresses[0] if addresses else socket.gethostname()


def _copy_status(connection, address):
    """What the new server says about the copy of one mailbox: whether one is running, how far it got, and
    what it last said. An answer it can't give is read as nothing running, which is the safe way round: the
    worst that comes of it is being offered a copy that is already done, and that copies nothing twice."""
    answer = connection.ask({"request": "copy_mail_status", "address": address})
    if answer.get("ok"):
        return answer
    return {"ok": False, "started": False, "running": False, "error": answer.get("error")}


def _copy_words(found):
    """How far a copy is, in a few words."""
    copied = _plural(found.get("copied") or 0, "message", "messages")
    if not found.get("started"):
        return "not copied here yet"
    if found.get("running"):
        return "%s so far" % copied
    if found.get("errors"):
        return "%s, %s" % (copied, _plural(found["errors"], "error", "errors"))
    if found.get("exit") == 0:
        return "%s, no errors" % copied
    return "stopped after %s" % copied


def _watch_copies(connection, addresses, say=print, doing=None, every=COPY_WATCH_EVERY, wait=None):
    """Keeps asking how far the copies are until none of them is running any more, saying each change as it
    comes. Ctrl-C stops the watching only: the copies go on, on the new server, and the number that started
    them shows them again. Returns the ones that ended while it watched."""
    doing = doing if doing is not None else [""]
    wait = wait or time.sleep
    left, said = list(addresses), {}
    _say("Copying. A big mailbox takes hours; press Ctrl-C to stop watching and leave them running.", say)
    try:
        while left:
            doing[0] = "copying the mail of %s" % ", ".join(left)
            wait(every)
            for address in list(left):
                found = _copy_status(connection, address)
                words = _copy_words(found)
                if not found.get("running"):
                    left.remove(address)
                    say("  %s: %s" % (address, words))
                    if found.get("exit") not in (0, None) and found.get("last"):
                        _say(found["last"], say)  # it stopped on something; the line it stopped on says what
                    continue
                if words != said.get(address):
                    said[address] = words
                    say("  %s: %s" % (address, words))
    except KeyboardInterrupt:
        say("")
        _say("Stopped watching. The copies keep running on the new server; the same number says how far they "
             "are.", say)
    return [address for address in addresses if address not in left]


def _mailbox_words(found):
    """What one mailbox holds, in a few words."""
    return "%s in %s" % (_size(found.get("bytes") or 0),
                         _plural(found.get("messages") or 0, "message", "messages"))


def _copy_compare(address, mine, there, short, say=print):
    """What one mailbox holds on each server, in a small block of its own above the question about copying it.
    It is the one thing on the screen that answers "is this one across already", so it is not a sentence in a
    paragraph of warnings: a line per server, under the address, and a mark on what it comes to."""
    heading(address, say)
    if not mine:
        note("No mailbox under %s on this server, so there is nothing of it to copy." % VMAIL, say)
        return
    width = max(len("here"), len(short))
    say("  %s  %s" % ("here".ljust(width), _mailbox_words(mine)))
    if there is None:
        note("How much of it the new server holds couldn't be asked.", say)
        return
    theirs = there.get(address)
    say("  %s  %s" % (short.ljust(width), _mailbox_words(theirs) if theirs else "nothing yet"))
    if theirs and _across(mine, theirs):
        if _synced(mine, theirs) and (mine.get("messages") or 0) > (theirs.get("messages") or 0):
            success("imapsync opened both mailboxes and found every message of this one on the new server. "
                    "The counts differ because a maildir holds files dovecot doesn't serve as messages.", say)
        else:
            success("Its messages are across already; copying again brings over what is new since.", say)
    elif theirs:
        missing = _left_of(mine, theirs)
        left = (mine.get("bytes") or 0) - (theirs.get("bytes") or 0)
        if missing > 0:
            warn("%s still to come, about %s of mail." % (_plural(missing, "message", "messages"),
                                                          _size(max(left, 0))), say)
        else:
            warn("Every message is there, but about %s less of them than here: they may have come over "
                 "cut short." % _size(max(left, 0)), say)


# ---- Counting a mailbox on both servers, with imapsync itself ---------------

COMPARE_WARNING = ("imapsync opens the mailbox on both servers and counts every folder of it, and copies "
                   "nothing: this only says whether the messages are across. It is the same program that "
                   "does the copying, reading the mailboxes the same way, so what it counts is what a mail "
                   "program sees, and not what lies in the folders on disk. Counting tens of gigabytes takes "
                   "minutes, and the account's own password is asked for, as for the copy itself.")
COMPARE_HEADS = ("Folder", "Messages here", "On %s", "Missing")
NOT_THERE_YET = "not there yet"


def _compare_short(name):
    """A folder as the screen names it: INBOX itself keeps its name, and the folders under it lose the prefix
    that only says they are under it."""
    for prefix in ("INBOX.", "INBOX/"):
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def _folder_holds(found):
    """How many messages one folder holds. The count is what this table is for: a folder can weigh the same on
    both servers and still be a message short, and the size of each one is noise beside that. The sizes are
    added up under the table, where they say how long a copy would take."""
    if not found:
        return NOT_THERE_YET
    return "%d" % (found.get("messages") or 0)


def _compare_groups(folders):
    """One folder for every folder, however many names it answers to. A mailbox that has been moved before
    opens its sent mail as Sent, as Verzonden items and as INBOX.Sent, and imapsync counts it once for each
    name: counting the same folder three times says three times as much is missing as really is. Two of the
    names are the same folder when they hold the same number of messages and the same number of bytes and
    hold something: every empty folder weighs the same as every other one, and those are left alone."""
    groups, order = {}, []
    for folder in folders:
        here = folder.get("here") or {}
        key = ((here.get("messages"), here.get("bytes")) if here.get("messages") else ("alone", id(folder)))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(folder)
    return [_one_folder(groups[key]) for key in order]


def _one_folder(listings):
    """One folder out of the names it answers to: the shortest of the names, and the fullest of what the new
    server holds under any of them."""
    if len(listings) == 1:
        return listings[0]
    names = sorted((folder.get("name") or "?" for folder in listings), key=lambda name: (len(name), name))
    fullest = max(listings, key=lambda folder: (folder.get("there") or {}).get("messages") or -1)
    return {"name": names[0], "names": names, "here": listings[0].get("here"),
            "there": fullest.get("there"), "name_there": fullest.get("name_there")}


def _also_called(folders):
    """How many of these folders this server lists under more than one name."""
    return sum(1 for folder in folders if len(folder.get("names") or []) > 1)


def _short_by(folder):
    """How many messages of one folder are not on the new server yet."""
    here = (folder.get("here") or {}).get("messages") or 0
    there = (folder.get("there") or {}).get("messages") or 0
    return max(here - there, 0)


COMPARE_ROWS = 20  # folders with a row of their own; a mailbox can have a hundred, and the ones that are
# short are the whole reason for the table. The rest are one line under it.


def _compare_order(folders):
    """The folders that are short first, then the ones the new server hasn't got at all, then the rest: what
    is missing has to be on the screen even for a mailbox with more folders than the screen has room for."""
    return sorted(folders, key=lambda folder: (not _short_by(folder), -_short_by(folder),
                                               folder.get("there") is not None, folder.get("name") or ""))


def _compare_rows(folders, short):
    """The table of the folders: what each holds here, what it holds there, and what is missing."""
    heads = [COMPARE_HEADS[0], COMPARE_HEADS[1], COMPARE_HEADS[2] % short, COMPARE_HEADS[3]]
    cells = []
    ordered = _compare_order(folders)
    for folder in ordered[:COMPARE_ROWS]:
        missing = _short_by(folder)
        cells.append([_compare_short(folder.get("name") or "?"),
                      _folder_holds(folder.get("here")), _folder_holds(folder.get("there")),
                      "%d" % missing if missing else "-"])
    rest = ordered[COMPARE_ROWS:]
    if rest:
        cells.append(["and %d more, all across" % len(rest),
                      "%d" % sum((folder.get("here") or {}).get("messages") or 0 for folder in rest),
                      "%d" % sum((folder.get("there") or {}).get("messages") or 0 for folder in rest), "-"])
    widths = _widths(heads, cells, 10)
    out = ["  " + "  ".join(_paint(head, "bold", LOOK["colour"]) + " " * (width - len(head))
                            for head, width in zip(heads, widths)).rstrip(),
           "  " + _rule(sum(widths) + 2 * (len(widths) - 1))]
    for line in cells:
        shown = []
        for i, text in enumerate(line):
            fitted = _fit(text, widths[i])
            style = {2: "dim" if text == NOT_THERE_YET else None,
                     3: None if text == "-" else "yellow"}.get(i)
            shown.append(_paint(fitted, style, LOOK["colour"]) + " " * (widths[i] - len(fitted)))
        out.append("  " + "  ".join(shown).rstrip())
    return out


def _compare_total(folders, part):
    """What these folders come to on one of the servers. Counted up from the folders themselves rather than
    taken from imapsync's own total, which adds a folder up once for every name it answers to."""
    held = [folder.get(part) or {} for folder in folders]
    return {"messages": sum(one.get("messages") or 0 for one in held),
            "bytes": sum(one.get("bytes") or 0 for one in held)}


SAME_FOLDER = ("%s here answers to more than one name, from a move this mailbox has been through before. "
               "Each of them is counted once here, and the copy puts the messages of all of them in the one "
               "folder on %s.")


def _compare_said(address, answer, short, say=print):
    """The answer about one mailbox, on the screen: the folders, and then what it comes to."""
    folders = _compare_groups(answer.get("folders") or [])
    for line in _compare_rows(folders, short):
        say(line)
    say("")
    missing = sum(_short_by(folder) for folder in folders)
    here, there = _compare_total(folders, "here"), _compare_total(folders, "there")
    held = "%s in %s here, %s in %s on %s." % (
        _plural(here.get("messages") or 0, "message", "messages"), _size(here.get("bytes") or 0),
        _plural(there.get("messages") or 0, "message", "messages"), _size(there.get("bytes") or 0), short)
    twice = _also_called(folders)
    if twice:
        note(SAME_FOLDER % (_plural(twice, "folder", "folders"), short), say)
        say("")
    if not missing:
        success("%s Every message of %s is across." % (held, address), say)
        return 0
    warn("%s %s of %s %s not there yet; number 9 copies them, and copies nothing twice."
         % (held, _plural(missing, "message", "messages"), address,
            "is" if missing == 1 else "are"), say)
    return missing


def _compare_one(connection, address, password, host, short, say=print, step=None):
    """Asks the new server to count one mailbox on both servers. Returns how many messages are missing, or
    None when it couldn't be counted."""
    step = _step if step is None else step
    try:
        answer = step("Counting %s on both servers" % address,
                      lambda: connection.ask({"request": "compare_mail", "address": address,
                                              "password": password, "host": host},
                                             timeout=COMPARE_TIMEOUT),
                      summary=lambda found: "done" if found.get("ok") else "no answer")
    except Stopped as problem:
        warn(problem.message, say)
        return None
    if not answer.get("ok"):
        warn(answer.get("error") or "The new server couldn't count that mailbox.", say)
        return None
    return _compare_said(address, answer, short, say)


def _compare_mail(connection, row, hello=None, doing=None, ask=None, say=print, database=None, step=None):
    """The 'c' on a customer's screen: counts every mailbox of this customer on both servers, folder by
    folder, and says what is missing. Nothing is moved and nothing is changed."""
    doing = doing if doing is not None else [""]
    accounts = row.accounts()
    if not accounts:
        _say("%s has no mail accounts on this server, so there is nothing to count." % row.name, say)
        return
    if row.word("mail") != DONE:
        _say("The mail of %s isn't all on the new server yet, so there are no mailboxes there to count "
             "against. Move the mail first." % row.name, say)
        return
    short = _fit(_short_name(_new_name(connection, hello)), 18)
    heading("Counting the mail of %s on both servers" % row.name, say)
    say("")
    note(COMPARE_WARNING, say)
    host = _this_server()
    for address in accounts:
        say("")
        heading(address, say)
        doing[0] = "counting the mail of %s" % address
        password = _password_for(address, database, say=say, ask=ask)
        if not password:
            note("Nothing was counted for %s." % address, say)
            continue
        _compare_one(connection, address, password, host, short, say=say, step=step)

def _password_for(address, database, say=print, ask=None):
    """The account's own password, asked for here and checked against the hash this server keeps for it. None
    when there is nothing here to check against, or when Enter was pressed."""
    try:
        stored = _stored_hash(address, database) if database else None
    except CommandFailed as problem:
        _say("I can't read what this server has for %s (%s), so I can't check a password for it."
             % (address, problem.message), say)
        return None
    if not stored:
        _say("This server has no password stored for %s, so there is nothing to check a password against."
             % address, say)
        return None
    return ask_password(address, stored, say=say, ask=ask)


def _copy_mail(connection, row, hello=None, doing=None, ask=None, say=print, database=None, watch=None,
               run=None):
    """The number on a customer's screen: copies the messages of this customer's mailboxes to the new server,
    one question per account. The accounts have to be there first, since the copy writes into the mailboxes
    there; a copy that is already running is not started again, only watched."""
    doing = doing if doing is not None else [""]
    ask = ask or _question
    watch = _watch_copies if watch is None else watch
    accounts = row.accounts()
    if not accounts:
        _say("%s has no mail accounts on this server, so there is no mail to copy." % row.name, say)
        return []
    if row.word("mail") != DONE:
        _say("The mail of %s isn't all on the new server yet, so there are no mailboxes there to copy into. "
             "Move the mail first." % row.name, say)
        return []
    short = _fit(_short_name(_new_name(connection, hello)), 18)
    heading("Copying the mail of %s" % row.name, say)
    say("")
    note(COPY_WARNING, say)
    say("")
    note(COPY_NEEDS_PASSWORD, say)
    host = _this_server()
    here, there = _mail_for(row, connection, doing)
    started, running = [], []
    for address in accounts:
        _copy_compare(address, here.get(address), there, short, say)
        found = _copy_status(connection, address)
        if found.get("running"):
            note("It is being copied already: %s." % _copy_words(found), say)
            running.append(address)
            continue
        if found.get("started"):
            note("It was copied here before: %s." % _copy_words(found), say)
        say("")
        if not _is_yes(ask("Copy the mail of %s to the new server now?" % address)):
            note("The mail of %s was left where it is." % address, say)
            continue
        password = _password_for(address, database, say=say)
        if password is None:
            # Without it nothing was tried, and a screen that comes straight back says nothing about why.
            warn("No password, so the mail of %s was not copied. imapsync opens the mailbox on both servers "
                 "like a mail program does, and there is no way in without one." % address, say)
            continue
        if not store_password(connection, address, password, say=say):
            # The new server has rules of its own, and a password it won't take is a mailbox imapsync can't
            # open there. One this tool makes up fits them and is set on both servers at once.
            password = new_password_on_both(connection, address, database, say=say, ask=ask, run=run)
            if not password:
                warn("The mail of %s was not copied: the same password has to open the mailbox on both "
                     "servers." % address, say)
                continue
        doing[0] = "starting the copy of %s" % address
        answer = connection.ask({"request": "copy_mail", "address": address, "password": password,
                                 "host": host})
        if not answer.get("ok"):
            fail("The new server didn't start the copy of %s: %s"
                 % (address, answer.get("error") or "it gave no reason"), say)
            continue
        started.append(address)
        running.append(address)
        success("The copy of %s is running on the new server." % address, say)
    if running:
        watch(connection, running, say=say, doing=doing)
    return started


# ---- The list of what is still to do, over every customer -------------------

# The board has a column for the mail, the website and the jobs of each customer, and no room for the rest of
# the move: whether the messages are across, whether this server still delivers mail it has handed over,
# whether a redirect is set up, and where the names point. Those are what this list is, and it is printed when
# it is asked for: it looks every customer's names up and walks their mailboxes, which takes a moment.
TODO_SECTIONS = (
    ("mail", "Mail that is not all on the new server"),
    ("messages", "Messages that are still only on this server"),
    ("redirect", "Redirects that are not set up on the new server"),
    ("delivery", "Mail this server still delivers itself, though the world sends it on"),
    ("dns", "Names that still don't point at the new server"),
)
TODO_DONE = "Nothing of that is left: the mail, the messages, the redirects, the delivery and the DNS are all "\
            "on the new server."


def todo(rows, status, connection, hello, short="the new server", run=None):
    """What is still to do over all customers, as {section: [(customer, what)]}."""
    found = dict((key, []) for key, _ in TODO_SECTIONS)
    for row in rows:
        if row.items["mail"] and row.word("mail") in (TO_DO, PART):
            found["mail"].append((row, _mail_behind(row, status, short)))
        here, there = _mail_for(row, connection)
        behind = _behind(here, there) if there is not None else []
        if behind:
            found["messages"].append((row, "%s in %s" % (
                _plural(_messages_left(here, there), "message", "messages"),
                _plural(len(behind), "mailbox", "mailboxes"))))
        for item in row.items["site"]:
            if item.get("redirect") and _state_of(item, status) != "exists":
                found["redirect"].append((row, _goes_to(item)))
        seen = dns_seen(row, hello, run=run)
        for name in kept_here(seen, run=run):
            found["delivery"].append((row, "number 7 stops it" if name == row.name
                                      else "%s; number 7 stops it" % name))
        still = _todo_dns(seen)
        if still:
            found["dns"].append((row, still))
    return found


def _mail_behind(row, status, short):
    """How much of one customer's mail the new server has against what is here: a count, not "there already",
    since the whole point of the line is what is missing."""
    here = there = (0, 0)
    for item in row.items["mail"]:
        mine = _mail_here(item)
        theirs = _mail_there(_said_about(item, status)) or (0, 0)
        here = (here[0] + mine[0], here[1] + mine[1])
        there = (there[0] + theirs[0], there[1] + theirs[1])
    said = "%d of %s" % (there[0], _plural(here[0], "account", "accounts"))
    if here[1]:
        said += " and %d of %s" % (there[1], _plural(here[1], "forward", "forwards"))
    return "%s on %s" % (said, short)


def _todo_dns(seen):
    """What of one customer the world is still not sent to the new server for, in a few words. Nothing when
    every name of it is there, and nothing when there is nothing to look up."""
    out = [what for what in DNS_WHATS if what in seen["here"]]
    out += ["%s of %s is somewhere else" % (what, name) for what, name, _said in seen["away"]]
    out += ["%s of %s can't be looked up" % (what, name) for what, name in seen["unknown"]]
    return ", ".join(out)


def show_todo(found, colour=False, say=print):
    """The list itself, a section at a time, with each customer's own number in front of it so the thing to do
    next to it can be typed straight away."""
    anything = False
    for key, title in TODO_SECTIONS:
        lines = found.get(key) or []
        if not lines:
            continue
        anything = True
        heading(title, say)
        width = max(len(row.name) for row, _what in lines)
        for row, what in lines:
            say("  %s  %s  %s" % (_paint("%2d" % (row.number or 0), "bold", colour),
                                  _fit(row.name, width).ljust(width), _paint(what, "dim", colour)))
    if not anything:
        success(TODO_DONE, say)


def _todo(connection, rows, status, hello, doing=None, colour=False):
    """What `t` does at the prompt."""
    doing = doing if doing is not None else [""]
    doing[0] = "looking at what is left to do for every customer"
    short = _fit(_short_name(_new_name(connection, hello)), 18)
    found = _step("Looking at every customer: their names, their mailboxes and what this server still does",
                  lambda: todo(rows, status, connection, hello, short),
                  lambda said: "done")
    show_todo(found, colour=colour)


# ---- What was typed ----------------------------------------------------------

def _number_words(word, count, notes, one="item", many="items"):
    """The numbers one word names, inside a list of 'count' things, with a note when it says more than that.
    None when the word is not a number or a range at all."""
    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", word)
    if not match:
        return None
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if last < first:
        first, last = last, first
        notes.append("%s is backwards, so it is taken as %d-%d." % (word, first, last))
    low, high = max(first, 1), min(last, count)
    if low > high:
        notes.append("%s is not on the list, so nothing is picked for it." % word)
        return []
    if (low, high) != (first, last):
        outside = [str(number) if number == other else "%d-%d" % (number, other)
                   for number, other in ((first, low - 1), (high + 1, last)) if number <= other]
        notes.append("The list has %s, so %s is taken as %s. Not on the list: %s." % (
            _plural(count, one, many), word, "%d-%d" % (low, high) if low != high else str(low),
            ", ".join(outside)))
    return list(range(low, high + 1))


UNDERSTAND = ('I don\'t understand: %s. Type a number to do its next step, a to do all of '
              'them, "i 3" to look inside one or "p 3" to give one new mail passwords.')
AFTER_NUMBER = '"%s" goes after a number, like "3 %s".'
LETTER_NEEDS = '"%s" says which customer, so it goes with a number, like "%s 1".'


class Typed(object):
    """What was typed, worked out: 'move' is what to move, in the order that works, 'look' the customers to
    look inside and 'passwords' the customers to give new mail passwords."""

    def __init__(self):
        self.move, self.look, self.passwords = [], [], []


def picked(rows, typed, say=print):
    """What was typed, as a Typed. A number does that customer's next step, "a" or "all" the next step of every
    customer, "3 all" every step customer 3 has left, "i 3" looks inside one and "p 3" gives one new mail
    passwords. A part by name ("3 mail") does that part alone. Ranges and commas work. A number that is not on
    the list is never acted on, and is said so."""
    out, picks, notes, unreadable = Typed(), [], [], []
    words = [word for word in re.split(r"[\s,]+", str(typed).lower()) if word]
    if not words:
        say("You typed nothing, so nothing is picked.")
    letter, last = None, []
    for word in words:
        stuck = re.fullmatch(r"([ipd])([0-9]+(?:-[0-9]+)?)", word)
        if word in ("i", "inside", "d", "detail", "p", "password", "passwords") or stuck:
            letter = (stuck.group(1) if stuck else word)[0]
            if not stuck:
                continue
            word = stuck.group(2)
        if word in PART_WORDS or (word in ("a", "all") and last):
            if not last:
                notes.append(AFTER_NUMBER % (word, word))
            for place in last:
                picks[place] = (picks[place][0], PART_WORDS.get(word, ""))
            continue
        chosen = (list(range(1, len(rows) + 1)) if word in ("a", "all")
                  else _number_words(word, len(rows), notes, "customer", "customers"))
        if chosen is None:
            unreadable.append(word)
            continue
        last = []
        for number in chosen:
            row = rows[number - 1]
            if letter == "p":
                if row not in out.passwords:
                    out.passwords.append(row)
            elif letter in ("i", "d"):
                if row not in out.look:
                    out.look.append(row)
            else:
                last.append(len(picks))
                picks.append((row, None))
        letter = None
    if letter:
        notes.append(LETTER_NEEDS % (letter, letter))
    if unreadable:
        say(UNDERSTAND % ", ".join(unreadable))
    seen = set()
    for row, part in picks:
        chosen = next_step(row)[0] if part is None else (row.to_move(part) if part else row.to_move())
        for item in chosen:
            if id(item) not in seen:
                seen.add(id(item))
                out.move.append(item)
        if not chosen:
            notes.append("Nothing of %s is left to do%s." % (
                row.name, " for its %s" % PART_HEADS[part].lower() if part else ""))
    for note in notes:
        _say(note, say)
    return out


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


def _apply_with_questions(connection, item, ask, report, ask_name=None):
    """(answer, question nobody answered or None). Each question from the new server goes to 'ask', which gives
    "yes" or "no"; any other reply (or none) means nobody answered.

    The question about a database that is already there has a third answer: another name for the site's
    database on the new server. That one is asked with 'ask_name', and the item is sent again under the new
    name, which the new server may have as well, in which case it asks about that one.
    """
    ask_name = _database_question if ask_name is None else ask_name
    answers, tried = {}, set()
    while True:
        answer, left_out = apply_item(connection, item, answers=answers)
        report.left_out.extend(left_out)
        question = answer.get("ask") if not answer.get("ok") else None
        if not isinstance(question, dict):
            return answer, None
        about = question.get("about")
        if about in answers:
            return answer, None
        text = question.get("question") or "The new server has a question about %s." % _title(item)
        reply = (ask_name if about == "database" else ask)(text)
        reply = reply.strip().lower() if isinstance(reply, str) else None
        if about == "database" and reply not in ("yes", "no", None):
            if reply in tried:
                return answer, question  # it was offered and the new server has that one too
            tried.add(reply)
            item = dict(item, new_database=reply)
            continue
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
    """The programs this server needs to bring a site over that
    aren't here. Looked for before the site is made on the new server:
    finding out afterwards leaves a site made there with nothing in it."""
    which = which or shutil.which
    if item.get("kind") == "site":
        wanted = ["rsync"] + (["mysqldump"] if item.get("database") else [])
    else:
        return []
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
        extra_notes=None, wp=None, ask_name=None):
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
    'php' and 'wp' are the PHP and the wp-cli the new server says it has: a
    cron job is rewritten to run those, with its paths under the new web
    root, before it is sent, and what changed is put in 'extra_notes' for
    the report."""
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
        heading("Moving %d of %d: %s %s" % (index + 1, len(items),
                                            KIND_NAMES.get(item.get("kind"), "Item"), _title(item)))
        made = False  # a site the new server made, whose files are not there yet
        try:
            if item.get("kind") == "cron":
                try:
                    item, changed = cron_for_new_server(
                        item, _new_web_root(item, docroots, notes), php, wp)
                except MoveFailed as problem:
                    report.skipped.append((item, problem.message))
                    warn(problem.message)
                    continue
                for text in changed:
                    if extra_notes is not None and text not in extra_notes:
                        extra_notes.append(text)
            missing = _missing_programs(item)
            if missing:
                why = NEEDS_PROGRAM % (", ".join(missing), _title(item),
                                       " ".join(missing))
                report.skipped.append((item, why))
                warn(why)
                continue
            left = notes.entry(item) if notes is not None else {}
            said = (status or {}).get(item.get("id")) or {}
            if (left.get("status") == "made" and left.get("docroot")
                    and said.get("state") == "exists"):
                # Made in an earlier run: only its files are missing. No
                # apply happens here, so the question the apply would have
                # asked about the database is asked here: a run that got
                # halfway may have left data in it, and the dump replaces
                # whatever is there.
                answer = {"ok": True, "docroot": left["docroot"],
                          "database": left.get("database")}
                if left.get("database") and not _is_yes(
                        ask(REPLACE_QUESTION % (left["database"],
                                                _title(item)))):
                    report.skipped.append((item, NOT_REPLACED % _title(item)))
                    note(NOT_REPLACED % _title(item))
                    continue
                unanswered = None
            else:
                answer, unanswered = _apply_with_questions(
                    connection, item, ask, report, ask_name)
            if unanswered is not None:
                report.failed.append((item, "The new server asked a question "
                                      "that got no answer: %s"
                                      % unanswered.get("question")))
            elif answer.get("ok"):
                note = answer.get("note")
                if (extra_notes is not None and isinstance(note, str)
                        and note.strip() and note not in extra_notes):
                    extra_notes.append(note)
                # A redirect is the whole site: the new server has written its
                # rules, and there is nothing to copy after it.
                made = item.get("kind") == "site" and not item.get("redirect")
                if item.get("kind") == "site" and isinstance(answer.get("docroot"), str):
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
            fail("Stopping here, since nothing else would work either: %s"
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
            warn("Stopped, as you asked.")
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


NOT_WORDPRESS = ("No wp-config.php was found in %s, so this is not a WordPress site: its database settings "
                 "are in a file somewhere in its own code, and nothing here can read them. Type f and pick "
                 "its database, and its files and that database are copied over; the new server then says "
                 "the password, and you put it in the site's own settings yourself.")
SIGNAGE_SITE = ("piSignage runs behind this name on port %d, and Apache here does nothing but hand requests "
                "on to it, so there are no website files to copy. Type g, or run migrate.py signage, and its "
                "media, its data folder and its MongoDB database are brought across into a piSignage server "
                "the new one makes for it.")
SAME_USER = ("%s would get the same Linux user on the new server as %s (the "
             "name of both comes down to %s), and one user there means one "
             "home, one web root and one wp-config.php. Move this one on its "
             "own, under a name of its own: migrate.py --host NEW --domain "
             "%s --user NAME")


REDIRECT_BY_HAND = ("Not all of what %s does moves with it: %s")


def _leftover(site):
    """What this name does besides sending visitors somewhere else, which no rule in a .htaccess can do for
    it. It is said where the site stands, not only in the report: it is the half that is still a person's."""
    lines = (site.get("redirect") or {}).get("by_hand") or []
    return REDIRECT_BY_HAND % (site["name"], " ".join(lines)) if lines else None


def site_items(found, owner_of=_owner_of, users=None):
    """One item per site from sites(). The id is the same on every run. The
    database password is not in the item: move_site reads it from the site's
    wp-config.php when it needs it. 'users' is {domain: user} for a site
    whose Linux user was given on the command line. A site that is not
    WordPress is minted like any other, with the reason it is not moved: the
    design has those listed with what was found and left alone."""
    given = users or {}
    items = []
    for site in found:
        name = site["name"].strip().lower()
        docroot = site.get("docroot")
        problem = site.get("problem")
        redirect = site.get("redirect") if redirect_only(site) else None
        other = False
        if not problem and site.get("signage"):
            problem = SIGNAGE_SITE % site["signage"]["port"]
        elif not problem and not site.get("wordpress") and not redirect:
            problem, other = NOT_WORDPRESS % (docroot or "its web directory"), True
        items.append({
            "id": "site:" + name, "kind": "site", "name": name,
            "server": site.get("server"),
            "user": given.get(name) or site_user(name),
            "old_owner": owner_of(docroot) if docroot else None,
            "aliases": site.get("aliases") or [], "docroot": docroot,
            "database": site.get("database"),
            "db_user": site.get("db_user"), "db_host": site.get("db_host"),
            "size": site.get("size"), "problem": problem,
            "wordpress": bool(site.get("wordpress")),
            "redirect": redirect, "leftover": _leftover(site),
            "signage": (site.get("signage") or {}).get("port"), "other": other})
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
    keeps it whatever its place in the alphabet. A site that is not being
    moved anyway holds no name: it would refuse a site that is."""
    kept = {}
    for item in sorted(items, key=lambda item: (item["name"] not in given,
                                                item["name"])):
        if item.get("problem"):
            continue
        first = kept.setdefault(item["user"], item)
        if first is not item:
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
NO_NEW_WP = ("The new server didn't say where its wp-cli is, so %s can't be "
             "replaced by it and this job would not run there. Install "
             "wp-cli there and send the job again.")
# Paths that mean the same on any server, so they travel as they are: the places a job throws its output away,
# and the scratch folders where a job makes its own lock file.
_KEEP_PATHS = ("/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr")
_KEEP_FOLDERS = ("/tmp/", "/var/tmp/")
# The directories every Linux has, so a PATH that names them says nothing about the old server.
_ORDINARY_PLACES = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin")
PATH_FROM_OLD = ("This job's PATH is kept as it is, and it names %s, which "
                 "may not be there on the new server. A program this job "
                 "runs by its bare name has to be in one of the directories "
                 "its PATH names, so look at that job once it has run.")


def _is_php(word):
    """Whether a program is a PHP: php, php7.4, lsphp81, /opt/plesk/php/8.1/bin/php and the panel copies."""
    return bool(word) and _PHP_NAME.match(os.path.basename(word)) is not None


def _is_wp(word):
    """Whether a program is wp-cli, which most of a WordPress site's own jobs run."""
    return bool(word) and os.path.basename(word) == "wp"


def _program_place(interpreter, instead):
    """The directory the program a job runs came out of on the old server,
    when the new server has a place of its own for it; None when there is
    nothing to put in its place."""
    if not (instead and interpreter.startswith("/")):
        return None
    return os.path.dirname(interpreter)


def _new_program(interpreter, php, wp):
    """Where the program a job runs is on the new server: the PHP or the
    wp-cli the new server named itself. None for a program only the old
    server knows the place of, and None when the new server doesn't have
    this one."""
    if _is_php(interpreter):
        return php
    if _is_wp(interpreter):
        return wp
    return None


def _cron_parts(item):
    """(the start of the job's line that is not rewritten, its command). The
    time fields are left alone: "*/15" would otherwise read as the path
    /15."""
    line, command = item.get("line") or "", item.get("command")
    if isinstance(command, str) and command and line.endswith(command):
        return line[:len(line) - len(command)], command
    return "", line


def _mapped_path(path, old_root, new_root, interpreter, instead):
    """A path of a cron job as it must read on the new server, or None when
    there is no way to know: inside the site it is the same path under the
    new web root, the program the job runs becomes 'instead', and a path
    that means the same everywhere stays as it is."""
    inside = os.path.normpath(path)
    if old_root and (inside == old_root or inside.startswith(old_root + "/")):
        return new_root.rstrip("/") + inside[len(old_root):]
    if path in _KEEP_PATHS or path.startswith(_KEEP_FOLDERS):
        return path
    if interpreter and path == interpreter and instead:
        return instead
    return None


def _rewrite_paths(text, old_root, new_root, interpreter, instead):
    """(the text with every path pointing at the new server, the paths it
    has no mapping for, which are left as they were)."""
    loose = []

    def one(found):
        path = found.group(0)
        mapped = _mapped_path(path, old_root, new_root, interpreter, instead)
        if mapped is None:
            loose.append(path)
            return path
        return mapped

    return _CRON_PATH.sub(one, text), loose


def _rewrite_settings(settings, old_root, new_root, interpreter, instead):
    """(the NAME=value lines as they must be on the new server, what changed
    in plain words). A PATH is the old server's own: the directory of the
    program the job runs becomes the new one's and a directory inside the
    site moves with the site. Nothing is ever dropped from a PATH — a job
    that needed it would fail in a way nobody could explain — so an entry
    that can't be mapped is kept and named, unless it is a place every
    Linux has."""
    old_place = _program_place(interpreter, instead)
    new_place = os.path.dirname(instead) if instead else None
    kept, said = [], []
    for line in settings:
        name, sign, value = line.partition("=")
        if not sign:
            kept.append(line)
            continue
        if name.strip().upper() == "PATH":
            places, changed, from_old = [], False, []
            for place in value.split(":"):
                place = place.strip()
                if not place:
                    continue
                moved = (new_place if old_place and place.rstrip("/") == old_place
                         else _mapped_path(place, old_root, new_root, None, None))
                if moved is not None and moved != place:
                    place, changed = moved, True
                elif (place.startswith("/") and place.rstrip("/") not in _ORDINARY_PLACES
                        and _mapped_path(place, old_root, new_root, None, None) is None):
                    from_old.append(place)
                if place not in places:
                    places.append(place)
            if places and changed:
                said.append("The PATH this job runs with on the new server "
                            "is %s." % ":".join(places))
            if from_old:
                said.append(PATH_FROM_OLD % ", ".join(from_old))
            kept.append("PATH=" + ":".join(places) if places else line)
            continue
        moved = _rewrite_paths(value, old_root, new_root, None, None)[0]
        if moved != value:
            said.append("The %s this job runs with on the new server is %s."
                        % (name.strip(), moved.strip()))
        kept.append(name + sign + moved)
    return kept, said


def cron_for_new_server(item, docroot, php, wp=None):
    """The cron job as it must be sent: every path inside the site pointing
    at the new server's web root, and the PHP or wp-cli it runs replaced by
    the one the new server named. Returns (the item to send, what changed in
    plain words). Raises MoveFailed, with the same words the dashboard
    showed, when there is something it can't rewrite; nothing on either
    server is touched then.
    """
    name = item.get("site") or "its site"
    old_root = (item.get("site_docroot") or "").rstrip("/")
    if not old_root:
        raise MoveFailed(NO_OLD_ROOT % name, told=False)
    if not docroot:
        raise MoveFailed(NO_NEW_ROOT % name, told=False)
    interpreter = item.get("interpreter") or ""
    instead = _new_program(interpreter, php, wp)
    if _is_php(interpreter) and not instead:
        raise MoveFailed(NO_NEW_PHP % interpreter, told=False)
    if _is_wp(interpreter) and not instead:
        raise MoveFailed(NO_NEW_WP % interpreter, told=False)
    before, command = _cron_parts(item)
    command, loose = _rewrite_paths(command, old_root, docroot, interpreter,
                                    instead)
    if loose:
        raise MoveFailed(OUTSIDE_SITE % ", ".join(sorted(set(loose))),
                         told=False)
    command = _written_out(command, interpreter, instead)
    line = before + command
    settings, said = _rewrite_settings(item.get("settings") or [], old_root,
                                       docroot, interpreter, instead)
    if line != item.get("line"):
        said.insert(0, "The job of %s runs this on the new server: %s"
                    % (name, line))
    return dict(item, line=line, settings=settings), said


def _written_out(command, interpreter, instead):
    """The command with a program the job runs by its bare name written out
    as the path the new server named. A job that runs 'php' leans on the old
    server's PATH, and the new server's own PHP may not be called php at all
    (OpenLiteSpeed's is lsphp), so the word is replaced rather than hoped
    for. Only the first of them, which is the program; the same word inside
    a path is left alone by the edges of the pattern."""
    if not instead or interpreter.startswith("/") or not interpreter:
        return command
    pattern = r"(?<![\w./-])%s(?![\w./-])" % re.escape(interpreter)
    return re.sub(pattern, lambda found: instead, command, count=1)


def _cron_problem(item):
    """Why a job of a site can't be set up on the new server as it is, in
    plain words; None when it can. The same paths are looked at as when it
    is sent, but the new server's own places aren't known yet, so the site's
    own folder and a program the new server can name both stand in for
    themselves, and only what cannot be mapped at all counts."""
    old_root = (item.get("site_docroot") or "").rstrip("/")
    if not old_root:
        return NO_OLD_ROOT % (item.get("site") or "its site")
    interpreter = item.get("interpreter") or ""
    instead = interpreter if _new_program(interpreter, interpreter,
                                          interpreter) else None
    loose = _rewrite_paths(_cron_parts(item)[1], old_root, old_root,
                           interpreter, instead)[1]
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

    def __init__(self, path=None, say=print):
        self.path, self.say, self.items = path or STATE_PATH, say, {}
        self.server = None  # the new server's name, so it is asked for once
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, NotADirectoryError):
            return
        except (OSError, ValueError):
            say("I couldn't read my notes in %s, so I start without them. A "
                "site made earlier but never filled will show as already on "
                "the new server." % self.path)
            return
        if isinstance(data, dict) and isinstance(data.get("server"), str):
            self.server = data["server"]
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.items = {k: v for k, v in data["items"].items()
                          if isinstance(v, dict)}
        else:
            say("My notes in %s are not in a form I know, so I start without "
                "them. A site made earlier but never filled will show as "
                "already on the new server." % self.path)

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

    def moved(self, item, detail=None):
        """Writes down that this item is completely moved. 'detail' is what is worth keeping about it for a
        later report."""
        entry = dict(self.entry(item), status="moved")
        if isinstance(detail, dict):
            entry.update(detail)
        self.items[item["id"]] = entry
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
                json.dump({"version": 1, "server": self.server, "items": self.items}, handle,
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


REPLACE_QUESTION = ("The database %s on the new server, for the site %s, "
                    "will be emptied and replaced by the one on this "
                    "server. Go on?")
NOT_REPLACED = ("Nothing was done for %s: the database was not to be "
                "replaced.")


def _is_yes(reply):
    return isinstance(reply, str) and reply.strip().lower() in ("yes", "y")


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


def run_into(path, *args):
    """Runs a command with its output going straight into a file, and gives its exit code. A dump of a big
    database never passes through this program's memory, and there is no time limit, for the reason given at
    run_visible."""
    try:
        with open(path, "wb") as out:
            return subprocess.call(args, stdin=subprocess.DEVNULL, stdout=out)
    except FileNotFoundError:
        raise CommandFailed("%s isn't installed" % args[0], missing=True)
    except OSError as problem:
        raise CommandFailed("can't write %s: %s" % (path, problem))


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
        reply = ask(REPLACE_QUESTION % (made_database, name)) if ask is not None else None
        if not _is_yes(reply):
            return {"ok": False, "skipped": True,
                    "message": NOT_REPLACED % name}
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
    reply = _step("Finishing the site on the new server",
                  lambda: connection.ask({"request": "finish_site", "item": site}),
                  lambda said: "done" if said.get("ok") else "failed")
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
    Report: moved, skipped (said no) and failed. The new server's place for a
    site's files is the one remembered from the run that made it."""
    report = Report()
    for index, item in enumerate(items):
        entry = notes.entry(item)
        try:
            if not entry.get("docroot"):
                raise MoveFailed("I don't know where %s's files go on the "
                                 "new server: it was not made by this tool "
                                 "here." % item["name"])
            else:
                result = move_site(connection, item, entry, again=True,
                                   notes=notes, ask=ask,
                                   extra_notes=extra_notes, **fakes)
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


# ---- Moving what SpamAssassin learned ----------------------------------------

# The tables that make a database SpamAssassin's Bayes, the part of it that learns from the mail it is shown.
BAYES_TABLES = ("bayes_token", "bayes_vars")
_BAYES_DATABASE = re.compile(r"[A-Za-z0-9_]+")
SPAM_QUESTION = ("The new server has learned from %s itself. This server's learning (%s) is added to it: "
                 "neither is thrown away, and a message both have already learned from is not counted twice. "
                 "Go on?")


def _learned_words(counts):
    return "%s spam and %s good messages" % ("{:,}".format(counts.get("spam") or 0),
                                             "{:,}".format(counts.get("ham") or 0))


def spam_learning(run=run_command):
    """(database, counts) of what SpamAssassin has learned on this server: the database its Bayes is kept in, and
    {"spam", "ham", "tokens"}. Raises MoveFailed, in words for a person, when there is none or it can't be read."""
    names = ", ".join("'%s'" % table for table in BAYES_TABLES)
    try:
        rows = query(run, "SELECT table_schema FROM information_schema.tables WHERE table_name IN (%s) "
                          "GROUP BY table_schema HAVING COUNT(DISTINCT table_name) = %d ORDER BY table_schema"
                     % (names, len(BAYES_TABLES)))
        found = [row[0] for row in rows if row and _BAYES_DATABASE.fullmatch(row[0])]
        if not found:
            raise MoveFailed("This server keeps no spam learning in MySQL: no database has SpamAssassin's Bayes "
                             "tables. So there is none to move.")
        database = "bayes" if "bayes" in found else found[0]
        counts = query(run, "SELECT (SELECT COALESCE(SUM(spam_count), 0) FROM `{0}`.bayes_vars), "
                            "(SELECT COALESCE(SUM(ham_count), 0) FROM `{0}`.bayes_vars), "
                            "(SELECT COUNT(*) FROM `{0}`.bayes_token)".format(database))
    except CommandFailed as problem:
        raise MoveFailed("I couldn't look at the spam learning on this server: %s." % problem.message)
    if len(counts) != 1 or len(counts[0]) != 3 or not all(count.isdigit() for count in counts[0]):
        raise MoveFailed("I couldn't read how much the spam learning in %s holds." % database)
    return database, dict(zip(("spam", "ham", "tokens"), map(int, counts[0])))


def move_spam_learning(connection, ask, run=run_command, piper=run_piped):
    """Brings what SpamAssassin learned here to the new server and adds it to what that one learned itself. It
    goes into a database beside the new server's own first, and is added to it only once all of it is there. Returns
    a dict with 'ok' and 'steps', or 'skipped' and 'message'; raises MoveFailed, or Stopped when the new server can't
    be reached. 'ask' is asked first when the new server has learned something of its own."""
    database, here = spam_learning(run)
    if not here["tokens"]:
        return {"ok": False, "skipped": True,
                "message": "SpamAssassin on this server has learned nothing yet, so there is nothing to move."}
    there = connection.ask({"request": "bayes"})
    if not there.get("ok"):
        raise MoveFailed("The new server can't take the spam learning: %s"
                         % (there.get("error") or "it gave no reason."))
    if there.get("tokens") and not _is_yes(ask(SPAM_QUESTION % (_learned_words(there), _learned_words(here)))):
        return {"ok": False, "skipped": True,
                "message": "The spam learning was not moved: the new server keeps its own."}
    ready = connection.ask({"request": "bayes_prepare"})
    target = ready.get("database")
    if not ready.get("ok") or not (isinstance(target, str) and _BAYES_DATABASE.fullmatch(target)):
        raise MoveFailed("The new server has no place ready for the spam learning: %s"
                         % (ready.get("error") or "it did not say where it goes."))
    dump = ["mysqldump", "--single-transaction", "--quick", "--hex-blob", "--add-drop-table", "--", database]
    try:
        dumped, read = _step("Sending the spam learning (%s)" % _learned_words(here),
                             lambda: piper(dump, connection.command("mariadb", target)),
                             lambda codes: "sent" if codes == (0, 0) else "failed")
    except CommandFailed as problem:
        raise MoveFailed("The spam learning was not sent: %s. The new server's own is as it was."
                         % problem.message)
    if read == 255 or (dumped < 0 and read != 0):
        raise MoveFailed("The connection to the new server was lost while the spam learning was sent. The new "
                         "server's own is as it was; send it again.")
    if dumped != 0:
        raise MoveFailed("mysqldump stopped with code %d, so the spam learning was not moved. The new server's own "
                         "is as it was." % dumped)
    if read != 0:
        raise MoveFailed("The new server did not read the spam learning in (code %d). Its own is as it was."
                         % read)
    swapped = connection.ask({"request": "bayes_swap"})
    if not swapped.get("ok"):
        raise MoveFailed("The spam learning came across, but the new server did not add it to its own: %s"
                         % (swapped.get("error") or "it gave no reason."))
    return {"ok": True, "steps": ["Moved the spam learning. SpamAssassin on the new server now knows %s."
                                  % _learned_words(swapped)]}


def _move_spam(connection, doing=None, ask=None):
    """What `spam` and s do: moves the spam learning and says what came of it. Returns the exit code."""
    doing = doing if doing is not None else [""]
    doing[0] = "moving the spam learning"
    heading("Moving the spam learning")
    try:
        result = move_spam_learning(connection, ask or _question)
    except MoveFailed as problem:
        fail(problem.message)
        return 1
    if result.get("ok"):
        success(" ".join(result["steps"]))
    else:
        note(result.get("message"))
    return 0


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


# The colour of a section of the report, by how its title starts: what worked, what didn't, what was left.
_SECTION_STYLES = (("Moved", "green"), ("Did not work", "red"), ("The run stopped", "red"), ("Not tried", "yellow"),
                   ("Left alone", "yellow"), ("Made on the new server, but", "yellow"))


def _section(out, title, lines):
    if lines:
        style = next((style for start, style in _SECTION_STYLES if title.startswith(start)), None)
        out.extend([""] + [_paint(_paint(line, "bold", LOOK["colour"]), style, LOOK["colour"])
                           for line in textwrap.wrap(title, 79)] + lines)


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


def _same_item(one, other):
    return one.get("id") is not None and one.get("id") == other.get("id")


def _cron_by_hand(item):
    return item.get("kind") == "cron" and item.get("problem") == NO_SITE_JOB


def loose_databases(found_sites, databases, mail_database):
    """The databases on this server that belong to no site: not a site's own, not the mail server's and not one
    the server itself needs. They are only reported, never moved."""
    known = set(SYSTEM_DATABASES) | {SPAM_DATABASE}
    known.update(name for name in (mail_database,) if name)
    known.update(site["database"] for site in (found_sites or []) if site.get("database"))
    return sorted(name for name in (databases or {}) if name not in known)


def _inventory_lists(found_sites, databases, mail_database, jobs, notes):
    """The lists about the whole server, as lines: (cron jobs that belong to
    no site, sites that are not WordPress, databases that belong to no site,
    what could not be read, redirect rules that have to be set up by hand).
    A site that is nothing but a redirect is moved, so it is not among the
    ones to copy by hand."""
    others = [s for s in (found_sites or [])
              if not s.get("wordpress") and not redirect_only(s)]
    return (["%s (user %s)" % (j["line"], j.get("old_user") or j.get("user"))
             for j in jobs],
            ["%s (files in %s)" % (s["name"], s.get("docroot")
                                   or "an unknown place") for s in others],
            loose_databases(found_sites, databases, mail_database),
            list(notes or []),
            ["%s: %s" % (s["name"], line) for s in (found_sites or [])
             for line in ((s.get("redirect") or {}).get("by_hand") or [])])


def report(result, connection=None, old_server=None, found_sites=None,
           mail=None, databases=None, items=None, notes=None,
           extra_notes=None, mail_database=None, addresses=None,
           everything=False, new_server=None, passwords=None):
    """What a run did and what is still to do by hand, as text of at most
    79 columns. 'connection' is asked for the DNS records; 'found_sites' is
    sites(), 'databases' is database_sizes(), 'items' everything the
    dashboard offered, 'mail' the Mail from mail_inventory(), 'notes'
    what the discovery could not read and 'addresses' the addresses the new
    server says it has, for the sites' DNS records. It is about the move that
    just happened; only with 'everything' does it go on to the lists about
    the whole server (what is not WordPress, loose databases, jobs of no
    site, what could not be read), which 'list' shows too."""
    new_name = getattr(connection, "host", None) or new_server
    known_host = bool(new_name) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", str(new_name)) is not None
    new_name = new_name if known_host else "the new server"  # a phrase, for sentences only; never in a command
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

    cron_by_hand = _cron_by_hand

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

    domains = [item.get("name") or "" for item, _ in moved
               if item.get("kind") == "mail_domain"]
    if passwords:
        _section(out, "Accounts that now open with the same password on both servers:",
                 _bullets(passwords))
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
    lists = _inventory_lists(found_sites, databases, mail_database, jobs, notes)
    if everything:
        _section(out, "Cron jobs that belong to no site, so they were not "
                 "moved:", _bullets(lists[0]))

    mail_left = list(getattr(mail, "left_out", None) or [])
    if not everything:
        wanted = [d.lower() for d in domains]
        mail_left = [t for t in mail_left
                     if any(d and d in t.lower() for d in wanted)]
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
    if everything:
        _section(hand, "Sites that are not WordPress. They are not moved; "
                 "copy them by hand:", _bullets(lists[1]))
        _section(hand, "Parts of a redirect that can't be moved. Set these "
                 "up on the new server by hand:", _bullets(lists[4]))
        _section(hand, "Databases that belong to no site. They are not "
                 "moved:", _bullets(lists[2]))
        _section(hand, "What the server could not read while looking around:",
                 _bullets(lists[3]))
    if hand:
        out.extend(["", "Still to do by hand", "-------------------"])
        out.extend(hand[1:] if hand[0] == "" else hand)
    if not out:
        return "Nothing was moved.\n"
    if not (result.moved or result.files_to_copy):
        # A page of nothing but hand work would otherwise leave the one thing
        # the person wants to know first unsaid.
        out = ["", "Nothing was moved."] + out
    return "\n".join([_paint("Report", "bold", LOOK["colour"]), "======"] + out) + "\n"


# ---- Sites that are neither WordPress nor a redirect -------------------------
#
# A site this tool can't read the settings of is still a directory of files and, nearly always, one database.
# Both can be copied; what can't be copied is the line in the site's own code that holds the database password,
# because nothing here knows which file that is. So the files and the database go over, and the new server says
# the password out loud for a person to put in place. That is the whole of what is left by hand.

OTHER_WARNING = ("This copies the files of %s to the new server, and the database you pick with them. The dump "
                 "is written into the site's own directory here, goes over with the files, and is read in "
                 "there; the new server moves it out of the web root afterwards, where anyone could have "
                 "downloaded it. Nothing here is changed or deleted.")
OTHER_BY_HAND = ("A site that is not WordPress keeps its database settings in a file only the person who built "
                 "it knows, so nothing here can write the new password into it. The new server says it below; "
                 "put it in the site's own settings, and then point the DNS with number 6.")
OTHER_ITS_OWN = ("The new server made the database with the user and password you gave, so the settings that "
                 "came over with the site's files fit it as they are. Nothing is left to type in but the DNS, "
                 "with number 6.")
OTHER_LOGIN = ("The site's own settings hold a user name and a password for %s. Type them here and the new "
               "server makes exactly those, so the site works there without a line of it being changed. Press "
               "Enter at the password instead and the new server makes one up, which it says afterwards for "
               "you to put in the site's settings yourself.")
DB_USER_PROMPT = "The user the site logs in as [%s]: "
DB_PASSWORD_PROMPT = "The password of %s (Enter to have a new one made): "
OTHER_THERE = ("The files of %s and the database it was given are on the new server. f brings over what has "
               "changed here since; it asks for the database again, and reads it in over the one there.")


def _database_login(database, say=print, typed=None, secret=None):
    """The name and password the site logs in to its database with, asked of the person: they are in a file of
    the site's own that nothing here can find, and making the same ones on the new server is what lets the
    site work there untouched."""
    note(OTHER_LOGIN % database, say)
    user = ((typed or _typed)(DB_USER_PROMPT % database) or "").strip() or database
    password = (secret or _typed_password)(DB_PASSWORD_PROMPT % user)
    return user, (password.strip() if isinstance(password, str) and password.strip() else None)
OTHER_NO_DOCROOT = ("%s has no web root on this server that I can find, so there are no files to copy. Its "
                    "configuration is in the list above.")
DUMP_SUFFIX = ".sql"
PICK_DATABASE = "Number of the database of %s, its name, or Enter for none: "
NO_DATABASE_PICKED = "No database picked, so only the files of %s go over."
DUMP_FAILED = "The database %s was not dumped (%s), so nothing was copied."


def other_sites(row):
    """The sites of this customer that are neither WordPress, nor a redirect, nor a piSignage server: files to
    copy, and a database nobody here can name."""
    return [item for item in row.items["site"]
            if not item.get("wordpress") and not item.get("redirect") and not item.get("signage")]


def _database_choices(databases, sites, mail_database):
    """The databases to offer for a site, the likeliest first: the ones that belong to no site this tool knows
    come first, since a WordPress site's own database is already spoken for."""
    loose = loose_databases(sites, databases, mail_database)
    rest = [name for name in sorted(databases or {}) if name not in loose
            and name not in SYSTEM_DATABASES and name != SPAM_DATABASE]
    return loose + rest


def pick_database(name, databases, sites=None, mail_database=None, say=print, choose=None):
    """Which database belongs to this site, asked of the person: nothing in the files says it, and guessing it
    would copy somebody else's data over. Returns the name, or None for a site that has none."""
    choices = _database_choices(databases, sites, mail_database)
    if not choices:
        note("There is no database on this server to pick from, so only the files of %s go over." % name, say)
        return None
    heading("Which database does %s use?" % name, say)
    for number, found in enumerate(choices, 1):
        say("%4d  %-32s %s" % (number, _fit(found, 32), _size((databases or {}).get(found))))
    say("")
    typed = ((choose or _typed)(PICK_DATABASE % name) or "").strip()
    if not typed:
        note(NO_DATABASE_PICKED % name, say)
        return None
    if typed.isdigit() and 0 < int(typed) <= len(choices):
        return choices[int(typed) - 1]
    if typed in (databases or {}):
        return typed
    warn("There is no database called %s on this server. Nothing was copied." % typed, say)
    return None


def _dump_into(docroot, database, runner=None):
    """Writes the dump into the site's own directory, so it goes over with the files and is there afterwards.
    Returns the file's name. --single-transaction and --quick: the site is still in use, so no table may be
    locked and a big one is streamed row by row. The file is the owner's alone until it is read in on the new
    server, which moves it out of the web root; anything in a web root can be downloaded."""
    path = os.path.join(docroot, database + DUMP_SUFFIX)
    code = (runner or run_into)(path, "mysqldump", "--single-transaction", "--quick",
                                "--default-character-set=utf8mb4", database)
    if code != 0:
        raise CommandFailed("mysqldump stopped with code %d" % code)
    os.chmod(path, 0o600)
    return os.path.basename(path)


def copy_other_site(connection, item, databases, sites=None, mail_database=None, ask=None, choose=None,
                    say=print, run=run_command, runner=run_visible, dump=None, state=None, secret=None):
    """The 'f' on a customer's screen: brings a site that is not WordPress across. The new server makes the
    site, the files are copied into it, and the database the person picks is dumped into the site's own
    directory, goes over with the files and is read in there. Returns the steps done."""
    ask = ask or _question
    name, docroot = item.get("name"), item.get("docroot")
    heading("Copying %s to the new server" % name, say)
    say("")
    if not docroot or not os.path.isdir(docroot):
        fail(OTHER_NO_DOCROOT % name, say)
        return []
    note(OTHER_WARNING % name, say)
    say("")
    database = pick_database(name, databases, sites, mail_database, say=say, choose=choose)
    login = (None, None)
    if database:
        say("")
        login = _database_login(database, say=say, typed=choose, secret=secret)
    say("")
    if not _is_yes(ask("Copy the files of %s%s to the new server now?"
                       % (name, " and the database %s" % database if database else ""))):
        note("%s was left alone." % name, say)
        return []
    report = _Collected()
    sent = dict(item, database=database, wordpress=False)
    answer, unanswered = _apply_with_questions(connection, dict(sent, database=None), ask, report)
    if unanswered is not None or not answer.get("ok"):
        fail("The new server didn't make %s: %s" % (name, answer.get("error") or unanswered.get("question")), say)
        return []
    there = answer.get("docroot")
    steps = ["Made the site %s on the new server, its user %s and its directories." % (name, item.get("user"))]
    if database:
        try:
            dumped = (dump or _dump_into)(docroot, database)
        except (CommandFailed, OSError) as problem:
            fail(DUMP_FAILED % (database, getattr(problem, "message", problem)), say)
            return steps
        steps.append("Dumped the database %s into %s." % (database, os.path.join(docroot, dumped)))
    try:
        copy_files(connection, docroot, there, name, runner)
    except MoveFailed as problem:
        fail(problem.message, say)
        return steps
    steps.append("Copied the files of %s to %s." % (name, there))
    if database:
        made = _adopt_database(connection, sent, dumped, ask, say, login=login)
        if made is None:
            for step in steps:
                success(step, say)
            return steps
        steps.extend(made.get("steps") or [])
        for step in steps:
            success(step, say)
        say("")
        note(OTHER_ITS_OWN if made.get("its_own") else OTHER_BY_HAND, say)
        say("")
        say(_settings_block(made))
        return steps
    for step in steps:
        success(step, say)
    return steps


def copy_files(connection, here, there, name, runner=run_visible):
    """Copies what is in one directory on this server into one on the new server. Slashes on both ends, so the
    files land in that directory itself and not in a directory inside it."""
    if not isinstance(there, str) or not there:
        raise MoveFailed("The new server didn't say where the files of %s go, so nothing was copied." % name)
    shell = " ".join(shlex.quote(word) for word in ["ssh"] + connection.options())
    try:
        code = runner("rsync", "-az", "-s", "--info=progress2", "--human-readable", "--omit-dir-times",
                      "--no-owner", "--no-group", "-e", shell, "--rsync-path", "sudo -n rsync",
                      here.rstrip("/") + "/", "%s@%s:%s/" % (connection.user, connection.host, there.rstrip("/")))
    except CommandFailed as problem:
        raise MoveFailed("Copying the files of %s did not work: %s" % (name, problem.message))
    if code not in (0, 24):
        raise MoveFailed("Copying the files of %s did not work: rsync stopped with code %d (its own message is "
                         "above)." % (name, code))
    return code


def _adopt_database(connection, item, dumped, ask, say=print, login=(None, None)):
    """Has the new server make the database and read the dump that just arrived into it. The question about a
    database that is already there is asked here, as it is for a WordPress site."""
    tried = set()
    answers = {}
    while True:
        request = {"request": "adopt_database", "item": item, "dump": dumped}
        if login[0]:
            request["db_user"] = login[0]
        if login[1]:
            request["password"] = login[1]
        request.update(answers)
        try:
            answer = connection.ask(request, timeout=ADOPT_TIMEOUT)
        except Stopped as problem:
            warn(problem.message, say)
            return None
        if answer.get("ok"):
            return answer
        question = answer.get("ask")
        if not isinstance(question, dict):
            fail("The database of %s was not made: %s" % (item.get("name"), answer.get("error") or
                                                          "the new server gave no reason"), say)
            return None
        reply = (_database_question(question.get("question")) or "").strip().lower()
        if reply == "yes":
            answers["replace"] = True
            continue
        if reply in ("no", ""):
            note("The database of %s was left alone. Its files are across." % item.get("name"), say)
            return None
        if reply in tried:
            fail("The new server has a database called %s as well. Nothing was made." % reply, say)
            return None
        tried.add(reply)
        item = dict(item, new_database=reply)


def _settings_block(made):
    """What to put in the site's own settings, as four lines to read off the screen."""
    return "\n".join("  %-9s %s" % (word, made.get(key) or "")
                     for word, key in (("database", "database"), ("user", "user"),
                                       ("password", "password"), ("host", "host")))


class _Collected(object):
    """What _apply_with_questions puts aside while it asks; this flow reports its own steps."""

    def __init__(self):
        self.left_out = []

# ---- Moving piSignage servers ----------------------------------------------------
#
# A piSignage server (github.com/colloqi/pisignage-server) is a Node.js app on a port of its own, with Apache in
# front of it under a name like andre.milocast.nl. On the new server, domainctl signage gives each its own Linux
# user, port and database. What comes across is what its customer made: the uploaded media, its data folder
# (licences and player software) and its MongoDB database. The code is fetched fresh there.

PISIGNAGE = "pisignage-server"  # the name in its package.json, and its folder in the user's home on the new server
PISIGNAGE_MEDIA = "media"  # beside that folder, where piSignage keeps the uploads
PROC = "/proc"
_PROXY_TO = re.compile(r"^(?:https?|wss?)://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)(?:/|$)", re.I)
_LISTENING = re.compile(r":(\d+)\s.*\bpid=(\d+)")
_MONGO_URI = re.compile(r"""["'](mongodb(?:\+srv)?://[^"']+)["']""")
SIGNAGE_NO_NAME = ("The piSignage server in %s (port %d) isn't behind any site of Apache's, so it has no name to "
                   "move to. Set it up on the new server by hand: domainctl signage add DOMAIN.")
SIGNAGE_PROMPT = "Numbers of the ones to move (a for all of them; Enter for none): "
SIGNAGE_DNS_QUESTION = ("Point the DNS of %s at the new server now? Its players then talk to the new server once "
                        "the old record has expired. What changes here after this copy stays behind; running "
                        "this again before you say yes copies it across again.")


def _proxied_ports(sites_dir=SITES_ENABLED, notes=None):
    """{port: name}: the site of Apache's that sends its requests on to each port on this machine."""
    ports = {}
    for path in sorted(glob.glob(os.path.join(sites_dir, "*.conf"))):
        text = _read_config(path, notes)
        lines = text.split("\n") if text else []
        for number, line in enumerate(lines, 1):
            if not line.strip().lower().startswith("<virtualhost"):
                continue
            block = _vhost_block(lines, number)
            name = _vhost_directives(block)[0]
            for entry in block:
                words = [word.strip("\"'") for word in entry.split("#", 1)[0].split()]
                proxied = len(words) >= 3 and (words[0].lower() == "proxypass" or (
                    words[0].lower() == "rewriterule" and _PROXY_FLAG.search(entry)))
                found = _PROXY_TO.match(words[2]) if proxied else None
                if name and found:
                    ports.setdefault(int(found.group(1)), name)
    return ports


def _listeners(run):
    """{port: pid} of the programs that listen on TCP here."""
    found = {}
    for line in run("ss", "-ltnpH").split("\n"):
        match = _LISTENING.search(line)
        if match:
            found.setdefault(int(match.group(1)), int(match.group(2)))
    return found


def _environment(pid, proc=PROC):
    try:
        with open(os.path.join(proc, str(pid), "environ"), "rb") as handle:
            data = handle.read()
    except OSError:
        return {}
    return dict(item.decode("utf-8", "replace").split("=", 1) for item in data.split(b"\0") if b"=" in item)


def _pisignage_database(code, environment):
    """The address and name of the database a piSignage server uses: MONGOLAB_URI, which comes before its settings,
    or else the address in the settings of its NODE_ENV, development when none is set."""
    uri = environment.get("MONGOLAB_URI")
    if not uri:
        settings = _read_config(os.path.join(code, "config", "env",
                                             "%s.js" % (environment.get("NODE_ENV") or "development")))
        found = _MONGO_URI.search(settings or "")
        uri = found.group(1) if found else None
    if not uri:
        return None, None
    rest = uri.split("://", 1)[-1]
    name = rest.split("/", 1)[1].split("?", 1)[0] if "/" in rest else ""
    return uri, name or None


def signage_servers(run=run_command, sites_dir=SITES_ENABLED, proc=PROC, notes=None):
    """The piSignage servers running here, each a dict with its 'name' (the site Apache serves it under), 'port',
    'code' (its folder), 'media', 'data', 'uri' and 'database'. One that Apache doesn't serve under any name is
    left out, with a note."""
    notes = notes if notes is not None else []
    names = _proxied_ports(sites_dir, notes)
    found = []
    for port, pid in sorted(_listeners(run).items()):
        try:
            code = os.readlink(os.path.join(proc, str(pid), "cwd"))
            with open(os.path.join(code, "package.json"), encoding="utf-8") as handle:
                if json.load(handle).get("name") != PISIGNAGE:
                    continue
        except (OSError, ValueError, AttributeError):
            continue
        if port not in names:
            notes.append(SIGNAGE_NO_NAME % (code, port))
            continue
        uri, database = _pisignage_database(code, _environment(pid, proc))
        found.append({"name": names[port], "port": port, "code": code,
                      "media": os.path.normpath(os.path.join(code, "..", PISIGNAGE_MEDIA)),
                      "data": os.path.join(code, "data"), "uri": uri, "database": database})
    return found


def move_signage(connection, server, ask, run=run_command, runner=run_visible, piper=run_piped):
    """Brings one piSignage server across: domainctl signage add makes it on the new server, its media and data are
    copied into it, and its database replaces the new one's. With a yes from 'ask', its DNS moves too and it gets
    its certificate. Returns the steps done; raises MoveFailed, or Stopped when the new server can't be reached."""
    name = server["name"]
    if not server.get("database"):
        raise MoveFailed("I can't tell which database the piSignage server of %s uses, so nothing was moved." % name)
    user = site_user(name)
    code = runner(*connection.command("domainctl", "signage", "add", name, "--user", user, "--no-dns", "--yes"))
    if code != 0:
        raise MoveFailed("The new server didn't set up the piSignage server of %s (code %d; what it said is above)."
                         % (name, code))
    steps = ["Set up the piSignage server of %s on the new server, as %s." % (name, user)]
    try:
        home = run(*connection.command("getent", "passwd", user)).split(":")[5]
    except (CommandFailed, IndexError):
        raise MoveFailed("The new server made the piSignage server of %s, but didn't say where %s's home is, so "
                         "nothing was copied." % (name, user))
    shell = " ".join(shlex.quote(word) for word in ["ssh"] + connection.options())
    for source, target, what in ((server["media"], "%s/%s" % (home, PISIGNAGE_MEDIA), "media"),
                                 (server["data"], "%s/%s/data" % (home, PISIGNAGE), "data folder")):
        if not os.path.isdir(source):
            continue
        try:
            # As for a site's files: what is IN the folder lands in the one there. domainctl signage restore makes
            # it all the user's afterwards.
            copied = runner("rsync", "-az", "-s", "--info=progress2", "--human-readable", "--omit-dir-times",
                            "--no-owner", "--no-group", "-e", shell, "--rsync-path", "sudo -n rsync",
                            source.rstrip("/") + "/", "%s@%s:%s/" % (connection.user, connection.host, target))
        except CommandFailed as problem:
            raise MoveFailed("Copying the %s of %s did not work: %s" % (what, name, problem.message))
        if copied not in (0, 24):
            raise MoveFailed("Copying the %s of %s did not work: rsync stopped with code %d (its own message is "
                             "above)." % (what, name, copied))
        steps.append("Copied its %s to %s." % (what, target))
    dump = ["mongodump", "--quiet", "--uri=" + server["uri"], "--archive", "--gzip"]
    restore = connection.command("domainctl", "signage", "restore", user, "--from-database", server["database"],
                                 "--dump", "-", "--yes")
    try:
        dumped, restored = piper(dump, restore)
    except CommandFailed as problem:
        raise MoveFailed("The files of %s are across, but its database was not: %s." % (name, problem.message))
    if restored == 255 or (dumped < 0 and restored != 0):
        raise MoveFailed("The files of %s are across, but the connection to the new server was lost while its "
                         "database was sent. Move it again." % name)
    if dumped != 0:
        raise MoveFailed("The files of %s are across, but mongodump stopped with code %d, so its database was not "
                         "read in on the new server." % (name, dumped))
    if restored != 0:
        raise MoveFailed("The files of %s are across, but the new server did not read its database in (code %d)."
                         % (name, restored))
    steps.append("Read its database %s in on the new server." % server["database"])
    if not _is_yes(ask(SIGNAGE_DNS_QUESTION % name)):
        steps.append("Its DNS still points here. Move this one again to bring the new server up to date, and say "
                     "yes to the DNS then.")
        return steps
    code = runner(*connection.command("domainctl", "signage", "add", name, "--user", user, "--yes"))
    if code != 0:
        raise MoveFailed("The piSignage server of %s is across, but moving its DNS or getting its certificate did "
                         "not work (code %d; what the new server said is above). On the new server, run: domainctl "
                         "signage add %s" % (name, code, name))
    steps.append("Pointed the DNS of %s at the new server; it is served there over HTTPS." % name)
    return steps


SIGNAGE_NOT_RUNNING = ("%s is set up as a piSignage server, but no piSignage server is running here under "
                       "that name now, so there is nothing to copy. Start it and look again.")
SIGNAGE_QUESTION = ("Move the piSignage server of %s to the new server now? Its media, its data folder and its "
                    "database %s are copied into a server domainctl makes for it there."
                    " Nothing here is changed or deleted.")


def _signage_for(connection, row, doing=None, ask=None, run=run_command, say=print):
    """The 'g' on a customer's screen: moves this customer's piSignage server, which is the one part of a
    customer that is an application and not a website. The work itself is move_signage, the same as the
    'signage' command does."""
    doing = doing if doing is not None else [""]
    ask = ask or _question
    names = [item["name"] for item in signage_of(row)]
    notes = []
    servers = [one for one in signage_servers(run, notes=notes) if one["name"] in names]
    for text in notes:
        warn(text, say)
    for name in names:
        if name not in [one["name"] for one in servers]:
            warn(SIGNAGE_NOT_RUNNING % name, say)
    for server in servers:
        heading("Moving the piSignage server of %s" % server["name"], say)
        say("")
        if not _is_yes(ask(SIGNAGE_QUESTION % (server["name"], server["database"] or "(name unknown)"))):
            note("%s was left alone." % server["name"], say)
            continue
        doing[0] = "moving the piSignage server of %s" % server["name"]
        try:
            steps = move_signage(connection, server, ask, run)
        except MoveFailed as problem:
            fail(problem.message, say)
            continue
        for step in steps:
            success(step, say)


def _move_signage(connection, doing=None, ask=None, choose=None, run=run_command):
    """What `signage` does: lists the piSignage servers here and moves the ones picked. Returns the exit code."""
    doing = doing if doing is not None else [""]
    doing[0] = "looking for piSignage servers"
    notes = []
    servers = signage_servers(run, notes=notes)
    heading("piSignage servers")
    for text in notes:
        warn(text)
    if not servers:
        note("There is no piSignage server here to move.")
        return 0
    for number, server in enumerate(servers, 1):
        print("%4d  %s  (port %d, database %s)" % (number, server["name"], server["port"],
                                                   server["database"] or "unknown"))
    print()
    typed = (choose or _typed)(SIGNAGE_PROMPT).strip().lower()
    picked = servers if typed in ("a", "all") else \
        [servers[number - 1] for number in _numbers_in(typed) if 0 < number <= len(servers)]
    failed = 0
    for server in picked:
        doing[0] = "moving the piSignage server of %s" % server["name"]
        heading("Moving the piSignage server of %s" % server["name"])
        try:
            steps = move_signage(connection, server, ask or _question, run)
        except MoveFailed as problem:
            fail(problem.message)
            failed += 1
            continue
        for step in steps:
            success(step)
    return 1 if failed else 0


# ---- Running it ---------------------------------------------------------------

HELP_DESCRIPTION = """Moves the mail accounts, the WordPress sites and the cron jobs of this
server to a new one. The mail messages themselves are not moved.

How to use it:
  1. Copy this file and migratectl.py to the OLD server, for example from
     your own machine:
       scp migrate.py migratectl.py root@OLD-SERVER:
     With migratectl.py beside it, it puts that on the new server itself
     whenever the one there is another one, so nothing is copied there by
     hand.
  2. On the old server, as root, get the key:
       python3 migrate.py key
     Paste that line into /home/ansible/.ssh/authorized_keys on the new
     server (once).
  3. On the old server, as root, run it:
       python3 migrate.py
     It asks for the new server's name the first time and remembers it. It
     shows one row for each customer, with where its mail, its website and
     its cron jobs stand and what its next step is, and then stays up: a
     number does that customer's next step, a does the next step of every
     customer, "3 all" every step one customer has left, "i 3" says what one
     holds, "p 3" gives its accounts new passwords, s moves what SpamAssassin
     has learned about spam, r looks around again and q leaves.

The commands below do one thing each and stop, which is what a script wants;
each takes --help of its own. --no-colour leaves the colour off.

What it needs on the old server: Python 3.6 or newer, the ssh and ssh-keygen
programs and rsync. To find things it also uses the mysql or mariadb client,
mysqldump, doveadm, apache2ctl and crontab, when they are there; what it can't
look at it says so. The new server must be built with this repository.

It writes only these on the old server: its own key pair (migrate_key and
migrate_key.pub) and the known-hosts file beside it (migrate_known_hosts), in
the .ssh folder of the user running it, and its notes in
/var/lib/migrate/state.json."""

COMMANDS = (
    ("key", "print the key to paste on the new server, and where"),
    ("forget", "forget the new server's name, so it is asked for again"),
    ("check", "connect, and say which server answered and what it has"),
    ("list", "look around and show what can be moved (changes nothing)"),
    ("move", "do the next step of the customers the numbers name (asks "
     "which, with none given)"),
    ("again", "catch up what already moved: a site's files and database"),
    ("spam", "move what SpamAssassin learned about spam here, in place of "
     "what the new server learned itself"),
    ("signage", "move the piSignage servers here: their media, data and "
     "database, and their DNS when you say so"),
    ("report", "print the report of what has been done so far"),
)


def _options(parser, host=True, domain=True, user=True):
    """The options a command takes. Nothing given before the command is
    wiped by a default here (that is what SUPPRESS is for), so --no-colour
    and --host work in front of the command as well as behind it."""
    if host:
        parser.add_argument("--host", metavar="NEW", default=argparse.SUPPRESS,
                            help="the name or address of the new server")
    if domain:
        parser.add_argument("--domain", metavar="example.nl",
                            default=argparse.SUPPRESS,
                            help="look at only this mail domain and its sites")
    if user:
        parser.add_argument("--user", metavar="NAME",
                            default=argparse.SUPPRESS,
                            help="the Linux user to give the site on the new "
                            "server, instead of the name taken from its "
                            "domain; goes with --domain, for a site whose "
                            "name would clash with another one's")
    parser.add_argument("--no-colour", action="store_true", dest="no_colour",
                        default=argparse.SUPPRESS,
                        help="print no colour at all")


def _parser():
    parser = argparse.ArgumentParser(
        prog="migrate.py", description=HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", metavar="NEW",
                        help="the name or address of the new server")
    parser.add_argument("--no-colour", action="store_true", dest="no_colour",
                        help="print no colour at all")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND",
                                     title="commands")
    helps = dict(COMMANDS)
    commands.add_parser("key", help=helps["key"], description=helps["key"])
    commands.add_parser("forget", help=helps["forget"],
                        description=helps["forget"])
    every = commands.add_parser("report", help=helps["report"],
                                description=helps["report"])
    _options(every, domain=False, user=False)
    every.add_argument("--all", action="store_true", dest="everything",
                       help="also look around this server and add the lists "
                       "about all of it: what is not WordPress, databases and "
                       "jobs of no site, what could not be read")
    _options(commands.add_parser("check", help=helps["check"],
                                 description=helps["check"]),
             domain=False, user=False)
    _options(commands.add_parser("list", help=helps["list"],
                                 description=helps["list"]))
    _options(commands.add_parser("spam", help=helps["spam"],
                                 description=helps["spam"] + ". Asks first "
                                 "when the new server has learned anything."),
             domain=False, user=False)
    _options(commands.add_parser("signage", help=helps["signage"],
                                 description=helps["signage"] + ". Lists "
                                 "them and asks which."),
             domain=False, user=False)
    move = commands.add_parser("move", help=helps["move"],
                               description=helps["move"] + ". A number does "
                               "that customer's next step; numbers, ranges "
                               "like 2-5, commas, 'all' and '3 all' work.")
    move.add_argument("numbers", nargs="*", metavar="NUMBERS",
                      help="the numbers on the screen, like 1 3, 2-5, all, or "
                      "'3 all' for every step one customer has left")
    _options(move)
    again = commands.add_parser("again", help=helps["again"],
                                description=helps["again"] + ". Copies its "
                                "files again and replaces its database, after "
                                "asking.")
    again.add_argument("domain", metavar="DOMAIN",
                       help="the site that already moved")
    _options(again, domain=False)
    return parser


def _arguments(argv):
    args = _parser().parse_args(argv)
    args.again = args.command == "again"
    if not hasattr(args, "numbers"):
        args.numbers = []
    for name, value in (("host", None), ("domain", None), ("user", None),
                        ("everything", False), ("no_colour", False),
                        ("sizes", False)):
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


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


def _typed_line(prompt):
    """What was typed, or None at the end of the input: the live prompt must
    leave then, instead of asking the same thing for ever."""
    try:
        return input(prompt)
    except EOFError:
        print()
        return None


ANOTHER_NAME = "[y/n, or another name]"


def _database_question(text):
    """The question about a database that is already on the new server: yes empties it, no leaves the site
    alone, and a name gives the site a database of that name there instead. The name is checked here, so a
    name the new server would refuse never costs a round trip."""
    lines = textwrap.wrap(str(text).strip(), _WIDTH - 8)
    if lines[:-1]:
        print("\n".join(lines[:-1]))
    prompt = "%s %s " % (_paint(lines[-1] if lines else "Go on?", "bold", LOOK["colour"]),
                         _paint(ANOTHER_NAME, "cyan", LOOK["colour"]))
    while True:
        reply = _typed(prompt).strip()
        if reply.lower() in ("y", "yes"):
            return "yes"
        if reply.lower() in ("", "n", "no"):
            return "no"
        if len(reply) <= MAX_USER_NAME and _NEW_NAME.fullmatch(reply.lower()):
            return reply.lower()
        print("Type y to empty it, n to leave the site alone, or a name of at most %d lower case letters, "
              "digits, - and _, starting with a letter or a digit." % MAX_USER_NAME)


def _question(text):
    """Asks a question that is answered yes or no, and gives "yes" or "no". The choice goes at the end of its
    last line, as in mailctl, and Enter alone is no. Anything else is asked again."""
    lines = textwrap.wrap(re.sub(r"\s*(\[y/N\]|Type yes or no:?)\s*$", "", str(text).strip()), _WIDTH - 8)
    if lines[:-1]:
        print("\n".join(lines[:-1]))
    prompt = "%s %s " % (_paint(lines[-1] if lines else "Go on?", "bold", LOOK["colour"]),
                         _paint("[y/n]", "cyan", LOOK["colour"]))
    while True:
        reply = _typed(prompt).strip().lower()
        if reply in ("y", "yes"):
            return "yes"
        if reply in ("", "n", "no"):
            return "no"
        print("Type y for yes or n for no.")


# The old server's own mail and groupware addresses. The new server gives its own webmail and autodiscover, so
# these are not sites to move, and they are not listed as sites at all.
_PLUMBING_NAMES = ("webmail.", "autodiscover.", "autoconfig.", "mail.")
_PLUMBING_PLACES = ("/var/lib/roundcube", "/usr/share/z-push", "/usr/share/roundcube")


def is_plumbing(site):
    name = str(site.get("name") or "").lower()
    docroot = str(site.get("docroot") or "").rstrip("/") + "/"
    return name.startswith(_PLUMBING_NAMES) or any(
        docroot.startswith(place + "/") for place in _PLUMBING_PLACES)


def _look_around(args, notes, run=run_command):
    """(mail, mail_database, sites, databases, items) of this server. What
    could not be read goes into notes. Each step says what it is doing, and
    what it found."""
    domain = args.domain.lower() if args.domain else None
    wanted_user = getattr(args, "user", None)

    def look_at_mail():
        database = mail_database(run, notes)
        mail = mail_inventory(run, database, domain) if database else None
        if mail is not None:
            notes.extend(mail.notes)
        remember_webmail(webmail_folders(run, notes))
        return database, mail

    def mail_summary(found):
        mail = found[1]
        if mail is None:
            return "none found"
        text = "%s, %s" % (_plural(len(mail.file["domains"]), "domain", "domains"),
                           _plural(len(mail.file["addresses"]), "account", "accounts"))
        if _WEBMAIL_FOLDERS:
            text += " (and %s in webmail)" % _plural(sum(_WEBMAIL_FOLDERS.values()),
                                                     "address book or calendar", "address books and calendars")
        return text

    def look_at_sites():
        found = sites(run, notes)
        return ([s for s in found if not is_plumbing(s)],
                database_sizes(run, notes), [s for s in found if is_plumbing(s)])

    def site_summary(found):
        text = "%s, %d WordPress" % (_plural(len(found[0]), "site", "sites"),
                                     len([s for s in found[0] if s.get("wordpress")]))
        if found[2]:
            text += " (and %d webmail and similar, left out)" % len(found[2])
        return text

    database, mail = _step("Looking at the mail", look_at_mail, mail_summary)

    every, sizes, plumbing = _step("Looking at the websites", look_at_sites,
                                   site_summary)

    def look_at_jobs():
        return cron_jobs(run, every, notes)

    jobs = _step("Looking at the cron jobs", look_at_jobs,
                 lambda found: _plural(len(found), "job", "jobs"))
    found = every
    if domain:
        found = [s for s in found if _in_domain(s["name"], domain)
                 or any(_in_domain(a, domain) for a in s.get("aliases") or [])]
        names = set(s["name"] for s in found)
        jobs = [j for j in jobs if j.get("site") in names]
        plumbing = [s for s in plumbing if _in_domain(s["name"], domain)]
    items = _domain_items(mail) if mail else []
    minted = site_items(found, users=_user_for(found, domain, wanted_user))
    items += minted + cron_items(jobs, minted)
    items += [{"id": "aside:" + s["name"], "kind": "aside", "name": s["name"]}
              for s in plumbing]
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


# What the new server can answer from the two lists it reads once, `mailctl domain list` and `domainctl list`.
# A cron job costs it a read of a crontab each, so jobs are asked about one customer at a time instead, when
# that customer is looked into or moved.
CHEAP_KINDS = ("mail_domain", "site")


def _status_of(connection, items):
    answer = connection.ask({"request": "status", "items": [
        item for item in items if item.get("kind") != "aside"]})
    if not answer.get("ok") or not isinstance(answer.get("items"), dict):
        raise Stopped("The new server could not say what it already has: %s"
                      % (answer.get("error") or "no reason given"))
    return answer["items"]


def _choose(lines, prompt):
    typed = _typed(prompt)
    return pick(lines, typed)


def _to_do_again(items, state, status):
    """What a catch-up can be done for: a site this tool moved, whose place on the new server it knows and which
    the new server still has. Before the DNS is flipped the new server reports a site it made itself as
    'exists', which is what lets this be the catch-up before the flip that the design asks for."""
    return [item for item in items if item.get("kind") == "site"
            and state.entry(item).get("status") == "moved"
            and state.entry(item).get("docroot")
            and (status.get(item["id"]) or {}).get("state") == "exists"]


def _new_name(connection, hello):
    return (hello or {}).get("server") or getattr(connection, "host", None)


def _inspect(args, connection, notes, doing, hello):
    """Looks around this server and asks the new one what it has. Returns
    (mail, database, sites, databases, items, status)."""
    doing[0] = "looking around this server"
    mail, database, found, sizes, items = _look_around(args, notes)
    doing[0] = "asking the new server what it already has"
    name = _short_name(_new_name(connection, hello))
    asked = [item for item in items if item.get("kind") in CHEAP_KINDS]
    status = _step("Asking %s what it has" % name,
                   lambda: _status_of(connection, asked) if asked else {},
                   lambda said: "answered")
    _measure_mailboxes(customers(items, status), connection, doing)
    return mail, database, found, sizes, items, status


def _measure_mailboxes(rows, connection, doing=None):
    """Measures every customer's mailboxes once, here and on the new server, so the board can say which
    customer still has mail of its own. Without it the row of a customer whose accounts are across reads
    "nothing to move" while tens of gigabytes of its messages are still here, and the step it offers is to
    point the mail at a server that hasn't got them."""
    doing = doing if doing is not None else [""]
    # Only the customers whose mail is all on the new server. Theirs is the row that reads "nothing to move"
    # while their messages are still here; a customer whose accounts are not across yet has mail to move on
    # its row already, and its own screen measures its mailboxes when it is opened.
    waiting = [row for row in rows
               if row.accounts() and row.word("mail") == DONE and not sizes_remembered(row.name)]
    if not waiting:
        return
    doing[0] = "measuring the mailboxes"

    def measure():
        behind = 0
        for row in waiting:
            try:
                mail_sizes(row, connection)
            except Stopped:
                return None  # the new server can't be reached; the screens say so themselves
            behind += 1 if messages_behind(row.name) else 0
        return behind

    _step("Measuring the mailboxes of %s" % _plural(len(waiting), "customer", "customers"), measure,
          lambda found: "all across" if found == 0 else
          ("couldn't ask" if found is None else "%s still has mail here"
           % _plural(found, "customer", "customers")))


def _board(items, status, state, connection, hello, colour=None,
           rows=None, databases=None):
    """The screen: one row for each customer, and the next step for each."""
    return dashboard(items, status, state, rows=rows, databases=databases,
                     colour=_wanted_colour() if colour is None else colour,
                     old_server=socket.gethostname(),
                     new_server=_new_name(connection, hello),
                     addresses=(hello or {}).get("addresses"))


def _loose_of(looked):
    """The databases that belong to no site, from what looking around found."""
    _, database, found, sizes, _, _ = looked
    return loose_databases(found, sizes, database)


def _ask_about(connection, row, status, hello, done):
    """Asks the new server about one customer's own cron jobs, which the start of a run leaves alone. Once per
    customer in a run: asking again would read the same crontab again."""
    if row.name in done:
        return
    done.add(row.name)
    asking = [item for item in row.every() if item.get("kind") not in CHEAP_KINDS]
    if asking:
        _step("Asking %s about %s" % (_short_name(_new_name(connection, hello)),
                                      _fit(row.name, 24, True)),
              lambda: status.update(_status_of(connection, asking)),
              lambda said: "answered")


def _look_inside(connection, row, status, state, hello, done, colour=False):
    """What `i N` does: asks the new server about this customer's jobs, then says in sentences what the
    customer holds and what its number does next."""
    _ask_about(connection, row, status, hello, done)
    print()
    print(inside(row, status, state, colour=colour,
                 new_server=_new_name(connection, hello), mail=_mail_for(row, connection)))


def _open_customer(name, args, connection, state, notes, doing, hello, colour, asked, looked):
    """A number opens that customer: its mail, its website and its cron jobs, where each stands, and a short
    list of what can be done for it. Nothing moves until one of those is typed. Returns (looked, result,
    leave): 'looked' is None when something moved and the new server must be asked again, and 'leave' says q
    was typed."""
    result = None
    while True:
        if looked is None:
            del notes[:]
            looked = _inspect(args, connection, notes, doing, hello)
        items, status = looked[4], looked[5]
        row = next((one for one in customers(items, status, state) if one.name == name), None)
        if row is None:
            success("%s has nothing left for me to move." % name)
            return looked, result, False
        _ask_about(connection, row, status, hello, asked)
        dns = _dns_for(row, hello, doing)
        print()
        print(customer_screen(row, status, state, colour=colour,
                              new_server=_new_name(connection, hello), dns=dns,
                              mail=_mail_for(row, connection, doing), kept=kept_here(dns)))
        print()
        doing[0] = "waiting for you to say what to do for %s" % name
        typed = _typed_line(_paint(CUSTOMER_PROMPT % _fit(name, 40), "bold", colour) + " ")
        word = (typed or "").strip().lower()
        if typed is None or word in ("q", "quit"):
            return looked, result, True
        if word in ("", "b", "back"):
            return looked, result, False
        chosen, what, over = customer_action(row, word)
        if what is None:
            warn("I don't know '%s'. Type one of the numbers above, b to go back to the list, or q to leave."
                 % word)
            continue
        if what == "dns":
            _point_dns(connection, row, doing)
            forget_dns(row.name)  # it is the records themselves that moved
            quick_look([row], (hello or {}).get("addresses"))  # and the board's line about them with it
            status.update(_status_of(connection, row.every()))  # the new server has the site finished now
            continue
        if what == "repair":
            _repair(connection, row, doing)
            forget_dns(row.name)  # it publishes the mail records, so the row above them is out of date
            # Only the new server's side of this one customer changed. Reading this whole server again takes
            # a minute and says exactly what it said before.
            status.update(_status_of(connection, row.every()))
            continue
        if what == "other":
            for item in chosen:
                doing[0] = "copying %s to the new server" % item.get("name")
                if copy_other_site(connection, item, looked[3], looked[2], looked[1], ask=_question,
                                   state=state):
                    state.moved(item)  # it is across; the screen says so, and f again brings over changes
            status.update(_status_of(connection, row.every()))
            continue
        if what == "signage":
            _signage_for(connection, row, doing)
            forget_dns(row.name)  # moving it can move its DNS too
            quick_look([row], (hello or {}).get("addresses"))
            # The new server has the piSignage server now, so the row above the menu is out of date: without
            # this it keeps saying "not there yet" about the thing that just went across.
            status.update(_status_of(connection, row.every()))
            continue
        if what == "compare_mail":
            # It counts and says; nothing on either server changes, so nothing has to be asked again.
            _compare_mail(connection, row, hello, doing, database=looked[1])
            continue
        if what == "webmail":
            doing[0] = "copying the contacts and calendars"
            # Webmail's own tables on the new server change, and nothing this screen shows comes from them.
            copy_webmail(connection, row, ask=_question)
            continue
        if what == "copy_mail":
            # Nothing the new server has changes, so there is nothing to ask it again. A copy that really
            # started does make the mailboxes there grow, so then what was measured of them is thrown away.
            if _copy_mail(connection, row, hello, doing, database=looked[1]):
                forget_sizes(row.name)
            continue
        if what == "handover":
            # Nothing on the new server changes, so there is nothing to ask it again.
            _hand_over(connection, row, hello, doing)
            quick_look([row], (hello or {}).get("addresses"))
            continue
        if what == "passwords":
            doing[0] = "setting new mail passwords"
            set_passwords(connection, row, database=looked[1], ask=_question,
                          old_server=socket.gethostname(),
                          new_server=_new_name(connection, hello))
            continue
        if not chosen:
            note(why_nothing(row, word))
            continue
        if over:
            # The site is on the new server already: its files are copied over the ones there and its
            # database is replaced, each after the question do_again asks for it.
            doing[0] = "copying %s over what is there" % row.name
            extra = []
            result = do_again(connection, chosen, state, _question, extra)
            print()
            print(report(result, connection=connection, old_server=socket.gethostname(),
                         notes=notes, extra_notes=extra,
                         addresses=(hello or {}).get("addresses")))
            looked = None
            continue
        chosen = _confirmed(chosen, _typed, state)
        if not chosen:
            note("Nothing will be moved.")
            continue
        result = _move_and_report(connection, state, notes, looked, hello, chosen, doing)
        if result.interrupted:
            return looked, result, True
        looked = None  # what the new server has has changed


def _mail_for(row, connection, doing=None):
    """How much mail this customer's mailboxes hold here and on the new server, with a line of its own on the
    screen while they are measured: walking a mailbox of tens of gigabytes takes a moment. A measurement that
    is still remembered is used without a word."""
    doing = doing if doing is not None else [""]
    if not row.accounts() or sizes_remembered(row.name):
        return mail_sizes(row, connection)
    doing[0] = "measuring the mailboxes of %s" % row.name
    return _step("Measuring the mailboxes of %s" % row.name, lambda: mail_sizes(row, connection),
                 lambda found: _held(found[0]))


def _look_where(rows, hello, doing=None):
    """The quick look over every customer, with a line of its own: it is one lookup per name, but a server on
    the other side of the world can take a moment and there is one per customer."""
    doing = doing if doing is not None else [""]
    doing[0] = "looking up where the names of every customer point"
    _step("Looking up where the world is sent for each customer",
          lambda: quick_look(rows, (hello or {}).get("addresses")),
          lambda found: "%s still to do" % _plural(len(found), "customer", "customers") if found
          else "all of them point at the new server")


def _dns_for(row, hello, doing=None):
    """Where this customer's names point, with a line of its own on the screen while it is being looked up:
    name servers on the other side of the world can take a moment, and a screen that stops dead for them looks
    like a tool that has hung. An answer that is still remembered is used without a word."""
    doing = doing if doing is not None else [""]
    if dns_remembered(row.name):
        return dns_seen(row, hello)
    doing[0] = "looking up where the names of %s point" % row.name
    return _step("Looking up where the names of %s point" % row.name, lambda: dns_seen(row, hello),
                 lambda found: "done")


def _lay_of_the_land(found, sizes, database, items, notes):
    """The lists about the whole server that a move does not print: what is
    not WordPress, databases of no site, and what could not be read."""
    jobs = [item for item in items if _cron_by_hand(item)]
    _, others, loose, unread, rules = _inventory_lists(found, sizes, database, jobs, notes)
    out = []
    _section(out, "Sites that are not WordPress. They are not moved; copy "
             "them by hand:", _bullets(others))
    _section(out, "Parts of a redirect that can't be moved. Set these up on "
             "the new server by hand:", _bullets(rules))
    _section(out, "Databases that belong to no site. They are not moved:",
             _bullets(loose))
    _section(out, "What the server could not read while looking around:",
             _bullets(unread))
    return out[1:] if out and out[0] == "" else out


def _list(args, connection, state, notes, doing=None, hello=None):
    """Looks around and prints every item there is, with the number of the customer each belongs to. Changes
    nothing."""
    doing = doing if doing is not None else [""]
    mail, database, found, sizes, items, status = _inspect(
        args, connection, notes, doing, hello)
    print()
    print(items_board(items, status, state, detail=True,
                      numbers=_domain_numbers(customers(items, status, state)),
                      colour=_wanted_colour(args),
                      old_server=socket.gethostname(),
                      new_server=_new_name(connection, hello),
                      addresses=(hello or {}).get("addresses")))
    lines = _lay_of_the_land(found, sizes, database, items, notes)
    if lines:
        print("\n" + "\n".join(lines))


def _counts(accounts, forwards):
    parts = []
    if accounts:
        parts.append(_plural(accounts, "account", "accounts"))
    if forwards:
        parts.append(_plural(forwards, "forward", "forwards"))
    return " and ".join(parts) or "no accounts or forwards"


def _will_happen(item, state=None):
    """What moving this item will do, in a sentence."""
    kind = item.get("kind")
    if kind == "mail_domain":
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        return ("the domain with %s; their filters and spam settings come too"
                % _counts(len(data.get("addresses") or []), len(data.get("forwards") or [])))
    if kind == "site":
        size = _size(item.get("size"))
        if item.get("redirect"):
            return ("the site is made on the new server with its %s in its own .htaccess, and nothing is "
                    "copied" % _plural(len((item["redirect"].get("rules") or [])), "rule", "rules"))
        if state is not None and state.outstanding(item):
            return "made already; now its files and database are copied"
        return ("its files%s are copied to the new server, as the user %s"
                % (" and its database of %s" % size if size else " and its database"
                   if item.get("database") else "", item.get("user") or site_user(item["name"])))
    if kind == "cron":
        command = _cron_parts_of(item)[1]
        return "%s: %s, goes into the crontab of %s on the new server" % (
            schedule_in_words(_cron_parts_of(item)[0]), _fit_left(command, 40),
            item.get("user") or item.get("old_user") or "its user")
    return "moved"


GO_AHEAD = "[y/n, or the numbers of the ones to do]"


def _confirmed(chosen, ask, state=None, numbers=None):
    """Says what each picked item is and what will happen to it, then asks. Returns what to go ahead with: all
    of it for a yes, or only the ones whose numbers were typed. Empty for no.

    One customer can hold more than one website, and a number that moves 'everything that is left' would take
    them all. Saying which is the only way to leave one behind without doing the rest by hand.
    """
    print("You picked:")
    for place, item in enumerate(chosen, 1):
        text = "%d. %s %s: %s" % (place, KIND_NAMES.get(item.get("kind"), "Other"),
                                  _fit(_title(item), 40, True), _will_happen(item, state))
        print("\n".join(textwrap.wrap(text, _WIDTH, subsequent_indent="   ")))
    answer = (ask("%s %s " % (_paint("Go ahead?", "bold", LOOK["colour"]),
                              _paint(GO_AHEAD, "cyan", LOOK["colour"]))) or "").strip()
    if _is_yes(answer):
        return list(chosen)
    wanted = [number for number in _numbers_in(answer) if 1 <= number <= len(chosen)]
    return [chosen[number - 1] for number in sorted(set(wanted))]


def _numbers_in(answer):
    """The numbers in what was typed, however they are written apart: "1 3", "1,3" or "1, 3"."""
    return [int(word) for word in re.findall(r"[0-9]+", answer)]


PICK_PROMPT = 'Numbers to do their next step (a for all of them; Enter for none): '
LIVE_PROMPT = ("Type a customer's number to open it, a to do the next step of every "
               "customer, t for what is still to do, s to move the spam learning, q to leave: ")
LIVE_HELP = ('A number opens that customer and asks what to do for it: its mail, its '
             'website, its cron jobs, everything that is left, or giving an account its own '
             'password on the new server. '
             'a does the next step of every customer, t lists what is still to do over all of them '
             '(the messages, the redirects, the mail this server still delivers itself and the names that '
             'still point here), s moves what SpamAssassin has learned about '
             'spam here to the new server, r looks around again, q leaves. '
             '"3 all" does everything one customer has left without opening it.')


def _live_prompt(colour):
    """The prompt as it is printed: wrapped to the width, like everything else."""
    return "\n".join(_paint(line, "bold", colour)
                     for line in textwrap.wrap(LIVE_PROMPT, _WIDTH)) + " "


def _move_and_report(connection, state, notes, looked, hello, chosen,
                     doing=None, passwords=None):
    """Moves what was picked and prints the report of that move alone.
    'looked' is what _inspect found. Returns the Report."""
    doing = doing if doing is not None else [""]
    mail, database, found, sizes, items, status = looked
    extra = []
    doing[0] = "moving what you picked"
    move = mover(connection, state, _question, extra)
    result = run(connection, chosen, _question, move=move,
                 notes=state, status=status, php=(hello or {}).get("php"),
                 wp=(hello or {}).get("wp"), extra_notes=extra)
    moved_domains = [i["name"] for i, _ in result.moved
                     if i.get("kind") == "mail_domain"]
    print()
    doing[0] = "writing the report"
    print(report(result, connection=connection,
                 old_server=socket.gethostname(), found_sites=found,
                 mail=_left_out_of(mail, moved_domains), databases=sizes,
                 items=items, notes=notes, extra_notes=extra,
                 mail_database=database, passwords=passwords,
                 addresses=(hello or {}).get("addresses")))
    if moved_domains:
        # The domain is on the new server now, and this server still has it in its own tables: the one thing
        # left to do with it is to stop keeping it here, so it is offered here and not only on a menu.
        _offer_handover(connection, moved_domains, hello, doing)
    return result


def _again(args, connection, state, notes, looked, doing, hello):
    """`again`: copies the files of a site that already moved and replaces
    its database, each after a question."""
    mail, database, found, sizes, items, status = looked
    extra = []
    chosen = _to_do_again(items, state, status)
    if not chosen:
        _say("Nothing that moved from here is still on the new server, so "
             "there is nothing to copy again. A site the new server no "
             "longer has is offered on the ordinary list instead.")
    else:
        print("What already moved from here:")
        for number, item in enumerate(chosen, 1):
            print("%3d  %s %s" % (number, KIND_NAMES.get(item.get("kind"), "Item"), _title(item)))
        chosen = _choose(chosen, "Numbers to copy again (like 1 3, 2-5, "
                         "all; Enter for none): ")
    result = do_again(connection, chosen, state, _question, extra)
    print()
    doing[0] = "writing the report"
    print(report(result, connection=connection,
                 old_server=socket.gethostname(), found_sites=found,
                 mail=None, databases=sizes, items=items, notes=notes,
                 extra_notes=extra, mail_database=database,
                 addresses=(hello or {}).get("addresses")))
    return result


def _work(args, connection, state, notes, doing=None, hello=None):
    """Does one move after the connection works and returns its Report.
    'doing' is a list whose one place always holds what is being done.
    'hello' is what the new server answered when it was first asked: which
    PHP and wp-cli it has, for the cron jobs, and its own addresses, for the
    DNS records in the report."""
    doing = doing if doing is not None else [""]
    colour = _wanted_colour(args)
    looked = _inspect(args, connection, notes, doing, hello)
    items, status = looked[4], looked[5]
    print()
    if args.again:
        return _again(args, connection, state, notes, looked, doing, hello)
    rows = customers(items, status, state)
    typed = " ".join(getattr(args, "numbers", None) or [])
    if not typed:
        print(_board(items, status, state, connection, hello, colour=colour,
                     rows=rows, databases=_loose_of(looked)))
        if rows:
            typed = _typed(PICK_PROMPT)
    chosen = picked(rows, typed) if typed.strip() else Typed()
    done = set()
    for row in chosen.look:
        _look_inside(connection, row, status, state, hello, done, colour)
    passwords = []
    for row in chosen.passwords:
        passwords.extend(set_passwords(
                connection, row, database=looked[1], ask=_question,
                old_server=socket.gethostname(),
                new_server=_new_name(connection, hello)))
    moving = set(id(item) for item in chosen.move)
    for row in rows:
        if any(id(item) in moving for item in row.every()):
            _ask_about(connection, row, status, hello, done)
    if chosen.move:
        chosen.move = _confirmed(chosen.move, _typed, state)
        if not chosen.move:
            print("Nothing will be moved.")
    return _move_and_report(connection, state, notes, looked, hello,
                            chosen.move, doing, passwords)


def _live(args, connection, state, notes, doing=None, hello=None):
    """The tool as people use it, with no subcommand: the screen with one row
    for each customer, and then a prompt that stays up. A number does that
    customer's next step, a does the next step of every customer in turn,
    "i 3" looks inside one, "p 3" gives one's accounts new passwords, r looks
    around again, q or the end of the input leaves. The report of each move is
    printed; after a move it looks around again, so what is on the screen is
    what the new server really has by then. The screen is drawn again only
    when it has changed, so an answer to the prompt does not push it off the
    top. Returns the last Report, or None when nothing was moved."""
    doing = doing if doing is not None else [""]
    colour = _wanted_colour(args)
    result, looked, asked, passwords, draw, fresh = None, None, set(), [], True, False
    while True:
        if looked is None:
            del notes[:]
            looked = _inspect(args, connection, notes, doing, hello)
            asked, draw, fresh = set(), True, True
        items, status = looked[4], looked[5]
        rows = customers(items, status, state)
        if fresh:
            # Where the world is sent for each customer, once per look around: a customer whose parts are all
            # across still has this to do, and the board would call it finished.
            _look_where(rows, hello, doing)
            fresh = False
        if draw:
            print()
            print(_board(items, status, state, connection, hello,
                         colour=colour, rows=rows, databases=_loose_of(looked)))
        draw = True
        print()
        doing[0] = "waiting for you to say what to do"
        typed = _typed_line(_live_prompt(colour))
        word = (typed or "").strip().lower()
        if typed is None or word in ("q", "quit"):
            return result
        if word == "r":
            forget_all_sizes()  # the one way to make it measure the mailboxes again
            looked = None
            continue
        if word == "s":
            _move_spam(connection, doing)
            draw = False  # no customer changed: the screen stays where it is
            continue
        if word == "t":
            _todo(connection, rows, status, hello, doing, colour)
            draw = False  # it is a list to read and act on: the board would push it off the top
            continue
        if not word or word in ("?", "h", "help"):
            _say(LIVE_HELP)
            draw = False
            continue
        if re.fullmatch(r"[0-9]+", word):
            # A number opens that customer and asks what to do for it. It never starts moving by itself.
            one = next((row for row in rows if row.number == int(word)), None)
            if one is None:
                _say("There is no customer %s on the list." % word)
                draw = False
                continue
            looked, opened, leave = _open_customer(
                one.name, args, connection, state, notes, doing, hello, colour, asked, looked)
            result = opened or result
            if leave:
                return result
            continue
        chosen = picked(rows, typed)
        for row in chosen.look:
            _look_inside(connection, row, status, state, hello, asked, colour)
        for row in chosen.passwords:
            doing[0] = "setting new mail passwords"
            passwords.extend(set_passwords(
                connection, row, database=looked[1], ask=_question,
                old_server=socket.gethostname(),
                new_server=_new_name(connection, hello)))
        if not chosen.move:
            draw = False  # the screen says the same as it did: it stays where it is
            continue
        # A job is only asked about one customer at a time, so the jobs of
        # what is about to move are asked about now: a job already there is
        # not sent again.
        moving = set(id(item) for item in chosen.move)
        for row in rows:
            if any(id(item) in moving for item in row.every()):
                _ask_about(connection, row, status, hello, asked)
        result = _move_and_report(connection, state, notes, looked, hello,
                                  chosen.move, doing, passwords)
        if result.interrupted:
            return result
        looked = None  # what the new server has has changed: look again


def _show_check(hello, host, colour):
    """What the new server said about itself."""
    tools = hello.get("tools") if isinstance(hello.get("tools"), dict) else {}
    print("%s answered." % _paint(str(hello.get("server") or host), "bold", colour))
    addresses = [a for a in hello.get("addresses") or [] if isinstance(a, str)]
    if addresses:
        print("Addresses:  %s" % ", ".join(addresses))
    print("This file and the new server speak the same release (%s)."
          % hello.get("version"))
    for name in ("mailctl", "domainctl"):
        have = tools.get(name)
        print("%-10s  %s" % (name, _paint("there", "green", colour) if have
                             else _paint("NOT there", "red", colour)
                             + " (the new server is not built with this repository)"))
    for name, what in (("php", "PHP"), ("wp", "wp-cli")):
        print("%-10s  %s" % (what, hello.get(name) or "not found"))



def _result_from_notes(state):
    """A Report of what the notes say was done: no looking around, no new
    server. A site that was made and not filled is files still to copy."""
    kinds = {"mail": "mail_domain", "site": "site", "cron": "cron"}
    result = Report()
    for key in sorted(state.items):
        word, _, name = key.partition(":")
        if word not in kinds or not name:
            continue
        item = {"id": key, "kind": kinds[word],
                "name": name if word != "cron" else "job " + name}
        status = state.items[key].get("status")
        if status == "moved":
            result.moved.append((item, {"docroot": state.items[key].get("docroot")}))
        elif status == "made":
            result.files_to_copy.append(item)
    return result


def _try_connecting(host):
    """A Connection to the new server when it can be reached, else None: the
    report then says what to run by hand."""
    if not host:
        return None
    try:
        connection = Connection(host)
        check(connection)
        return connection
    except Exception:
        return None


def _check_arguments(args):
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


def _remember(state, host, say=print):
    """Writes the new server's name down, so it is asked for only once. When
    the notes can't be written it says so and carries on: the next run asks
    again."""
    if host == getattr(state, "server", None):
        return
    state.server = host
    try:
        state.save()
    except MoveFailed as problem:
        _say("%s For now I carry on without it written down, so I ask for "
             "the new server again next time." % problem.message, say)


def _server_name(args, state):
    """The new server: what --host says, else what the notes remember, else
    what is typed in. Whatever it comes to is written down, so the next run
    does not ask."""
    host = args.host or getattr(state, "server", None)
    if not host:
        host = _typed("Name or address of the new server: ").strip()
    if not host:
        raise Stopped("I need the name or address of the new server: run me "
                      "again with --host NEW-SERVER.")
    _remember(state, host)
    return host


def _forget(state):
    """Drops the new server's name from the notes."""
    name = getattr(state, "server", None)
    if not name:
        print("I have no new server written down, so there is nothing to "
              "forget.")
        return 0
    state.server = None
    try:
        state.save()
    except MoveFailed as problem:
        print(problem.message)
        return 1
    print("I have forgotten %s. The next run asks for the new server again."
          % name)
    return 0


def main(argv=None, geteuid=os.geteuid):
    args = _arguments(argv)
    colour = _wanted_colour(args)
    set_look(colour)
    if geteuid() != 0:
        print(_paint("Run me as root: I read other users' cron jobs, mail "
                     "settings and websites.", "red", colour))
        return 1
    doing = ["starting"]
    _SIZES_SEEN.update(_read_sizes())  # what an earlier run measured, so this one needn't walk it all again
    try:
        _check_arguments(args)
        if args.command == "report":
            state = State()
            if args.host:
                _remember(state, args.host)
            host = args.host or getattr(state, "server", None)
            connection = _try_connecting(host)
            result = _result_from_notes(state)
            if not args.everything:
                print(report(result, connection=connection, new_server=host))
                return 0
            notes = []
            _, database, found, sizes, items = _look_around(args, notes)
            print()
            print(report(result, connection=connection, new_server=host,
                         found_sites=found, databases=sizes, items=items,
                         notes=notes, mail_database=database, everything=True))
            return 0
        if args.command == "key":
            doing[0] = "making the key"
            print("The new server must have this key (one line) in\n%s:\n\n%s\n"
                  % (AUTHORIZED_KEYS, own_key()))
            return 0
        state = State()
        if args.command == "forget":
            return _forget(state)
        doing[0] = "asking which server to move to"
        host = _server_name(args, state)
        doing[0] = "making the key"
        own_key()
        doing[0] = "connecting to the new server"
        connection = Connection(host)
        hello = _step("Connecting to %s" % host, lambda: check(connection),
                      lambda said: "answered")
        print()
        if args.command == "check":
            _show_check(hello, host, colour)
            return 0
        if args.command == "list":
            _list(args, connection, state, [], doing, hello)
            return 0
        if args.command == "spam":
            return _move_spam(connection, doing)
        if args.command == "signage":
            return _move_signage(connection, doing)
        work = _live if args.command is None else _work
        result = work(args, connection, state, [], doing, hello)
        if result is not None and result.interrupted:
            print("You stopped the run; the report above says what moved.")
            return 1
    except Stopped as problem:
        print(_paint(problem.message, "red", colour))
        return 1
    except KeyboardInterrupt:
        print(_paint("\nStopped by you while %s. What was already moved stays "
                     "moved; run me again to carry on." % doing[0], "red", colour))
        return 1
    except Exception as problem:
        print(_paint("\n".join(textwrap.wrap(
            "Stopped by an error while %s: %s: %s. What was already moved "
            "stays moved; run me again." % (doing[0], type(problem).__name__,
                                           problem), 79)), "red", colour))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
