# Design: `mailctl`, a management CLI for the mail servers

Date: 2026-09-14

## Goal

One command-line tool on every mail server to manage mail domains, addresses, forwards, DKIM keys and spam settings, and to look up an account's or domain's status. It replaces the two shell helpers `create_email_address.sh` and `setup_dkim_for_domain.sh`, and fixes the playbook wiping DKIM signing on every run.

The tool is polished: rich help text with an example for every command, colors, tables, and prompts for anything not given on the command line (passwords hidden and asked twice). Everything can also be passed as arguments and flags, so Ansible and scripts can use it without prompts.

It must work on both supported releases: Ubuntu 24.04 (Dovecot 2.3, MariaDB 10.11, Python 3.12) and Ubuntu 26.04 (Dovecot 2.4, MariaDB 11.8, Python 3.14).

---

## Commands

Running `mailctl` without arguments shows the command overview. `--help` on any command shows its options and an example. Arguments in brackets are asked for when left out.

| Command | What it does |
|---|---|
| `domain add [DOMAIN]` | Adds the domain, creates its DKIM key, updates the OpenDKIM tables, and prints the DNS records to publish (MX, SPF, DKIM, DMARC) |
| `domain list` | Domains with number of addresses, number of forwards, disk usage and whether a DKIM key exists |
| `domain delete [DOMAIN]` | Shows what goes along (addresses and forwards), asks to type the domain to confirm, and whether to delete stored mail. Removes the DKIM key |
| `address add [ADDRESS]` | Asks for the password, creates the account, its sender permission and its Maildir folders. Offers to add a missing domain |
| `address list [DOMAIN]` | Addresses with disk usage and last IMAP login |
| `address password [ADDRESS]` | Sets a new password |
| `address delete [ADDRESS]` | Asks for confirmation and whether to delete stored mail; logs out active sessions |
| `forward add [SOURCE] [DESTINATION]` | Adds a forward. When the destination is an account on this server, asks whether it may send as the source |
| `forward list [DOMAIN]` | Forwards, with whether the destination may send as the source |
| `forward delete [SOURCE] [DESTINATION]` | Removes a forward and the matching send-as permission |
| `dkim show [DOMAIN]` | The DKIM DNS record for the domain |
| `dkim create [DOMAIN]` | Creates a key if the domain has none (never replaces one) and updates the OpenDKIM tables. Used by Ansible |
| `status [ADDRESS_OR_DOMAIN]` | Address: folders with message counts and sizes, last logins, sending against the limits, recent bounces. Domain: DNS checklist, address and forward count, disk usage |
| `spam show [TARGET]` | Spam settings that apply and where each is set (address, domain, whole server) |
| `spam set [TARGET] [SETTING] [VALUE]` | Sets a spam setting for an address, a domain or `server` |
| `spam unset [TARGET] [SETTING] [VALUE]` | Removes a setting, or one value of a list setting |
| `filters show [ADDRESS]` | The account's Sieve scripts (active one marked) and the server-wide scripts that run after them |

Non-interactive flags:

- `--yes` / `-y` on `domain delete`, `address delete` and `spam unset`: confirm without asking.
- `--password-stdin` on `address add` and `address password`: read the password from standard input when it isn't a terminal. Passwords are never accepted as arguments, because arguments show up in the process list and shell history. In a terminal the password is always asked for, hidden.
- `--delete-mail` / `--keep-mail` on the delete commands (only asked when there is stored mail), `--send-as` / `--no-send-as` on `forward add`.

When a value or a decision is missing and standard input is not a terminal, the command fails with a message naming the argument or the flags to use.

Commands ask everything they need before they change anything.

---

## Installation (Ansible, `mail` role)

The role starts by checking that the Ubuntu release is one it knows (`dovecot_versions` in the role defaults). Upgrading a mail server from 24.04 to 26.04 in place isn't supported: the package upgrade keeps the Dovecot config files the role changed, and Dovecot 2.4 doesn't accept them (see the README).

New task file `roles/mail/tasks/install-mailctl.yml`, included from `main.yml` before `configure-opendkim.yml`:

1. Install `python3-typer`, `python3-rich` and `python3-dnspython` (`python3-pymysql` is already installed).
2. Copy the Python package's `.py` files from `roles/mail/files/mailctl/mailctl/` to `/usr/local/lib/mailctl/mailctl/` (`mailctl_lib` in the role defaults), with a filetree loop, so `__pycache__` from local test runs is never deployed. Files removed from the repository stay on the server; nothing imports them.
3. Install `/usr/local/bin/mailctl`, a shell wrapper: `PYTHONPATH=/usr/local/lib/mailctl exec /usr/bin/python3 -P -m mailctl "$@"`. `-P` keeps the current directory off the module path.
4. Write `/etc/mailctl/config.json` (mode 0644) from role variables:

