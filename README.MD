# Ansible server setup

Automated provisioning of Ubuntu servers with roles for base configuration, web hosting, database, and mail.

## Getting started

### 1. Prepare the target server

On a fresh Ubuntu server, create the `ansible` user and grant it passwordless sudo:

```bash
adduser ansible
usermod -aG sudo ansible
echo "ansible ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/ansible
```

Copy your SSH key to the server:

```bash
ssh-copy-id ansible@<server-ip>
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

#### Migrating learned spam to a new mail server

The `mail` role keeps SpamAssassin's Bayes learning in the MySQL `bayes`
database. A fresh provision starts with an empty Bayes filter. To carry over the
learning from an existing server, dump it on the old host
(`mysqldump bayes > bayes_dump.sql`) and drop the file at
`roles/mail/files/private/bayes_dump.sql` before running the playbook — it will
be imported automatically. See `roles/mail/files/private/README.md` for details.

#### Search index for migrated mailboxes

Dovecot indexes mail for IMAP search when it arrives. The playbook run that
changes the Dovecot config also queues all existing mail for indexing. If you
copy mailboxes onto the server afterwards, queue them yourself, so the first
search in each folder doesn't have to index it:

```bash
doveadm index -A -q '*'
```

#### DKIM after the first run with mailctl

The playbook used to rebuild OpenDKIM's signing table on every run, leaving out every domain but the server's own, and gave the server's own domain a new DKIM key each time. The first run with `mailctl` signs every domain that has a key again, and keys are never replaced any more. Check each domain, the server's own included, with `mailctl status DOMAIN`, and publish the record from `mailctl dkim show DOMAIN` where the DKIM check fails.

#### From Roundcube to SOGo

The mail role installs SOGo for webmail, calendars, address books and phone sync (ActiveSync), and removes Roundcube from servers that have it. Before running it:

- Add `sogo_db_pass` to `mailvars.yml` (see `roles/mail/vars/private/README.md`).
- OpenLiteSpeed needs listeners on ports 80 and 443. mailctl adds a virtual host per domain to its config, `webmail.DOMAIN`, with the note "Managed by mailctl"; leave those to mailctl. It doesn't touch your other sites, and WebAdmin stays editable.
- A site you set up by hand in OpenLiteSpeed for Roundcube stops working: remove it.

Webmail, calendar and contact sync, and ActiveSync are at `https://webmail.DOMAIN` for every mail domain whose `webmail.DOMAIN` A and AAAA records all point to the server. `mailctl webmail sync` does all of it:

- A domain at TransIP without any `webmail.DOMAIN` record gets one, pointing to the server; a record that's there is left alone. It waits (up to 5 minutes) until TransIP's nameservers serve the new record. This needs the TransIP login (`mailctl dns credentials`); `--no-dns` skips it.
- Each domain whose `webmail.DOMAIN` points to the server gets a site: a virtual host in OpenLiteSpeed, its settings in `conf/vhosts/webmail.DOMAIN/vhconf.conf`, with a Let's Encrypt certificate of its own that certbot gets through the site.
- Sites whose record no longer points here, or whose domain is gone, are removed with their certificates.

Run it yourself, on the server, after adding a domain or changing a `webmail.DOMAIN` record. A playbook run never does: it publishes DNS records, asks certbot for certificates, and takes sites away again, which is not something to discover afterwards. A run says so and names the command. `mail.DOMAIN` stays the name for IMAP and sending.

`mailctl domain add`, `dns show` and `dns publish` include the `webmail.DOMAIN` records and the `_caldavs._tcp` and `_carddavs._tcp` SRV records, which point calendar and contact apps at `https://webmail.DOMAIN` by themselves. `mailctl doctor` shows how a domain's webmail is doing, and `mailctl domain delete` takes its site and certificate along.

Calendars, address books and SOGo's settings live in the MariaDB database `sogo`: back it up along with the mail.

Roundcube's database stays, with the contacts people saved in it. Filters made in Roundcube keep working until someone saves filters or a vacation reply in SOGo; from then on SOGo's apply. `mailctl filters show ADDRESS` shows both.

Moving an account from another server: tar its sieve directory there (`tar czf sieve.tar.gz -C /var/vmail/example.nl/info sieve .dovecot.sieve`) and import it here:

```bash
mailctl filters import info@example.nl sieve.tar.gz --dry-run   # what it would do
mailctl filters import info@example.nl sieve.tar.gz
```

It takes the `.sieve` files from the archive and makes active the one `.dovecot.sieve` pointed to. Filters the account already has are kept, unless you add `--replace`. Filters imported from Roundcube stop applying as soon as that user saves filters or a vacation reply in SOGo.

#### Ubuntu release upgrades

The mail role supports Ubuntu 24.04 (Dovecot 2.3) and Ubuntu 26.04 (Dovecot 2.4), but not upgrading a mail server from one to the other in place: the package upgrade keeps the Dovecot config files the role changed, and Dovecot 2.4 doesn't accept them. Set up a new 26.04 server with the playbook and move the mail and databases to it instead.

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

