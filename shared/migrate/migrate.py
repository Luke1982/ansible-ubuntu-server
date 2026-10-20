#!/usr/bin/env python3
"""Moves what is on this server to a new one. Copy this one file to the old server and run it there.

It only reads: it changes nothing on this server. Standard library only and nothing newer than Python 3.6, since an
old server has an old Python and none of our other files.
"""

import copy
import getpass
import glob
import json
import os
import pwd
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
SPAM_RENAMED = {"whitelist_from": "welcomelist_from", "blacklist_from": "blocklist_from"}  # SpamAssassin before 4
DOVEADM_HEADER = "sieve script:\n"
SITES_ENABLED = "/etc/apache2/sites-enabled"
APACHE_ROOT = "/etc/apache2"
PROTOCOL_VERSION = 1  # of the talk with migratectl on the new server; both halves must come from the same release
KEY_NAME = "migrate_key"
KNOWN_HOSTS_NAME = "migrate_known_hosts"
AUTHORIZED_KEYS = "/home/ansible/.ssh/authorized_keys"


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
    inventory["forwards"] = _forwards(rows(
        "SELECT source, destination FROM %s.virtual_aliases WHERE LOWER(source) <> LOWER(destination) %s "
        "ORDER BY source, destination" % (database, where["aliases"]), "the forwards"), senders, left_out)

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
    return {"user": user, "line": line, "site": _site_of(command or "", sites), "interpreter": interpreter,
            "settings": settings, "problem": problem}


def cron_jobs(run, sites, notes=None, passwd=PASSWD, cron_dir=CRON_DIR):
    """One dict per cron job on the server: user, line (as written), settings (the NAME=value lines above it),
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


def own_key(directory=None, run=run):
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
                handle.write(run("ssh-keygen", "-y", "-f", private).strip() + " migrate\n")
        with open(private + ".pub") as handle:
            return handle.read().strip()
    except (OSError, CommandFailed) as problem:
        raise Stopped("I couldn't make the key this server uses to reach the new one in %s: %s. Check that this "
                      "account may write there and try again." % (directory, getattr(problem, "message", problem)))


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
        self.old_ssh_said = False

    def _command(self, host_check):
        # BatchMode: ssh must never wait for a password, a passphrase or a yes/no that nobody would see.
        return ["ssh", "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", "-o", "UserKnownHostsFile=" + self.known_hosts,
                "-o", "StrictHostKeyChecking=" + host_check, "%s@%s" % (self.user, self.host),
                "sudo", "-n", "migratectl"]

    def _once(self, host_check, line):
        try:
            process = subprocess.Popen(self._command(host_check), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, universal_newlines=True)
        except FileNotFoundError:
            raise Stopped("ssh isn't installed on this server, so I can't reach the new one. Install the OpenSSH "
                          "client (on Debian or Ubuntu: apt install openssh-client) and run me again.")
        except OSError as problem:
            raise Stopped("I couldn't start ssh to reach the new server: %s." % problem)
        out, err = process.communicate(line)
        return process.returncode, out or "", " ".join((err or "").split())

    def ask(self, request):
        """The new server's answer to one request, as a dict. Raises Stopped, and nothing else, when it can't."""
        try:
            line = json.dumps(request) + "\n"
        except (TypeError, ValueError):
            raise Stopped("I made a request that can't be sent. That's a mistake in this tool, not in your servers.")
        code, out, err = self._once("accept-new", line)
        if code != 0 and re.search(r"bad configuration option|unsupported option|accept-new", err, re.I):
            if not self.old_ssh_said:
                self.old_ssh_said = True
                print("Note: the ssh on this server is too old to remember the new server's identity, so I accepted "
                      "it without checking. Make sure %s really is your new server." % self.host)
            code, out, err = self._once("no", line)
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
            raise Stopped("The connection to %s stopped with an error (code %d).%s" % (self.host, code, said))
        if not out.strip():
            raise Stopped("The new server answered with nothing. Is migratectl on it the right program?%s" % said)
        try:
            answer = json.loads(out)
        except (ValueError, RecursionError):
            answer = None
        if not isinstance(answer, dict):
            raise Stopped("The new server answered with something I can't read, so I stopped. Both servers should "
                          "come from the same release of this repository.%s" % said)
        return answer


def check(connection):
    """The new server's answer to hello. Raises Stopped when it can't be reached, doesn't accept our key, or speaks
    another version than this file."""
    try:
        answer = connection.ask({"request": "hello"})
    except Stopped as problem:
        if not problem.refused:
            raise
        try:
            key = own_key(os.path.dirname(connection.key))
        except Stopped:
            key = "(I couldn't read the key; run me again to make it)"
        raise Stopped("%s Add this key, as one line, to %s on the new server (%s@%s), then run me again:\n\n%s\n"
                      % (problem.message.strip(), AUTHORIZED_KEYS, connection.user, connection.host, key))
    if not answer.get("ok"):
        raise Stopped("The new server wouldn't say hello: %s" % (answer.get("error") or "no reason given"))
    if answer.get("version") != PROTOCOL_VERSION:
        raise Stopped("This file speaks version %s of the talk with the new server, but the new server speaks "
                      "version %s. Both halves must come from the same release of this repository: copy this file "
                      "again from the release the new server was built with."
                      % (PROTOCOL_VERSION, answer.get("version")))
    return answer


def apply_item(connection, item, ask_password=None, say=print):
    """Sends one item to the new server to be made. Returns (answer, skipped). When the answer says some mail
    accounts need a password the new server can't get from the stored one, each is asked for here (nothing shows on
    screen), and the same item is sent again with it. Enter skips an account: it is left out and named in 'skipped'.
    A password goes nowhere but the one request: not into the state file, a command line or a message."""
    ask_password = ask_password or getpass.getpass
    item = copy.deepcopy(item)
    skipped, asked = [], set()
    answer = connection.ask({"request": "apply", "item": item})
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
                data["sieve"] = [entry for entry in data.get("sieve", []) if not (
                    isinstance(entry, dict) and str(entry.get("address", "")).lower() == address.lower())]
            password = None
        answer = connection.ask({"request": "apply", "item": item})
    return answer, skipped
