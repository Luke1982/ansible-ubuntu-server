# Ansible server setup

Automated provisioning of Ubuntu servers with roles for base configuration, web hosting, database, and mail.

## Getting started

### 1. Prepare the target server

On a fresh Ubuntu server, as root, create the `ansible` user and grant it passwordless sudo. The whole block can
be pasted at once: nothing in it asks a question.

```bash
adduser --disabled-password --gecos "" ansible
usermod -aG sudo ansible
echo "ansible ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/ansible
chmod 440 /etc/sudoers.d/ansible
```

Plain `adduser ansible` asks for a password and then for a full name, a room number and so on, and anything pasted
after it is answered into those questions instead of being run.

The user has no password: Ansible logs in with a key, and sudo asks for none. So give it the key you are logged in
with yourself, rather than `ssh-copy-id`, which would need a password to get in:

```bash
install -d -m 700 -o ansible -g ansible /home/ansible/.ssh
install -m 600 -o ansible -g ansible ~/.ssh/authorized_keys /home/ansible/.ssh/authorized_keys
```

From your own machine, check that it works before running anything else:

```bash
ssh ansible@<server-ip> sudo true
```

### 2. Install Ansible dependencies

On your local machine, from the project root:

```bash
ansible-galaxy collection install -r requirements.yml
```

### 3. Create a private inventory file

Copy `inventory-example.yml` to `private/<hostname>.yml` and fill in the server's name, IP, SSH settings and role variables. The comments in the example explain each setting.

```bash
mkdir -p private
cp inventory-example.yml private/<hostname>.yml
```

`ansible.cfg` loads every file in `private/` as one inventory, so you never pass `-i`.

### 4. Create private variable files

Create the required variable files for the roles you are deploying. Each role has a `vars/private/` directory with a README explaining the expected file structure.

| Role | Required file |
|------|---------------|
| `basic` | `roles/basic/vars/private/linuxusers.yml` |
| `mail` | `roles/mail/vars/private/mailvars.yml` |

A mail server can start with the Bayes learning of an older one: put a dump of its `bayes` database at `roles/mail/files/private/bayes_dump.sql` and the first run restores it, as long as this server has learned nothing yet. See [`roles/mail/files/private/README.md`](roles/mail/files/private/README.md).

### 5. Create a playbook

Create `<hostname>.yml` in the project root. Set `hosts:` to the server's name from its inventory file, and include only the roles that apply to that server:

```yaml
---
- name: Deploy <hostname>
  become: yes
  hosts: <hostname>
  roles:
    - basic
    - web
  vars_files:
    - roles/basic/vars/private/linuxusers.yml
```

A playbook only runs on the host it names, and that name has to be in the inventory. Without it:

```
[WARNING]: Could not match supplied host pattern, ignoring: web03.majorlabel.nl
skipping: no hosts matched
```