A playbook only runs on the host it names. If that name is not in any file in `private/`, Ansible warns that no hosts matched and does nothing.

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

Until `mailctl certificate sync` has run, a new mail server has only the empty
certificate files the playbook puts there for Dovecot to start with.

---

## Project structure

Each server has its own playbook (e.g. `web01.whservice.nl.yml`) in the project root, which targets that server by name. Each server also has an inventory file in `private/`, and `ansible.cfg` loads all of them as one inventory. Private variable files live under each role's `vars/private/`. None of these private files are committed to git.

## Roles

| Role | What it does |
|------|-------------|
| `basic` | Hostname, base packages, unattended-upgrades, Linux users, SSH hardening, iptables firewall (applied at boot), fail2ban |
| `web` | OpenLiteSpeed with LiteSpeed's PHP 8.3, PHP-FPM and CLI from Ubuntu (8.3 on Ubuntu 24.04, 8.5 on 26.04), Composer (global), nvm (system-wide), certbot, Let's Encrypt directory, the shared virtual host template, and `domainctl` (below) |
| `db` | MariaDB, phpMyAdmin on a site of its own at `pma.<hostname>` |
| `mail` | Postfix, Dovecot with full-text IMAP search (fts-xapian) and login tracking, OpenDKIM, SpamAssassin, SOGo (webmail, calendars, address books and ActiveSync), per-user sending limits (postfwd), fail2ban jails for SMTP, IMAP and webmail brute force, and `mailctl` (below) |

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

On servers that already had phpMyAdmin, the first run moves it out of
OpenLiteSpeed's Example site, where every name that site answers to could reach
it, including the server's own address. It keeps the settings it had, and gives
it the passphrase for cookie login that the sample settings don't have:
phpMyAdmin used to log everybody out whenever it restarted, and say so on every
page. The settings it replaces are kept as `config.inc.php.previous`.

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

## Managing web sites with domainctl

Web servers get `domainctl`, a command-line tool for the sites on the server. Run it as root; `domainctl --help` lists everything, and `--help` on a command explains it with an example.

| Command | What it does |
|---------|-------------|
| `domainctl add DOMAIN` | Sets a site up: its Linux user, directories, permissions, virtual host and certificate |
| `domainctl list` | The sites with their domain, user and certificate |
| `domainctl check [USER]` | Whether a site's names, permissions and certificate are in order |
| `domainctl delete USER` | Removes a site; `--purge` takes the user and its files too |
| `domainctl sync` | Brings the template block and every site's permissions up to date. Run it by hand; the playbook only says to |
| `domainctl dns credentials` | The TransIP login, shared with mailctl |

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
2. Looks the domain and `www.DOMAIN` up **in real DNS**, because that is what Let's Encrypt checks. The domain has to point here, and only here. A name with no record at all is offered to TransIP, if the domain is in that account and uses TransIP's nameservers; it then waits until the nameservers serve it.
3. `www` is included only when it points here. A name that doesn't resolve here would fail the whole certificate request and take the bare domain down with it, so it is left out with a warning instead. Run the command again once the record is right.
4. Makes the user, its home directory, `public_html/www` and `logs`, with a placeholder page, and sets the ACL entries above.
5. Adds the member to the template and restarts OpenLiteSpeed.
6. Checks that OpenLiteSpeed really answers for the domain on port 80, by fetching a file of its own from the challenge folder at each of the server's addresses. A failed Let's Encrypt validation counts against their limits; this check does not.
7. Switches the redirect to HTTPS off in the template, runs certbot as a test and then for real (`certonly --webroot`), and switches the redirect back on — always, also when certbot fails.

Run it again for the same domain whenever something stopped it: it picks up where it left off, and adds `www` to the certificate once that record points here.

Nothing is rolled back when certbot fails: the site stays up over HTTP and the message says what Let's Encrypt refused. Fix that and run `domainctl add DOMAIN` again, or take it away with `domainctl delete USER`.

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
| `mailctl forward add sales@example.nl info@example.nl` | Forwards mail for an address |
| `mailctl status info@example.nl` | Folders, last logins, sending against the limits, recent bounces |
| `mailctl status example.nl` | Checks the domain's DNS records and whether the certificate includes `mail.example.nl` |
| `mailctl dns show example.nl` | Shows the DNS records the domain needs |
| `mailctl dns publish example.nl` | Publishes those records at TransIP; shows the changes and asks first (`--dry-run` only shows them) |
| `mailctl doctor` | Checks the server (hostname, reverse DNS, certificate, spam filter) and every domain; exit status 1 when something is wrong |
| `mailctl autodiscover publish example.nl` | Lets Thunderbird and Outlook find the domain's mail settings by themselves (below) |
| `mailctl certificate sync` | Puts the hostname and every `mail.DOMAIN` that points here in one certificate, and makes Postfix and Dovecot read it (below). Run it by hand; the playbook only says to |
| `mailctl spam set example.nl required_score 4` | Changes a spam setting for an account, a domain or the whole server |
| `mailctl filters show info@example.nl` | Shows an account's mail filters |
| `mailctl filters import info@example.nl sieve.tar.gz` | Imports an account's filters from a tar of its sieve directory on another server |
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

