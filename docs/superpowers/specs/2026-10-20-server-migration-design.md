# Design: moving a server over, from the old one

Date: 2026-10-20. Uses [mailctl](2026-09-14-mailctl-design.md) and `domainctl` as they are.

## Goal

Move a customer off an old, hand-built server onto one set up with this repository, from a single terminal on the
old server. The old machine is whatever it happens to be: Apache, mail set up by hand or by a panel, databases and
sites in places nobody wrote down. Nothing may be assumed about it and nothing on it is changed.

What moves: the mail setup (domains, accounts with their password hashes, forwards, spam settings, Sieve filters),
WordPress sites with their files and databases, and the cron jobs that belong to them.

What does not move: the mail itself (`imapsync`, by hand afterwards), sites that are not WordPress (reported, never
touched), and DNS (reported; the records to set come from `mailctl dns show` and `domainctl`).

## The shape of it

Two halves, one connection:

- **`migrate`**, on the old server. Finds out what is there, shows the dashboard, and drives everything. It is the
  only thing anybody types in.
- **`migratectl`**, on the new server. Answers what it already has and what it can take, and does the work there:
  creating domains, accounts, sites, databases and cron jobs, through `mailctl` and `domainctl`.

The old server opens an SSH connection to the new one with a key it makes itself, as the `ansible` user, and runs
`sudo -n migratectl` there. Servers built with this repository refuse root over SSH (`PermitRootLogin no`) and
allow only the names on the `AllowUsers` line; `ansible` is on it and has passwordless sudo, so the work still
runs as root on the new side without opening the server up. The one thing done on the new server is pasting the
key into `/home/ansible/.ssh/authorized_keys`, once; after that a terminal there is for watching, never needed.

## Decisions

| Question | Decision |
|---|---|
| Where the work is driven from | The old server. The new one only answers and acts |
| Which side decides what is possible | The new one: it says what it has, what it can take, and what it refuses, with the reason |
| Overwriting | Never without asking. A domain, account, site or database that is already there stops that item and asks |
| Picking what moves | A list of everything found, with both sides' status; tick a batch and it runs to the end |
| Interruptions while it runs | Only a conflict the new side won't decide alone. Nothing else |
| Mail messages | Out of scope: `imapsync` afterwards. The run ends with the exact lines to use |
| Sites that aren't WordPress | Listed with what was found, and left alone |
| Cron jobs | Found per user and in `/etc/cron.d`, shown with the site or account they belong to, installed for the matching user on the new server |
| DNS | Reported only. The run ends with the records the domains need |
| Running it again | Everything is idempotent: a second run offers only what hasn't moved |
| A site that already moved | Can be done again, as a catch-up before the DNS is flipped: it re-syncs the files and **replaces** the database, after saying what it is about to replace and being told yes |
| The old mail setup | Postfix, Dovecot and MySQL with the same tables as this repository (`virtual_domains`, `virtual_users`, `virtual_aliases`, `virtual_sender_aliases`). Another panel's schema is not read: it says it found no mail it can read, names what it looked for, and the sites still move |
| imapsync | Each account's own password. The report prints a line per account with the password left to fill in |

## What the old side may rely on

An old server has what it has. `migrate` is one Python 3 file, standard library only, no third-party modules, and
nothing newer than Python 3.6 syntax. It uses the `mysql`/`mariadb` client, `doveadm`, `apache2ctl`, `crontab` and
`rsync` **when they are there**, and says what it couldn't look at when they aren't, rather than failing.

It reads; it writes nothing on the old server except its own SSH key and a state file under `/var/lib/migrate`.

*(My call, not asked for: a single stdlib file rather than reusing `export-mailserver.sh`, which needs `jq` and
would have to be shipped and kept in step. The export format it writes is the same one `mailctl import` reads.)*

## What it finds out

| Thing | Where it looks |
|---|---|
| Mail domains, accounts, forwards | The mail database: `virtual_domains`, `virtual_users`, `virtual_aliases`, `virtual_sender_aliases`, under whatever name it has (found by looking for those tables) |
| Spam settings | `spamassassin.userpref`, when there is one |
| Sieve filters | `doveadm sieve list` / `get` per account; the sieve directory under the mailbox when doveadm isn't there |
| Sites | Apache's own view of itself (`apache2ctl -S`), then each vhost's `DocumentRoot` and `ServerName`/`ServerAlias` |
| WordPress | A `wp-config.php` under the document root: the database name, user, password and table prefix come from it |
| Databases | The MariaDB/MySQL server: size per database, so the dashboard can say what the copy will cost |
| Cron jobs | Every user's crontab and `/etc/cron.d`, matched to a site by the paths in them |

## The conversation between the two

Line-based JSON over the SSH connection, one request and one answer per call; no process runs on the new server
between calls. `migratectl` reads a request on standard input and writes an answer on standard output.