That means there is no `private/web03.majorlabel.nl.yml` -- the playbook in the project root is not an inventory, it only names a host. See [Adding another server](#adding-another-server).

### 6. Run the playbook

```bash
ansible-playbook <hostname>.yml
```

No password prompts — Ansible authenticates via the key in `ansible_ssh_private_key_file`, and `become` uses passwordless sudo.

### 7. Bring the sites, webmail and the mail certificate up to date

A playbook run installs and configures; it never publishes a DNS record, asks
certbot for a certificate, or takes a site away. Those belong to three commands
you run on the server yourself, so you are there to read what they say. Each run
ends by naming them. After a first run, and after adding or removing a domain:

```bash
domainctl sync              # the web sites: the template block, the HTTPS redirect, file permissions
mailctl webmail sync        # webmail per mail domain, with a certificate of its own
mailctl certificate sync    # the hostname and every mail.DOMAIN in one certificate
```

`domainctl hostname` gets the certificate of the server's own name on its own, also on a server without mail.

Until `domainctl hostname` or `mailctl certificate sync` has run, a new mail server has only the empty
certificate files the playbook puts there for Dovecot to start with.

---

## Adding another server

Three files, every time:

| What | Where | From |
|------|-------|------|
| The inventory: address, SSH user, key, `hostname`, `defaultdomain` | `private/<hostname>.yml` | `cp inventory-example.yml private/<hostname>.yml` |
| The playbook: which roles that server gets | `<hostname>.yml` in the project root | copy another server's and change the name |
| The role variables, for the roles in that playbook | `roles/*/vars/private/` | the README in each of those directories |

Then check that Ansible knows it, before running anything:

```bash
ansible-inventory --list | grep <hostname>     # it is in the inventory
ansible <hostname> -m ping                     # it answers, and the key works
```

## Project structure

Each server has its own playbook (e.g. `web01.whservice.nl.yml`) in the project root, which targets that server by name. Each server also has an inventory file in `private/`, and `ansible.cfg` loads all of them as one inventory. Private variable files live under each role's `vars/private/`. None of these private files are committed to git.

## Roles

| Role | What it does |
|------|-------------|
| `basic` | Hostname, base packages, unattended-upgrades, Linux users, SSH hardening, iptables firewall (applied at boot), fail2ban |
| `web` | OpenLiteSpeed with LiteSpeed's PHP 8.3, PHP-FPM and CLI from Ubuntu (8.3 on Ubuntu 24.04, 8.5 on 26.04), Composer (global), wp-cli, nvm (system-wide), Redis with the PHP extension for it (object caching), certbot, fail2ban jails for WordPress password guessing, Let's Encrypt directory, the shared virtual host template, and `domainctl` (below) |
| `db` | MariaDB, phpMyAdmin on a site of its own at `pma.<hostname>`, and a gzipped dump of every database every night (below) |
| `mail` | Postfix, Dovecot with full-text IMAP search (fts-xapian) and login tracking, OpenDKIM, SpamAssassin, SOGo (webmail, calendars, address books and ActiveSync), per-user sending limits (postfwd), fail2ban jails for SMTP, IMAP and webmail brute force, and `mailctl` (below) |

## Database backups

Every night at three, a dump of every database on the server, gzipped, in `/home/dbbackups`. The ones older than 14 days go, but only after a run in which every database was dumped: failing backups shouldn't quietly eat the last good ones. A dump is written under a temporary name and renamed once it is whole, so nothing that looks like a backup is half a backup.

They are made by `dbbackups`, a user with no password at all: MariaDB knows it by its Linux name (`unix_socket`), so only that Linux user can be it, and there is nothing to read out of a file, a process list or one of the backups. It may read every database and nothing else -- `SELECT`, and what a dump needs besides: views, triggers, events and a lock on a table that isn't InnoDB.

```bash
systemctl list-timers mariadb-backup.timer     # when it last ran, and when it runs next
systemctl start mariadb-backup                 # now, rather than tonight
journalctl -u mariadb-backup -n 20             # what it did, with the size of each dump
zcat /home/dbbackups/sogo-20260930-030001.sql.gz | mariadb sogo   # putting one back
```

The hour, where they go, how long they are kept and the user's name are `db_backup_time`, `db_backup_home`, `db_backup_keep_days` and `db_backup_user` in [`roles/db/defaults/main.yml`](roles/db/defaults/main.yml). The dumps hold everything the databases hold, including the accounts in `mysql` and the password hashes in `mailserver`: they are readable by root and by `dbbackups`, and by nobody else. This server keeps them on its own disk, which is a copy, not a backup; copy them off it as well.

## phpMyAdmin

Servers with the `db` role get phpMyAdmin at `https://pma.<hostname>`, for
example `https://pma.web01.example.com`, on a site of its own in OpenLiteSpeed.
Publish an `A` record (and an `AAAA` record) for that name pointing at the
server: nothing publishes it for you. Log in with a MariaDB user, `root` for
instance.

The site has no certificate of its own, so OpenLiteSpeed serves it with the
certificate on the HTTPS listener, which is the self-signed one OpenLiteSpeed
comes with unless you replaced it. Browsers warn about it; the warning is about
the name in the certificate, not about the encryption. Plain HTTP redirects to
HTTPS, so the database password is never sent unencrypted.

phpMyAdmin's files are in `/var/www/phpmyadmin` and the site's settings in
`/usr/local/lsws/conf/vhosts/pma.<hostname>/vhconf.conf`, where WebAdmin keeps
every site's, so WebAdmin shows this one like all the others. Every playbook run
writes that file, so change the role, not WebAdmin, for anything about this site.

The role adds the site to `httpd_config.conf` and its name to every listener for
port 80 and 443, marked with the note "Managed by Ansible: phpMyAdmin (db
role)", and leaves the listeners themselves and your other sites alone. It keeps
the previous version of that config next to it as
`httpd_config.conf.pmasite.bak`. The script that edits it, and its tests, are in
`roles/db/files/pmasite`:

```bash
python3 -m pytest roles/db/files/pmasite
```

Anyone who reaches the site can try passwords on your databases. If it doesn't
need to be open to everyone, limit it to your own addresses under the site's
Access Control in WebAdmin.

phpMyAdmin that was installed in OpenLiteSpeed's Example site is moved to this
site, out of reach of every other name that site answers to. Its settings are
kept, with a passphrase added for cookie login, so a restart doesn't sign
everybody out; the file it replaces is kept as `config.inc.php.previous`.

## Blocking password guessing

Postfix, Dovecot and SOGo each have a fail2ban jail. Banning on failed logins
alone catches two things that look identical in the log: someone guessing
passwords, and a customer's phone that still has last week's password saved and
retries it every few minutes, forever. Banning the second one permanently locks
a real customer out of their mail.

So before the SMTP or IMAP jail bans an address, it asks
`/usr/local/sbin/f2b-mail-ignore.sh` whether the address looks like a customer.
It is left alone only if all three hold: it has logged in successfully at least
twice, it is failing on no more than three accounts, and it has tried at most
one account that does not exist. A guesser fails the first test, because it
does not know any password. The other two stop an address that cracked one
mailbox from hiding behind its own successes while guessing at the rest.

This is safe because whoever already has a working password logs in first try
and never produces a failure at all, so they never reach the jail in the first
place. The jail only ever sees guessers and real customers.

Webmail is different: a failed webmail login comes from the browser's address,
which has no mail login history behind it, so the check cannot tell a guesser
from someone mistyping their own password. That jail bans for an hour instead
of forever. Calendar, contact and ActiveSync logins have no jail at all: they
leave no log line fail2ban can tell apart from a normal first request, and are
throttled by SOGo itself (`roles/mail/templates/sogo.conf.j2`).

The thresholds are in `roles/mail/defaults/main.yml`. They were fitted to one
server's traffic and checked against a month of its logs, so replay real logs
before changing them. Addresses that were let off are logged: `journalctl -t
f2b-mail-ignore`. To check the rules themselves:

```bash
bash roles/mail/tests/f2b-mail-ignore/run-tests.sh
```