Run `mailctl certificate sync` on the server after adding or removing a mail domain. It only asks certbot when a name is missing or the certificate is about to expire. Certbot renews it by itself after that, and its deploy hook reloads Postfix and Dovecot, so mail programs never get an expired certificate. A playbook run doesn't do it: a certificate that is refused, or a name that no longer points here, is something to see while you are looking.

### Is spam still being filtered?

Postfix hands every message to spamass-milter, which asks spamd to score it and lets the message through untouched when spamd doesn't answer. A SpamAssassin that starts but can't do its work therefore breaks nothing anybody notices: mail keeps arriving, unmarked and unfiltered, and only the missing `X-Spam-` headers of a message already delivered show it. That is what happened on a server whose settings live in MariaDB but which had no `libdbd-mysql-perl`: every scan ended in `Error fetching user preferences via SQL`.

`mailctl doctor` now puts a test message down that same road -- the body every SpamAssassin scores 1000 by definition -- and says so when it comes back unscored, or when Postfix hands mail to no spam filter at all.

### Mail filters

An account's filters are Sieve scripts, and Dovecot runs **one** of them: the active one. Webmail keeps its filters in a script of its own and makes that one active whenever they are saved there, so filters imported with `mailctl address import-sieve` stop running at that moment, and the other way round. `mailctl filters show` marks which one is active, and the import says which filter it takes over from. After the account's own filter, the server's own runs for everyone: it files what SpamAssassin marked in `Junk`.

### Autoconfig and autodiscover

`mailctl autodiscover publish example.nl` sets up a site at `autodiscover.example.nl`, with `autoconfig.example.nl` as alias, which gives Thunderbird (autoconfig) and Outlook (autodiscover) the settings for `mail.example.nl`, and publishes both names at TransIP. The site is a member of an OpenLiteSpeed virtual host template the playbook installs, so it shows up in WebAdmin under Virtual Host Templates; mailctl adds the templates to `httpd_config.conf` the first time.

The site goes on HTTP first, since that is where Let's Encrypt checks a name and no mail program should meet another site's certificate in the meantime. The command then waits until both names point to this server — up to two minutes, for the records it has just published — asks certbot for the certificate itself, and switches the site to HTTPS.

When a name doesn't point here yet, or Let's Encrypt refuses, it says which and leaves the site on HTTP, with the command for the step it couldn't take:

```bash
certbot certonly --webroot -w /var/www/mailautodiscover --cert-name autodiscover.example.nl -d autodiscover.example.nl -d autoconfig.example.nl
mailctl autodiscover publish example.nl   # again: switches HTTPS on
```

Before certbot is asked, mailctl checks that OpenLiteSpeed really answers for the names on port 80, since a refused validation counts against Let's Encrypt's limits. An address where nothing answers at all is reported, not an error: Let's Encrypt falls back to the other address family, which is what happens on a server whose listeners are on IPv4 while the names also have AAAA records. An address where *another* site answers does stop it, because Let's Encrypt takes that answer and fails the validation.

Certbot renews the certificate by itself, and its deploy hook restarts OpenLiteSpeed so it uses the new one. `mailctl doctor` shows how a domain's site is doing, and `mailctl domain delete` takes the site along. `dns publish` keeps the two records of a domain that has the site, and removes a previous provider's for the other domains.

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

The code is in `roles/mail/files/mailctl`. Its tests start a throwaway MariaDB or MySQL server and run against the library versions of both Ubuntu releases:

```bash
cd roles/mail/files/mailctl
python3 -m venv /tmp/mailctl-24.04
/tmp/mailctl-24.04/bin/pip install -r tests/requirements-24.04.txt
MAILCTL_REQUIRE_DB=1 /tmp/mailctl-24.04/bin/pytest tests
```

Do the same with `tests/requirements-26.04.txt` (ideally on Python 3.14) for Ubuntu 26.04. The tests use the database server installed on your machine. To use another one, like MariaDB from Ubuntu's `mariadb-server-core` and `mariadb-client-core` packages (download both with `apt download` and unpack them into one folder with `dpkg -x`), point `MAILCTL_TEST_DB_PREFIX` at that folder. Without `MAILCTL_REQUIRE_DB=1` the database tests are skipped when there's no server; with it they fail. Database servers refuse to run as root, so run the tests as a normal user.

## Creating password hashes

Never store plaintext passwords. Generate a hash with:

```bash
mkpasswd --method=sha-512
```

Paste the result into `linuxusers.yml` as the `password` field.
