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
- OpenLiteSpeed needs a listener on port 443. The playbook adds a few `include` lines to `/usr/local/lsws/conf/httpd_config.conf` for the webmail sites, between `# BEGIN webmail` and `# END webmail` markers; keep them when you change that file by hand.
- A site you set up by hand in OpenLiteSpeed for Roundcube stops working: remove it.

Webmail, calendar and contact sync and ActiveSync are at `https://webmail.DOMAIN` for every mail domain whose `webmail.DOMAIN` A and AAAA records all point to the server. `mailctl webmail sync` sets up each such site with a Let's Encrypt certificate of its own; it runs every day and at the end of every playbook run, so a new domain gets its webmail within a day of its records being published. `mail.DOMAIN` stays the name for IMAP and sending.

Roundcube's database stays, with the contacts people saved in it. Filters made in Roundcube keep working until someone saves filters or a vacation reply in SOGo; from then on SOGo's apply. `mailctl filters show ADDRESS` shows both.

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

---

## Project structure

Each server has its own playbook (e.g. `web01.whservice.nl.yml`) in the project root, which targets that server by name. Each server also has an inventory file in `private/`, and `ansible.cfg` loads all of them as one inventory. Private variable files live under each role's `vars/private/`. None of these private files are committed to git.

## Roles

| Role | What it does |
|------|-------------|
| `basic` | Hostname, base packages, unattended-upgrades, Linux users, SSH hardening, iptables firewall (applied at boot), fail2ban |
| `web` | OpenLiteSpeed with LiteSpeed's PHP 8.3, PHP-FPM and CLI from Ubuntu (8.3 on Ubuntu 24.04, 8.5 on 26.04), Composer (global), nvm (system-wide), certbot, Let's Encrypt directory |
| `db` | MariaDB, phpMyAdmin |
| `mail` | Postfix, Dovecot with full-text IMAP search (fts-xapian) and login tracking, OpenDKIM, SpamAssassin, SOGo (webmail, calendars, address books and ActiveSync), per-user sending limits (postfwd), fail2ban jails for SMTP, IMAP and webmail brute force, and `mailctl` (below) |

## Managing mail with mailctl

Mail servers get `mailctl`, a command-line tool for the mail domains, addresses, forwards, DKIM keys and spam settings. Run it as root on the server; `mailctl --help` lists everything, and `--help` on a command explains it with an example.

| Command | What it does |
|---------|-------------|
| `mailctl domain add example.nl` | Adds a domain, creates its DKIM key and shows the DNS records to publish |
| `mailctl address add info@example.nl` | Creates an account; asks for the password |
| `mailctl forward add sales@example.nl info@example.nl` | Forwards mail for an address |
| `mailctl status info@example.nl` | Folders, last logins, sending against the limits, recent bounces |
| `mailctl status example.nl` | Checks the domain's MX, SPF, DKIM and DMARC records |
| `mailctl spam set example.nl required_score 4` | Changes a spam setting for an account, a domain or the whole server |
| `mailctl filters show info@example.nl` | Shows an account's mail filters |
| `mailctl webmail sync` | Sets up webmail at `https://webmail.DOMAIN` for the domains whose `webmail.DOMAIN` points here, and removes it for the others. Runs daily by itself |

Last logins are recorded for IMAP, which webmail and phones on ActiveSync use too; accounts that only send mail show no logins.

Every command asks for what you leave out. In scripts, pass everything on the command line and give passwords through standard input, never as an argument:

```bash
mailctl address add info@example.nl --password-stdin < password.txt
mailctl address delete info@example.nl --yes --keep-mail
```

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