| Request | Answer |
|---|---|
| `hello` | Which version it speaks, the server's name, which of `mailctl` and `domainctl` are there |
| `status` with the inventory | Per item: `new`, `exists` (with what it is now), or `refused` (with the reason) |
| `apply` with one item and the choices for it | What it did, or what it needs answered |

An item only ever moves through `apply`, one at a time, so a run that is interrupted leaves the rest untouched.

## Moving one thing

**A mail domain.** The old side writes the same JSON `mailctl import` reads, for that domain only. The new side
imports it, which adds what is missing and leaves what is already there alone. Accounts keep their password
hashes. For an account whose hash the new server can't check, the dashboard asks for the password while the move
runs; it is hashed on arrival, the way the new server hashes its own. Filters that can't be read are named and the
rest are imported.

**A WordPress site.** The new side makes the site first (`domainctl add`, which makes the user, the web root and
the logs even while the domain still points at the old server), then the database and its user. The old side
rsyncs the files over the same SSH connection and streams a dump of the database into the new one. The new side
writes the database name, user and password into the `wp-config.php` there. Nothing is deleted on the old server.

**A cron job.** Installed for the user the site belongs to and running from that moment, with the paths and the
PHP interpreter rewritten for the new server. A job that refers to anything outside the site is shown and skipped.
Until the DNS is flipped the old server runs the same jobs, which the run says once, in the report.

## What the end of a run says

What moved, what was skipped and why, then the three things that are still by hand: the `imapsync` lines per
account, the DNS records each domain needs, and the reminder that `domainctl add` must be run again per site once
the DNS points at the new server, for the certificate.

## Goals

- One terminal, on the old server, for the whole move.
- Nothing is assumed about the old server; what can't be read is reported, not fatal.
- Nothing on the new server is overwritten without being asked.
- A batch runs to the end without supervision.
- Running it again moves only what hasn't moved.

## Non-goals

- Mail messages, calendars and address books (`imapsync`, and SOGo's own database).
- Sites that aren't WordPress.
- DNS changes, certificates, or anything that only works once DNS points at the new server.
- Changing anything on the old server.
- Serving as a backup tool.

## Anti-goals

Things that would make this a failure even if it ran:

- Overwriting a live site, database or mail account on the new server.
- Needing to know the old server's quirks before it can start.
- Needing packages installed on the old server.
- Leaving a half-moved site that neither side can tell you about.

## Calls made while writing this up

A reviewer read this document and asked what a builder couldn't settle alone. These are the answers, as decisions:

| Question | Decision |
|---|---|
| Root on the new side, with `PermitRootLogin no` in the role | Connect as `ansible`, run `sudo -n migratectl`. Nothing about sshd changes |
| A password hash the new server can't check | The dashboard asks for that account's password while the move runs. It goes over the SSH connection and is hashed on the new server in the scheme it uses itself; it is never written down on either side. Enter skips the account, which is then named in the report |
| What "already there" means per item | Mail: `mailctl import` merges and only adds what is missing, so a domain that is partly there needs no question. A site asks when its web root holds files or its database exists. A database is never replaced without a yes meant for that database |
| Who makes the WordPress database and writes `wp-config.php` | A new `domainctl wp adopt`: it makes the database and its user, imports the dump, and changes the four `define()` lines in the `wp-config.php` that came across, keeping its salts, table prefix and own constants. `domainctl` stays the only thing that touches sites and databases; after the rsync the files are given to the site's user and `domainctl sync` is run |
| Cron jobs starting at once | Installed and running, as asked. Until the DNS is flipped both servers run them, which the run says once. The interpreter path is rewritten (`/opt/plesk/php/8.1/bin/php` and the like); a job whose paths can't be mapped is shown and skipped |
| Catch-all addresses and aliases mailctl can't express | Left out of the file before it is imported, so one of them can't refuse the whole domain, and listed at the end as hand work |
| One mail filter the new side refuses | `mailctl import` and `mailctl filters import` take the filters they can read and name the ones they can't, instead of refusing the whole file over one of them. That is a change to mailctl, and part of this work |
| Databases that belong to no WordPress site | Reported, like the sites that aren't WordPress. Never moved by itself |
| Being allowed to send under another address, where no forward goes with it | A mailbox can be allowed to put another address in the From line. The file carries that along with the forward it belongs to, so a permission without a forward has nowhere to go: it is printed at the end for you to set yourself |

## Left to the builder

- How the dashboard is laid out and how items are ticked (it is a terminal on an old server: plain, no curses
  needed, and it must work over a 80-column SSH session).
- How Apache's vhosts are read when `apache2ctl` isn't available.
- Where `migratectl` lives in this repository and how the roles install it.
- How the two halves are tested without an old server: fakes for the client commands, and a test that the JSON the
  old side writes is the one `mailctl import` reads.