### WordPress

Web servers have two jails for WordPress, `wordpress-hard` and `wordpress-soft`, that cover every site on the
server at once. They depend on the WP fail2ban plugin on each site. The plugin doesn't write to the site's own logs:
it sends each failed login to syslog with the site's name in the tag
(`wordpress(example.nl)[1234]: Authentication failure for admin from 1.2.3.4`). PHP for every site writes to the
same journal, so one jail reads them all, and a new site needs no change to fail2ban. A site without the plugin
logs nothing, so it isn't protected: `domainctl wp check` lists those sites, and `--fix` puts the plugin on them.

The hard jail bans for a day after one attempt, for things nobody does by accident, like user enumeration or
XML-RPC multicall guessing. The soft jail bans for an hour after five wrong passwords in ten minutes, because a
customer can mistype theirs. The filters are the plugin's own (`roles/web/files/filter-wordpress-*.conf`, from
version 5.4.1). Replace them when a newer version logs new messages. To see what the jails have caught:
`fail2ban-client status wordpress-soft`.

## Managing web sites with domainctl

Web servers get `domainctl`, a command-line tool for the sites on the server. Run it as root; `domainctl --help` lists everything, and `--help` on a command explains it with an example.

| Command | What it does |
|---------|-------------|
| `domainctl add DOMAIN` | Sets a site up: its Linux user, directories, permissions, virtual host and certificate |
| `domainctl list` | The sites with their domain, user and certificate |
| `domainctl state USER` | One word for a program to read: `served`, `made` (its user and directories are there, waiting for the domain to point here) or `absent` |
| `domainctl check [USER]` | Whether a site's names, permissions and certificate are in order |
| `domainctl delete SITE` | Removes a site, by its name or its domain; `--purge` takes the user and its files too. A site that stopped before it had a virtual host is only its user and files, so those go without `--purge` |
| `domainctl hostname` | Gives the server's own name a Let's Encrypt certificate, through a site of its own that only answers Let's Encrypt (the virtual host `Server`). Postfix and Dovecot present it |
| `domainctl sync` | Brings the template block and every site's permissions up to date. Run it by hand; the playbook only says to |
| `domainctl dns credentials` | The TransIP login, shared with mailctl |
| `domainctl wp install USER` | Installs WordPress on a site with wp-cli: its own database and MariaDB user, and the WP fail2ban plugin |
| `domainctl wp check [--fix]` | Which WordPress sites have WP fail2ban active; `--fix` installs and activates it where it isn't |

`domainctl wp install` runs wp-cli as the site's user and only in an empty web root (domainctl's placeholder page
aside). The database and its MariaDB user are named after the site, and only `wp-config.php` knows the password.
The administrator's password is made up and shown once. OpenLiteSpeed may write in `wp-content/uploads` and nowhere
else, so WordPress can't update itself or install plugins from its dashboard. Do that with wp-cli as the site's
user: `su - USER -c 'wp --path=public_html/www plugin update --all'`.

### How a site is put together

Every site is one member of a single OpenLiteSpeed template, `conf/templates/webhosting.conf`, which the playbook writes from `roles/web/templates/webhosting.conf.j2`. The member's name is the site's Linux user, and the template finds everything by it: the home directory it serves (`/home/$VH_NAME/public_html/www`), the logs it writes (`/home/$VH_NAME/logs/`) and the certificate it presents (`/etc/letsencrypt/live/$VH_NAME/`). So a site's certificate is named after its user, not its domain.

The block in `httpd_config.conf` is the one named in `web_template` (`webhosting`), or one already there whose name differs from it only in case: OpenLiteSpeed's own is called `Webhosting`, and a server whose sites were set up in WebAdmin has its members in that. `domainctl` manages the block that holds the sites, rather than adding a second one beside it, and says so when a server has both. A site that was made by hand therefore shows up in `domainctl list` as it is, with its user, its home directory and its certificate; `domainctl add DOMAIN --existing-user` gives a site to a Linux user that is already there.

The user has no password. It exists to keep one site's files away from the others, not to log in with: root reaches it with `su - USER`, and sshd only lets in the users on the `AllowUsers` line of `/etc/ssh/sshd_config.d/custom.conf`. The playbook puts the users from `linuxusers.yml` with `ssh_access: true` on that line, and keeps everyone already on it: giving a site SFTP by hand, by adding its user there and giving it a password, survives the next run. To take that access away, remove the name from the line on the server, or give the user `state: absent` in `linuxusers.yml`, which takes it out as well.

OpenLiteSpeed serves and runs PHP as `nobody`, which is not in the site's group, so it is let in by ACL and no further:

- `/home/USER` and `/home/USER/public_html` — `--x`, enough to walk through, not to list
- `/home/USER/public_html/www` — `r-x`, with the same as a default entry so files uploaded later are readable too
- `/home/USER/logs` — `rwx`, since OpenLiteSpeed writes the site's logs itself

**`nobody` can read the web root but never write it**, so a CMS that uploads files, writes a cache or installs plugins fails until you open that one directory up by hand:

```bash
setfacl -m u:nobody:rwx -m d:u:nobody:rwx /home/USER/public_html/www/wp-content/uploads
```

`domainctl check` and `domainctl sync` compare the ACL entries above and put back any that went missing; entries you added yourself are left alone.

### What `domainctl add` does