```json
{
  "hostname": "web02.xldiscounter.com",
  "send_limits": [{"recipients": 300, "seconds": 3600}, {"recipients": 1000, "seconds": 86400}],
  "send_limits_by_account": {}
}
```

`send_limits` and `send_limits_by_account` are the same variables that generate the postfwd rules. Any other `Config` field (system paths, service users) can be set in the file too; the tests use that.

The tool runs as root. It connects to MariaDB as root over the unix socket (`/run/mysqld/mysqld.sock`), like the Ansible `mysql_*` tasks, so it needs no password of its own.

### Changed and removed

- `roles/mail/files/create_email_address.sh` is removed (not deployed by Ansible; replaced by `mailctl address add`).
- `roles/mail/files/setup_dkim_for_domain.sh` and its copy task are removed; a task removes `/usr/local/bin/setup_dkim_for_domain.sh` from servers. The script also added `*.DOMAIN` to OpenDKIM's TrustedHosts; the template no longer gets that line back. Mail from logged-in users is signed without it.
- The `KeyTable` and `SigningTable` templates and tasks are removed (see DKIM below). `TrustedHosts` stays a template.
- Changing `/etc/opendkim.conf`, `/etc/default/opendkim` or TrustedHosts now restarts OpenDKIM (it never did).
- SpamAssassin's user preference query orders the rows explicitly (see Spam settings).

---

## Code structure

```
roles/mail/files/mailctl/
  mailctl/
    __main__.py          # python3 -m mailctl
    cli.py               # root Typer app; a group class turns MailctlError into a message and exit status 1
    session.py           # open_session(): root check, config, a database connection opened on first use
    ui.py                # console, text(), prompts, confirmations, messages, tables, DNS records, formatting
    commands/            # one Typer sub-app per command group: arguments, prompts, rendering
      shared.py          # shared arguments and options, ask_domain/ask_address/ask_target, domain_filter,
                         # decide_mail, attempt, warn_about_incoming_forwards
      domain.py address.py forward.py dkim.py status.py spam.py filters.py
    core/                # no printing, no prompting; everything here is testable without a terminal
      errors.py          # MailctlError(message, hint)
      config.py          # Config dataclass: config.json values plus system paths and users
      db.py              # Database wrapper around PyMySQL: rows/row/value/execute, transaction()
      names.py           # validation and normalisation of domains and addresses
      system.py          # run() for external commands, root check, service reload, remove_tree, server IPs
      domains.py         # virtual_domains
      addresses.py       # virtual_users and password hashing
      senders.py         # virtual_sender_aliases: who may send as which address
      forwards.py        # virtual_aliases, including the forward that keeps a forwarded account a copy
      dkim.py            # keys, OpenDKIM tables, DNS record value
      dns_check.py       # MX/SPF/DKIM/DMARC checks and recommended records
      mailbox.py         # Maildir creation and deletion, doveadm folder stats, Sieve scripts, kick, disk usage
      activity.py        # last logins (database) and sending activity (mail log)
      spam.py            # SpamAssassin user preferences
  tests/
```

Commands orchestrate core functions and handle all user interaction. Core modules do one thing each and never import from `commands` or `ui`. Core modules import each other in one direction: `domains` → `spam`; `spam` → `names`; `senders` → `domains`, `names`; `forwards` → `domains`, `names`, `senders`; `addresses` → `activity`, `domains`, `forwards`, `names`, `senders`, `spam`, `system`; `dkim` → `names`, `system`; `dns_check` → `dkim`; `mailbox` → `names`, `system`.

---

## Behaviour

### Names

Domains and addresses are trimmed and lowercased. A domain is valid when it has at least two labels of letters, digits and hyphens (no leading or trailing hyphen), its last label contains a letter, and it has at most 50 characters: the size of `virtual_domains.name`. An address is `local@domain` with a valid domain, at most 100 characters in total (the size of the address columns) and a local part of at most 64 characters: dot-separated runs of letters, digits, `_`, `+` and `-`. `%` isn't allowed, because SpamAssassin's settings use a leading `%` for domains.

### Domains