1. Works out the user name from the domain (`example.nl` becomes `example`), or takes `--user`.
2. Makes the user, its home directory, `public_html/www` and `logs`, with a placeholder page, and sets the ACL entries above. This happens whatever the DNS says, so a site that is moving here can be put in place before its domain is.
3. Looks the domain and `www.DOMAIN` up **in real DNS**, because that is what Let's Encrypt checks. The domain has to point here, and only here.
   - A name with **no record at all** is offered to TransIP, if the domain is in that account and uses TransIP's nameservers.
   - A name that **still points at the old server** is offered the move: it asks first, naming what it takes away, since visitors go to this server as soon as the old record expires. It never moves a name without a yes (`--yes` answers for you).
   - Either way it waits until TransIP's nameservers serve the new record.
   - When the domain still doesn't point here, that is where it stops: the directories are ready, and it says to run the same command again after the move. The virtual host and the certificate wait, because Let's Encrypt would refuse them.
4. `www` is included only when it points here. A name that doesn't resolve here would fail the whole certificate request and take the bare domain down with it, so it is left out with a warning instead. Run the command again once the record is right.
5. Adds the member to the template and restarts OpenLiteSpeed.
6. Checks that OpenLiteSpeed really answers for the domain on port 80, by fetching a file of its own from the challenge folder at each of the server's addresses. A failed Let's Encrypt validation counts against their limits; this check does not.
7. Switches the redirect to HTTPS off in the template, runs certbot as a test and then for real (`certonly --webroot`), and switches the redirect back on — always, also when certbot fails.

Run it again for the same domain whenever something stopped it: it picks up where it left off, taking over the user and directories it made itself, and adds `www` to the certificate once that record points here.

Every address record it publishes is the server's IPv4 address, for the reason in [Mail filters](#the-certificate-for-mail-programs)' neighbourhood: OpenLiteSpeed listens on IPv4 as it comes, so an `AAAA` record would cost every visitor a wait before their browser falls back to the address that answers.

Nothing is rolled back when certbot fails: the site stays up over HTTP and the message says what Let's Encrypt refused. Fix that and run `domainctl add DOMAIN` again, or take it away with `domainctl delete DOMAIN`.

### The redirect to HTTPS

The redirect lives in the shared template, between the markers `# BEGIN https-redirect` and `# END https-redirect`, so switching it off switches it off for **every** site on the server for as long as certbot runs. That is a few seconds and `domainctl` always puts it back, but it is worth knowing. The template also never redirects `/.well-known/acme-challenge/`, so `certbot renew` from its timer keeps working on its own, without anything switching anything off.

If a run is killed at exactly the wrong moment, the next `domainctl sync` switches the redirect back on and says it did.

OpenLiteSpeed's config is changed by reading it and renaming a new file into place, so two commands doing that at once would lose one another's changes. Both tools take a lock first, `/run/serverctl-openlitespeed.lock`, and hold it for the whole of the change: `domainctl add` including certbot, and `mailctl webmail sync`, `mailctl certificate sync` and `mailctl autodiscover publish` over their read and write. The second command waits; after five minutes it gives up and says another command is still busy.

### The code

`domainctl` is in `roles/web/files/domainctl`, with its tests in `roles/web/files/domainctl/tests`. The OpenLiteSpeed, TransIP, DNS and certbot code it shares with `mailctl` is in `shared/serverctl`, with its own tests in `shared/tests`; the playbook copies that directory next to each tool, so a web server needs no mail role and the other way round.

```bash
pytest shared/tests roles/web/files/domainctl/tests
```

## Managing mail with mailctl

Mail servers get `mailctl`, a command-line tool for the mail domains, addresses, forwards, DKIM keys, DNS records and spam settings. Run it as root on the server; `mailctl --help` lists everything, and `--help` on a command explains it with an example.

| Command | What it does |
|---------|-------------|
| `mailctl domain add example.nl` | Adds a domain, creates its DKIM key and shows the DNS records to publish |
| `mailctl address add info@example.nl` | Creates an account; asks for the password |
| `mailctl address import accounts.json` | Creates the accounts in a JSON file of addresses and passwords, for moving a server to this one |
| `mailctl import mailserver.json` | Creates the domains, accounts, forwards, spam settings and filters in a JSON file from another server (below) |
| `mailctl export mailserver.json` | Writes all of those from this server to a JSON file, for `mailctl import` on another one |
| `mailctl forward add sales@example.nl info@example.nl` | Forwards mail for an address |
| `mailctl status info@example.nl` | Folders, last logins, sending against the limits, recent bounces |
| `mailctl status example.nl` | Checks the domain's DNS records and whether the certificate includes `mail.example.nl` |
| `mailctl dns show example.nl` | Shows the DNS records the domain needs |
| `mailctl dns publish example.nl` | Publishes those records at TransIP; shows the changes and asks first (`--dry-run` only shows them) |
| `mailctl doctor` | Checks the server (hostname, reverse DNS, certificate, spam filter) and every domain; exit status 1 when something is wrong |
| `mailctl autodiscover publish example.nl` | Lets Thunderbird and Outlook find the domain's mail settings by themselves (below) |
| `mailctl certificate sync` | Puts the hostname and every `mail.DOMAIN` that points here in one certificate, and makes Postfix and Dovecot read it (below). Run it by hand; the playbook only says to |
| `mailctl spam set example.nl required_score 4` | Changes a spam setting for an account, a domain or the whole server |
| `mailctl spam learn` | Teaches SpamAssassin from everybody's Junk folder and inbox; runs every night by itself (below) |
| `mailctl filters show info@example.nl` | Shows an account's mail filters |
| `mailctl filters import mailserver.json` | Takes only the filters from another server's file, into webmail's list (below) |
| `mailctl filters run info@example.nl` | Runs an account's filters over the mail that is already in a folder (below) |
| `mailctl webmail brand --colour '#09526D'` | Puts webmail in the colours and the logo of whoever runs the server (below) |
| `mailctl webmail sync` | Publishes missing `webmail.DOMAIN` records at TransIP, sets up webmail with a certificate for the domains whose `webmail.DOMAIN` points here, and removes it for the others. Run it by hand; the playbook only says to |

Last logins are recorded for IMAP, which webmail and phones on ActiveSync use too; accounts that only send mail show no logins.

### How a domain's mail is set up

- Mail for `example.nl` is delivered to `mail.example.nl` (the MX record), which points to the server.
- Mail programs use `mail.example.nl` for IMAP and sending. SRV records tell them so, which lets many of them find their settings by themselves.
- The server sends mail as its hostname. The hostname must point to the server, and the server's addresses back to the hostname (reverse DNS, set at the server's provider).
- Postfix and Dovecot use the hostname's Let's Encrypt certificate for everyone, so it must include `mail.example.nl` for every domain. `mailctl certificate sync` adds the names that point to the server (below); `mailctl doctor` tells you when one is missing.

A new domain: `mailctl domain add example.nl`, then `mailctl dns publish example.nl` (or publish the records by hand when the domain isn't at TransIP), `mailctl certificate sync`, and check with `mailctl doctor example.nl`.

Every address record it publishes is the server's IPv4 address. A name is published to be reached, and an address nothing answers on costs each mail program and browser a wait before it falls back — a server whose web listeners are on IPv4, which is how OpenLiteSpeed comes, makes an `AAAA` record a delay on every first connection, and Outlook gives up on it altogether. An IPv6-only server publishes its `AAAA` record, since then that is the only way to it. SPF is a different matter and still allows both addresses: mail leaves over IPv6 from a server that has it, whatever the names point to.

`mailctl doctor` looks past the records themselves: it connects to every address these names point to, on the port that name is for, and warns about the ones where nothing answers. That is what a record left over from another server looks like, and it is invisible in DNS.

`dns publish` only touches the mail records: it replaces the MX, `mail`, DKIM and SRV records, removes a previous provider's `autoconfig` and `autodiscover` records, adds the server's addresses to an SPF record that doesn't allow the server yet (so the senders it allows keep working), and leaves an existing DMARC policy and every other record alone. A domain like `shop.example.nl` goes into the `example.nl` zone.

### The certificate for mail programs

Postfix and Dovecot present one certificate to everyone, so it has to hold every name mail programs connect to: the server's hostname and `mail.DOMAIN` of each domain. `mailctl certificate sync` keeps it that way. It asks certbot for a certificate with those names under the hostname's name, and afterwards Postfix and Dovecot read it again, without a restart. A name is only taken along once `mail.DOMAIN` points to this server; the others are named in the output, so publish their records first (`mailctl dns publish example.nl`).

Let's Encrypt checks each name over HTTP. Answering that is all an OpenLiteSpeed template of mailctl's does: the playbook installs it, its web root (`/var/www/mailcert`) holds nothing but the challenges, and each name is a member, so they show up in WebAdmin under Virtual Host Templates. A name another site in OpenLiteSpeed already answers for is left to that site, with a warning.

Once `domainctl hostname` has made the site of the server's own name, the virtual host `Server`, that site answers Let's Encrypt for the hostname and mailctl's template doesn't. Both commands keep the names the other one put in the certificate. Whichever runs first on a new server takes the empty placeholder files away, which certbot would otherwise refuse.

Run `mailctl certificate sync` on the server after adding or removing a mail domain. It only asks certbot when a name is missing or the certificate is about to expire. Certbot renews it by itself after that, and its deploy hook reloads Postfix and Dovecot, so mail programs never get an expired certificate. A playbook run doesn't do it: a certificate that is refused, or a name that no longer points here, is something to see while you are looking.

### Is spam still being filtered?

Postfix hands every message to spamass-milter, which asks spamd to score it and lets the message through untouched when spamd doesn't answer. A SpamAssassin that starts but can't do its work therefore breaks nothing anybody notices: mail keeps arriving, unmarked and unfiltered, and only the missing `X-Spam-` headers of a message already delivered show it. That is what happened on a server whose settings live in MariaDB but which had no `libdbd-mysql-perl`: every scan ended in `Error fetching user preferences via SQL`.

`mailctl doctor` now puts a test message down that same road -- the body every SpamAssassin scores 1000 by definition -- and says so when it comes back unscored, or when Postfix hands mail to no spam filter at all.

### Teaching the spam filter

SpamAssassin's Bayes half only helps when it is shown what spam and good mail look like here, and nobody has the time to do that by hand. They don't have to: every account's `Junk` folder is what people called spam themselves, and everything they keep elsewhere is what they didn't. `mailctl spam learn` hands both to `sa-learn`, which writes what it learns to the shared `bayes` database -- `bayes.cf` overrides the username, so every account teaches the same one.

A `mailctl-spam-learn.timer` runs it every night, so nothing has to be remembered. It only reads the messages that arrived or were moved since the last run, which keeps it short on a server with years of mail; `--all` reads everything again (`sa-learn` skips what it has already seen), and `--dry-run` says how much it would read. Mail nobody has opened yet is left out of the **inbox**, since it may be spam that hasn't been filed; a message filed in a folder counts whether it was read or not, because somebody put it there. That is also how a message marked as spam wrongly is put right: drag it out of `Junk` into the folder it belongs in, and the next run learns it as good mail, wherever it landed. `Trash` and `Drafts` teach nothing and are left out.

### Webmail

Webmail, calendar and contact sync, and ActiveSync are at `https://webmail.DOMAIN` for every mail domain whose `webmail.DOMAIN` A and AAAA records all point to the server. `mailctl webmail sync` does all of it:

- A domain at TransIP without any `webmail.DOMAIN` record gets one, pointing to the server; a record that's there is left alone. It waits (up to 5 minutes) until TransIP's nameservers serve the new record. This needs the TransIP login (`mailctl dns credentials`); `--no-dns` skips it.
- Each domain whose `webmail.DOMAIN` points to the server gets a site: a virtual host in OpenLiteSpeed, its settings in `conf/vhosts/webmail.DOMAIN/vhconf.conf`, with a Let's Encrypt certificate of its own that certbot gets through the site.
- Sites whose record no longer points here, or whose domain is gone, are removed with their certificates.

Run it yourself, on the server, after adding a domain or changing a `webmail.DOMAIN` record. A playbook run never does: it publishes DNS records, asks certbot for certificates, and takes sites away again, which is not something to discover afterwards. A run says so and names the command. `mail.DOMAIN` stays the name for IMAP and sending.

`mailctl domain add`, `dns show` and `dns publish` include the `webmail.DOMAIN` records and the `_caldavs._tcp` and `_carddavs._tcp` SRV records, which point calendar and contact apps at `https://webmail.DOMAIN` by themselves. `mailctl doctor` shows how a domain's webmail is doing, and `mailctl domain delete` takes its site and certificate along.

Webmail carries the colours and the logo of whoever runs the server. `mailctl webmail brand --colour '#09526D' --logo logo.png --bar-logo logo-white.png --icon icon.ico` puts them in place; without arguments it says what is set, and `--clear` puts SOGo's own back. The sites serve these files by the names SOGo's pages ask for, so an upgrade of SOGo doesn't undo them, and the colour is SOGo's own stylesheet with its palette recoloured: the colour given takes the place of SOGo's green-teal exactly, and every shade of it moves along, keeping how far it stood from white. Colours that say something -- the red of a warning, the green of the save button -- keep their own hue. The files the playbook installs are in [`roles/mail/files/webmail-branding`](roles/mail/files/webmail-branding); `mailctl webmail sync` makes the stylesheet again whenever SOGo's own is newer, which it is after an upgrade.

Calendars, address books and SOGo's settings live in the MariaDB database `sogo`: back it up along with the mail.

### Mail filters

An account's filters are Sieve scripts, and Dovecot runs **one** of them: the active one. Webmail (SOGo) is where filters are edited: it keeps them in its own database, renders them into the script named `sogo` and makes that one active, so a filter kept in any other script stops running the moment someone saves filters in webmail.

That is why filters that come in with `mailctl import` are put in webmail's list, and run from webmail's own script straight away. A rule webmail's editor can't express -- an away message, a test on a header it doesn't offer -- is named with the reason and left in a script of its own, which doesn't run while webmail's filters do. `mailctl filters show ADDRESS` shows every script an account has and marks the active one. `mailctl filters import mailserver.json` takes the filters, and nothing else, out of the file [`mailctl export`](#moving-a-whole-mail-server) writes on the other server: for accounts that are already here, or whose mail came over by other means. It puts their rules in webmail's list the same way, writes webmail's own script so they run at once, and leaves a filter an account already has under that name alone unless `--replace`. Add an address to take one account's filters only, and `--dry-run` to see what it would do. Webmail names the flags its editor offers `seen` and `flagged`, and refuses to save **anything** in its editor while one filter holds a flag under its Sieve name (*Sieve generation failure: Action with invalid flag argument*); `mailctl filters repair` gives them the names webmail wants, for one account or for all of them. Filters only sort mail as it arrives, so mail that came in before them stays where it is; `mailctl filters run ADDRESS` runs the account's active filter over a folder that is already there (the inbox unless `--folder` says otherwise) with Dovecot's own `sieve-filter`, and `--dry-run` says what it would move before anything is touched. After the account's own filter, the server's own runs for everyone: it files what SpamAssassin marked in `Junk`.

### Autoconfig and autodiscover

`mailctl autodiscover publish example.nl` sets up a site at `autodiscover.example.nl`, with `autoconfig.example.nl` as alias, which gives Thunderbird (autoconfig) and Outlook (autodiscover) the settings for `mail.example.nl`, and publishes both names at TransIP. A phone asking the same site for ActiveSync is sent to `https://webmail.example.nl/Microsoft-Server-ActiveSync`, which is where SOGo serves it: `mail.example.nl` has no web site at all, so a phone left to guess it is answered with a 404. The site is a member of an OpenLiteSpeed virtual host template the playbook installs, so it shows up in WebAdmin under Virtual Host Templates; mailctl adds the templates to `httpd_config.conf` the first time.

The site goes on HTTP first, since that is where Let's Encrypt checks a name and no mail program should meet another site's certificate in the meantime. The command then waits until both names point to this server — up to two minutes, for the records it has just published — asks certbot for the certificate itself, and switches the site to HTTPS.

When a name doesn't point here yet, or Let's Encrypt refuses, it says which and leaves the site on HTTP, with the command for the step it couldn't take:

```bash
certbot certonly --webroot -w /var/www/mailautodiscover --cert-name autodiscover.example.nl -d autodiscover.example.nl -d autoconfig.example.nl
mailctl autodiscover publish example.nl   # again: switches HTTPS on
```

Before certbot is asked, mailctl checks that OpenLiteSpeed really answers for the names on port 80, since a refused validation counts against Let's Encrypt's limits. An address where nothing answers at all is reported, not an error: Let's Encrypt falls back to the other address family, which is what happens on a server whose listeners are on IPv4 while the names also have AAAA records. An address where *another* site answers does stop it, because Let's Encrypt takes that answer and fails the validation.

Every certificate mailctl asks for has an RSA key, though certbot makes an elliptic-curve one by default: these are served to mail programs and to Microsoft's own ActiveSync servers, which phones using Outlook go through, and those refuse a certificate they see no RSA cipher suite for. Certbot renews the certificate by itself, and its deploy hook restarts OpenLiteSpeed so it uses the new one. `mailctl doctor` shows how a domain's site is doing, and `mailctl domain delete` takes the site along. `dns publish` keeps the two records of a domain that has the site, and removes a previous provider's for the other domains.

The first time, `dns publish` asks for the TransIP login and a key pair's private key (`mailctl dns credentials` enters or replaces them). Make the key pair in TransIP's control panel, under the API settings of your account, and put the server's addresses on its whitelist; mailctl shows them. It checks that TransIP accepts the key and saves both in `/etc/mailctl` (`transip.json` and `transip.key`, readable only by root), where Ansible leaves them alone. Try it without changing anything: `mailctl dns publish example.nl --dry-run`.

Every command asks for what you leave out. In scripts, pass everything on the command line and give passwords through standard input, never as an argument:

```bash
mailctl address add info@example.nl --password-stdin < password.txt
mailctl address delete info@example.nl --yes --keep-mail
```

Moving a server brings tens of accounts whose passwords are already known. `mailctl address import accounts.json` creates them from a file of addresses with their passwords, or of entries with an `address` and a `password`:

```json
{"info@example.nl": "a password", "sales@example.nl": "another one"}
```

The file is read and checked whole first, so a mistake in it is found before any account exists; an address that is already there is left as it is, and every domain has to be on this server already. `--dry-run` checks the file and creates nothing. Delete the file afterwards: it holds the passwords in plain text, and mailctl says so when others can read it.

### Moving a whole mail server

Dovecot indexes mail for IMAP search as it arrives, and a playbook run queues everything already there. Mail copied onto the server after that is indexed on the first search in a folder, unless you queue it yourself: `doveadm index -A -q '*'`.

The mail role runs on Ubuntu 24.04 (Dovecot 2.3) and 26.04 (Dovecot 2.4). A mail server can't be upgraded from one release to the other in place: the upgrade keeps the Dovecot configuration this role writes and Dovecot 2.4 refuses it. Set up a new server and move the mail across.

`mailctl import mailserver.json` brings over a whole mail server at once: its domains, its accounts with their passwords, its forwards (and who may send as the forwarded address), its spam settings and every account's Sieve filters. Write the file on the old server with `mailctl export mailserver.json`, or, on a server without mailctl, with [`roles/mail/files/export-mailserver.sh`](roles/mail/files/export-mailserver.sh): copy it there and run `sudo ./export-mailserver.sh mailserver.json`. It reads the same tables this role sets up, needs `jq`, and changes nothing. `--domain example.nl` writes one domain instead of the whole server: its accounts, its forwards, the spam settings of the domain and its accounts, and their filters, without the settings of the server itself.

Importing the same file again creates nothing twice: a domain, account or forward that is already here is named and skipped, a spam setting for the whole server is only added where this server has none, and filters are only put in for the accounts the import creates. So a run that stopped halfway can simply be run again, and the file can be imported on a server that already holds part of it.

```json
{
  "version": 1,
  "domains": ["example.nl"],
  "addresses": [{"address": "info@example.nl", "password_hash": "{SHA512-CRYPT}$6$..."},
                {"address": "sales@example.nl", "password": "a password"}],
  "forwards": [{"source": "sales@example.nl", "destination": "info@example.nl", "send_as": true}],
  "spam": [{"target": "server", "setting": "required_score", "value": "5"},
           {"target": "example.nl", "setting": "welcomelist_from", "value": "*@partner.nl"}],
  "sieve": [{"address": "info@example.nl", "name": "roundcube", "active": true, "content": "require ..."}]
}
```

Every part may be left out. Accounts keep the password hash the old server has, so nobody needs a new password; a password in plain text works too. Hashes are kept as they are, with their scheme in front so webmail reads them too. For every account whose hash isn't SHA512-CRYPT, like this server's, the import asks for a new password in a terminal, and Enter keeps the hash; `--keep-hashes` doesn't ask. Weak hashes, like MD5-CRYPT, are pointed out. An account whose hash Dovecot here can't read needs a new password, or is skipped. What's already on this server is summed up in one line; `--verbose` names it. The whole file, and every filter's syntax, is checked before anything is created. Domains and accounts that are already on this server are left as they are, and so are their spam settings and filters; a spam setting for the whole server is only added where this server has none. Running the import again carries on where it stopped. `--dry-run` only checks the file. New domains get a DKIM key; publish their DNS records afterwards (`mailctl dns show` or `mailctl dns publish`). The file holds password hashes, so the export makes it readable by root only; delete it once it's imported.

The code is in `roles/mail/files/mailctl`. Its tests start a throwaway MariaDB or MySQL server and run against the library versions of both Ubuntu releases:

```bash
cd roles/mail/files/mailctl
python3 -m venv /tmp/mailctl-24.04
/tmp/mailctl-24.04/bin/pip install -r tests/requirements-24.04.txt
MAILCTL_REQUIRE_DB=1 /tmp/mailctl-24.04/bin/pytest tests
```

Do the same with `tests/requirements-26.04.txt` (ideally on Python 3.14) for Ubuntu 26.04. The tests use the database server installed on your machine. To use another one, like MariaDB from Ubuntu's `mariadb-server-core` and `mariadb-client-core` packages (download both with `apt download` and unpack them into one folder with `dpkg -x`), point `MAILCTL_TEST_DB_PREFIX` at that folder. Without `MAILCTL_REQUIRE_DB=1` the database tests are skipped when there's no server; with it they fail. Database servers refuse to run as root, so run the tests as a normal user.

## Moving a server over

For moving a hand-built server onto one built with this repository. Run it from the old server, as root; the new server only has to be built.

1. Copy [`shared/migrate/migrate.py`](shared/migrate/migrate.py) to the old server.
2. On the old server, as root: `python3 migrate.py key`. It prints a key. Paste that line into `/home/ansible/.ssh/authorized_keys` on the new server (once).
3. On the old server, as root: `python3 migrate.py`. That is the tool. It asks for the new server's name the first time and writes it down, looks around, and shows what it found, grouped into mail, mailboxes, websites and cron jobs, with what the new server already has next to each. Only what can be moved has a number.
4. It then stays up and asks what to do: the numbers to move (ranges like `2-5`, commas and `all` work), `r` to look around again, `q` or Ctrl-D to leave. It says what each pick is and asks to go ahead before it starts; after a move it prints the report of that move, looks again and asks again. One run moves as much as you like.
5. Each report says what moved, how many messages arrived in each mailbox, the DNS records for it, and what you still have to do by hand.

The commands do one thing each and stop, which is what a script wants: `migrate.py list --host <new-server>` shows the list and changes nothing; `migrate.py move 1-3 --host <new-server>` moves what the numbers name; `migrate.py check --host <new-server>` says which server answered, whether `mailctl` and `domainctl` are there and where it keeps mail; `migrate.py report` prints the report for what has been done so far, from its notes, and `migrate.py report --all` adds the lists about the whole server (sites that are not WordPress, databases and cron jobs that belong to no site, what could not be read), which `list` also shows. `migrate.py forget` forgets the new server's name, so it is asked for again; `--host` on any command replaces it. `--no-colour` leaves the colour off, as `NO_COLOR` and a pipe do.

The old server's own webmail, autodiscover and autoconfig addresses are not listed as sites: the new server has its own. Cron jobs that belong to no site, like the system's own, are counted in one line.

It moves:

- Mail domains, with their accounts and passwords, forwards, filters, and the spam settings of the domain and of each account.
- The mail messages themselves, mailbox by mailbox: the maildir is copied over the connection it already has, with no password anywhere. It checks where the old server really keeps that account's mail (`doveconf`, then `/var/vmail/<domain>/<user>/Maildir` and the home folder itself) instead of assuming; mail kept as mdbox or mbox is named and not copied. Where it goes comes from what the new server answers, never from a path in the file. Afterwards the new server gives the maildir to the user Dovecot reads it as, runs `doveadm force-resync` and counts the messages, and that count is what the report says. One mailbox is one pick: a 67 GB mailbox is not something to start by accident. A mailbox can only go once its domain has: until then it says "move its domain first".
- WordPress sites, with their files and database.
- The cron jobs that belong to a site, with their paths and their PHP rewritten for the new server. A job that uses something outside the site is shown and skipped.

It does not move:

- Sites that are not WordPress.
- DNS. The report prints the records each mail domain needs, and the addresses a site's A and AAAA records must point at.
- The server's own spam settings. The new server keeps its own; the report lists the old server's.

The report ends with what is still yours to do by hand: catch-all addresses and send-as permissions that could not move, sites that are not WordPress, databases and cron jobs that belong to no site, and anything the tool could not read.

`--domain example.nl` (for `list` and `move`) looks at one mail domain and its sites only. `--user name`, with `--domain`, gives that site another Linux user on the new server: two domains of one company, like `example.nl` and `example.com`, would otherwise come down to the same user, and the second is shown and not offered. `migrate.py again example.nl` is for a site that has already moved: it copies the files once more and replaces the new server's database, after asking. `migrate.py again info@example.nl` does the same for one mailbox, and that is the only way to a copy that also removes what the new server has and the old one hasn't; the question says so before it happens. Copying a mailbox the ordinary way only ever adds and replaces. Pick what has moved again as often as you like.

Once the DNS of a site points at the new server, run `domainctl add <domain>` on the new server to get its certificate. Until the DNS moves, both servers run the same cron jobs.

On the old server it writes four files: its key pair, `migrate_key` and `migrate_key.pub`, and `migrate_known_hosts`, all in `~/.ssh` of the user running it, and `/var/lib/migrate/state.json`.

## Creating password hashes

Never store plaintext passwords. Generate a hash with:

```bash
mkpasswd --method=sha-512
```

Paste the result into `linuxusers.yml` as the `password` field.