The old helper script inserted a domain row every time it ran, so a domain name can occur more than once. Reads group by name, and rows that refer to a domain use its lowest id.

- `add`: fails if the domain exists. Inserts into `virtual_domains`, then creates the DKIM key, updates OpenDKIM and reads the key's record. Failures after the insert are warnings, since the domain stands; without a key, a note gives the command to create it later. Prints the recommended records:
  - `MX  DOMAIN  10 HOSTNAME`
  - `TXT DOMAIN  v=spf1 mx ~all`
  - `TXT mail._domainkey.DOMAIN  <key record>` (when there is a key)
  - `TXT _dmarc.DOMAIN  v=DMARC1; p=quarantine`
- `delete`: shows how many addresses (listing up to 10) and forwards go along. `domains.delete` only deletes a domain without accounts (with its spam preferences; forwards and sender permissions go through the foreign keys), so the command deletes each account with `addresses.delete` and then the domain, in one transaction. Then it logs out the accounts' sessions, and removes `/var/vmail/DOMAIN` when asked, the DKIM key, and the domain from OpenDKIM, each as a warning when it fails. It warns about forwards from other domains to the domain's addresses.

### Addresses

- Password hashing: `openssl passwd -6 -stdin` (SHA512-CRYPT, the password goes in through standard input), stored as `{SHA512-CRYPT}$6$...`. Minimum length 8; line breaks are refused.
- `add`: asks, in this order, for the address (it must not exist), whether to add a missing domain (in a terminal; otherwise it fails with the command to add it), and the password. Only then does it add the domain (as `domain add` does) and, in one transaction, the account: inserts into `virtual_users`, grants the account permission to send as itself, keeps it a copy when the address already had forwards (see Forwards), and creates `/var/vmail/DOMAIN/LOCAL/Maildir` with `.Sent`, `.Trash` and `.Junk`, plus symlinks for other client folder names: `.Verzonden items`, `.Verzonden Items`, `.Sent Messages`, `.Sent Items` → `.Sent`; `.Verwijderde items`, `.Deleted Messages` → `.Trash`; `.Ongewenste e-mail` → `.Junk`. The folders are created as the `vmail` user (like the old script's `sudo -u vmail`), so everything is vmail's, mode 0700. Existing mail and folders are kept (a real folder with an alias name stays), and a note says so.
- `password`: the account must exist; updates the hash.
- `delete`: in one transaction, deletes the account, its forward to itself, its name from every permission's list of logins, its spam preferences and login rows. Logs out its sessions (failing to is ignored: the sessions end when the mail client logs out). Removes the home directory when asked. Notes the forwards from the address, which stay, and, unless the address still forwards its own mail, warns about forwards to it, which now point to an address that is no longer on this server.

Stored mail is only created and deleted below `/var/vmail`, and never through a symbolic link: mailctl runs as root, and the `vmail` user could point a link anywhere. Creating and deleting run as the `vmail` user and work through open folders (`O_NOFOLLOW` and `dir_fd`), so a folder that is swapped for a link while it runs can't lead anywhere `vmail` couldn't go itself. Messages that Dovecot expunges during a delete are skipped. A file or folder `vmail` isn't allowed to change stops the work with an error that names it and says the mail folders must belong to `vmail`. The mail folder is named after the login in lowercase, as Dovecot's `mail_path` builds it, also for accounts written with capitals before mailctl.

### Sender permissions (`virtual_sender_aliases`)

Postfix's `smtpd_sender_login_maps` looks up `alias` and gets `users`: the logins allowed to send as that address, separated by commas or spaces. `senders.allow(login, address)` adds a login to the row (creating it if needed); `senders.revoke(login, address)` removes it and deletes rows that end up empty. Logins are compared regardless of case, because rows from before mailctl may use capitals. The list holds at most 255 characters, the column's size; adding beyond that is refused with an explanation.

### Forwards (`virtual_aliases`)

Postfix delivers mail for an address that has forwards only to those forwards. So an account with forwards also gets a forward to itself, which keeps a copy in its mailbox: when an account gets its first forward, and when an address with forwards becomes an account. When its last real forward is deleted, that row goes too. These rows are never listed or counted as forwards. Accounts that were forwarded without such a row before mailctl are left that way.

- `add`: the source domain must be on this server; the destination may be anywhere; an address can't forward to itself; duplicates are refused. These checks run before any question. If the destination is an account on this server, the command asks whether it may send as the source (default no) and grants it with `senders.allow`; `--send-as` for a destination elsewhere gives a warning.
- `delete`: removes the forward and revokes the destination's permission to send as the source, and says so when there was one.
- `list` shows source, destination and whether send-as is allowed.

### DKIM (fixes the tables being wiped)

Keys live in `/etc/opendkim/keys/DOMAIN/mail.private` (selector `mail`), as before; a key folder the old script named with capitals is found too. The key directory is the single source of truth: `dkim.write_tables()` scans it and rewrites `/etc/opendkim/KeyTable` and `/etc/opendkim/SigningTable` when their content changes (write to a temp file, then rename). Folders whose names aren't domain names are left out.

```
KeyTable:      mail._domainkey.DOMAIN DOMAIN:mail:/etc/opendkim/keys/DOMAIN/mail.private
SigningTable:  *@DOMAIN mail._domainkey.DOMAIN
```

- New keys: `opendkim-genkey -b 2048 -s mail -d DOMAIN -D DIR`, `mail.private` owned by `opendkim`, mode 0600.
- The DNS record value is made from the public half of `mail.private` (`openssl pkey -pubout`): `v=DKIM1; h=sha256; k=rsa; p=...`. So a key copied over without its `mail.txt` works as well.
- `dkim.update_opendkim()` rewrites the tables and runs `systemctl reload-or-restart opendkim` only when they changed. When the reload fails, the previous tables are put back, so the next update sees the change again and retries.
- `dkim create` never replaces an existing key. It shows a new key's record before it updates OpenDKIM; when the reload fails, it ends with exit status 1, so Ansible stops and the next run retries. It prints "Created a DKIM key for DOMAIN" when it created one, and "Updated the OpenDKIM tables" when only the tables changed; the Ansible task's `changed_when` looks for both.

Because Ansible no longer templates the two tables, a playbook run keeps every domain's signing entry. The old playbook also gave the server's own domain a new key on every run. So after the first run with mailctl, which signs every domain with a key again, check every domain, the server's own included, with `mailctl status DOMAIN` (the README says so too).

### Status of an address

1. **Folders:** `doveadm -f tab mailbox status -u ADDRESS "messages vsize" "*"`. Folders whose Maildir directory is a symlink are shown as "Sent Items → Sent" and left out of the total, so aliased folders aren't counted twice.
2. **Last login:** logins are recorded for IMAP, which webmail uses too, not for sending. The most recent row per service from `mailserver.last_login`: when (date and "3 hours ago") and from which IP. None: "No login recorded".
3. **Sending:** for each configured limit window (account-specific limits replace the defaults; an empty list means no limit, and then the last hour and day are shown), the recipients sent in that window against the limit. Also how many recipients the sending limit refused in the last 24 hours (Postfix logs a line for every refused recipient).
4. **Bounces:** the five most recent bounced recipients in the last 7 days, with status code and reason.

Each part is shown on its own: when one fails, for example because Dovecot isn't running, it shows as a warning and the other parts still show. The same goes for the DNS checks of a domain.

Sending data comes from `/var/log/mail.log.1` and `/var/log/mail.log`, read line by line; lines without any of the markers below are skipped before parsing.

- `QUEUEID: client=...`: a line with `sasl_username=ADDRESS` (compared case-insensitively) makes the queue ID the account's; any other `client=` line takes it away, because Postfix reuses queue IDs.
- `QUEUEID: from=<...>, size=N, nrcpt=N` gives the recipient count, once per message (deferred mail logs it again at each retry).
- `QUEUEID: to=<...>, ... dsn=X, status=bounced (REASON)` gives bounces.
- `NOQUEUE: reject: ... Sending limit reached ...; from=<ADDRESS>` counts limit hits.

The numbers come close to what postfwd counts, but aren't the same: the log gives each message's recipients after forwards are expanded, while postfwd counts the recipients a mail client names.

Both syslog timestamp formats are parsed: RFC 3339 (`2026-09-14T13:25:36.123456+02:00`, Ubuntu's default) and the traditional `Sep 14 13:25:36`, taken as local time with the offset in effect on that date and the most recent year that doesn't put it in the future. A missing rotated log is normal; a log that can't be read, or no log at all, gives a warning.

### Status of a domain

Checks, each shown as ✓ (ok), ! (warning) or ✗ (problem) with a one-line explanation and, when publishing a record would solve it, that record:

- **MX:** at least one MX host resolves to one of this server's IP addresses (from `ip -json address show scope global`).
- **SPF:** exactly one `v=spf1` record, which must allow every public address of this server (private addresses are ignored, unless the server has no public ones). The evaluator handles `all`, `ip4`, `ip6`, `a`, `mx`, `include` and `redirect` with qualifiers and CIDR lengths, stopping after 10 DNS lookups. When they're reached, `exists`, `ptr` and macros make the result a warning, since they can't be judged; an unknown mechanism, an invalid address, or a referenced domain without exactly one SPF record is a problem.
- **DKIM:** the TXT record at `mail._domainkey.DOMAIN` has the same `p=` as the key on this server (spaces ignored). No key on the server is a problem, with the command to create one.
- **DMARC:** exactly one record starting with `v=DMARC1` at `_dmarc.DOMAIN`, or else at a parent domain's, showing its policy. Missing is a warning; two or more are a problem.

DNS lookups use the system resolver with a 5-second timeout; a failed lookup is a warning.

Below the checks: the number of addresses and forwards, and disk usage of `/var/vmail/DOMAIN`.

### Spam settings (`spamassassin.userpref`)

SpamAssassin reads the rows where `username` is the recipient address, `%DOMAIN`, or `$GLOBAL`. Its query now orders them explicitly (`ORDER BY CASE LEFT(username, 1) WHEN '$' THEN 0 WHEN '%' THEN 1 ELSE 2 END`): sorting on the username, as before, depends on the collation, and under MySQL 8's default or `utf8mb4_unicode_ci` it put a domain's settings before the server's. Single settings from later rows override earlier ones; list settings add up. spamass-milter only passes the recipient for mail with one recipient, so mail to several recipients at once gets the server's settings; `spam set`'s help says so.

Targets map to usernames: an address → the address (must be an account), `server` → `$GLOBAL` (shown as "the whole server"), anything else → `%DOMAIN` (must be a domain on this server). Setting names are accepted in any case.

| Setting | Kind | Validation |
|---|---|---|
| `required_score` | single value | a plain number like `5` or `-2.5` |
| `welcomelist_from` | list | an address or pattern like `*@example.nl`, at most 100 characters |
| `blocklist_from` | list | same |

`set` checks the setting before asking for a value, replaces a single value or adds to a list, and says when nothing changed. The commands validate and normalise settings and values (`known_setting`, `normalise`) before they reach `set_value` and `unset_value`. `prefid` is `MAX(prefid) + 1`, since the table has no auto-increment. `unset` removes the setting, or one value of a list; removing all values of a list with more than one asks for confirmation. `show` lists every row that applies to the target, with where it's set and whether it's in effect or overridden. The database matches the rows (so hand-made rows that differ in case or accents count too) and computes where each belongs with the same expression SpamAssassin's query orders by; a row that matches without starting with `$` or `%` (an invisible first character) is placed with the target's own rows. Rows with other preference names, set by hand, are shown too, as in effect.

### Filters

`doveadm sieve list -u ADDRESS` for the script names and which is active, and `doveadm sieve get -u ADDRESS NAME` for their contents, each in a panel. Then the `.sieve` files in `/etc/dovecot/sieve-after/` as server-wide scripts that run after the account's own.

---

## Last-login tracking (Dovecot)

New table in `setup_mailserver_tables.sql`:

```sql
CREATE TABLE IF NOT EXISTS `last_login` (
 `userid` varchar(100) NOT NULL,
 `service` varchar(10) NOT NULL,
 `last_access` bigint NOT NULL,
 `last_ip` varchar(40) NOT NULL,
 PRIMARY KEY (`userid`, `service`, `last_ip`)
 ) ENGINE=InnoDB DEFAULT CHARSET=utf8;
```

Dovecot's dict writes `INSERT ... ON DUPLICATE KEY UPDATE` and only updates `last_access`, so the IP is part of the key: each address a user logs in from gets its own row, and `mailctl` shows the latest one per service. A nightly cron job (`/etc/cron.d/mailctl-prune-last-login`) deletes every row that has a newer one for the same user and service.

The `last_login` plugin is loaded for IMAP only (webmail logs in over IMAP too, so it counts). Its dict is proxied to the dict service, which writes to MariaDB as `mailuser`. The dict socket is owned by `vmail`, mode 0660, so IMAP processes can reach it.

- **Dovecot 2.3:** `local.conf` gets `mail_plugins = $mail_plugins last_login` in `protocol imap`, `last_login_dict = proxy::sql`, `last_login_key = last-login/%s/%u/%r` and `last_login_precision = s` in `plugin`, the dict socket, and `dict { sql = mysql:/etc/dovecot/dovecot-dict-sql.conf.ext }`. The template `dovecot-dict-sql-2.3.conf.ext.j2` (mode 0640, group `dovecot`) holds the connection and the map.
- **Dovecot 2.4:** a protocol's `mail_plugins` replace the global list (checked with `doveconf`), so `local.conf` lists `fts`, `fts_xapian` and `last_login` in `protocol imap`, and `fts`, `fts_xapian` and `sieve` in `protocol lmtp`. The template `dovecot-last-login-2.4.conf.j2` → `/etc/dovecot/conf.d/95-last-login.conf` (mode 0640, group `dovecot`, because it holds the database password) holds the `last_login` block, the dict socket and a `dict_server` with the SQL connection and `dict_map`. The 2.3 files holding the password are removed on 2.4 servers.

---

## Output and errors

- Success: green `✓` line. Warning: yellow `!`. Problem: red `✗`. Notes: dim.
- Tables use Rich; sizes are human-readable (`1.2 GB`, 1 KB = 1024 bytes), times show both date and "3 hours ago".
- Messages and DNS record values are never broken into lines, so they can be copied in one piece and Ansible reads them whole.
- Text from users, the database or DNS goes through `ui.text()`: it's never read as Rich markup, and control characters are shown as `\x1b` and the like, so a Sieve script or DNS record can't send escape codes to the admin's terminal.
- Every expected failure raises `MailctlError(message, hint)`. The root command group prints it in red with the hint below it and exits with status 1. Unexpected exceptions keep their traceback.
- Once a change is saved in the database, failures in the steps that follow (deleting mail, the DKIM key, OpenDKIM) are shown as warnings after the success message, and messages only claim what actually happened.
- The root check runs when a command opens its session, so `--help` works for anyone.

---

## Testing

- **Setups:** the tests run in virtualenvs pinned to the Ubuntu packages (`tests/requirements-24.04.txt`, `tests/requirements-26.04.txt`): Typer 0.9.0, Click 8.1.6, Rich 13.7.1, PyMySQL 1.0.2 and dnspython 2.6.1 on Python 3.12, and Typer 0.19.2, Click 8.1.8, Rich 13.9.4, PyMySQL 1.1.1 and dnspython 2.8.0 on Python 3.14. The database tests run against MariaDB 10.11 unpacked from Ubuntu 24.04's `mariadb-server-core` and against MySQL 8.0.
- **Unit tests** for everything that doesn't need a server: name validation, SPF evaluation and the other DNS checks with a fake resolver, mail log parsing (both timestamp formats, daylight saving time, deferred mail, reused queue IDs, bounces, limit hits), OpenDKIM table generation and the retry after a failed reload, the DKIM record from a real key, folder alias detection and disk usage in a temp Maildir, symbolic link refusal, spam-setting merging, and the output helpers (control characters, no hard wraps at 80 columns, formatting).
- **Database tests** against a throwaway server started by a pytest fixture (unix socket only), loaded with the role's own SQL schema files: domains, addresses, sender permissions, forwards and the copy rows, spam settings (including SpamAssassin's configured query, on a collation where sorting by username alone gives the wrong order), last logins written the way Dovecot writes them and the nightly cleanup query taken from the Ansible task, and the delete cascades. `MAILCTL_TEST_DB_PREFIX` selects the installation; without a server they're skipped, or fail with `MAILCTL_REQUIRE_DB=1`.
- **CLI tests** with Typer's `CliRunner` against the test database, with fake `opendkim-genkey` (making real short keys), `doveadm` and `systemctl` on `PATH` that record their exact arguments: help output and examples, the add/list/delete flows, `--password-stdin`, `--yes`, prompts, logouts, failures after a saved change, and the missing-argument errors without a terminal. Output meant for Ansible is checked on standard output alone.
- **Dovecot config:** the 2.4 templates are checked with `doveconf` on an Ubuntu 26.04 server against a config read from standard input (nothing written to the server). The 2.3 templates are checked with `doveconf` from Ubuntu 24.04's `dovecot-core` package, unpacked locally.

---

## Out of scope

- DKIM key rotation.
- Reverse DNS (PTR) checks: they concern the server, not a domain.
- Quotas.
- Upgrading a mail server from Ubuntu 24.04 to 26.04 in place.
- Roundcube's password plugin, which hashes with MariaDB's `ENCRYPT()` (still available in MariaDB 11.8), is unchanged.
