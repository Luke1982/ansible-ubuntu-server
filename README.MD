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

Copy `inventory-example.yml` to `private/<hostname>.yml` and fill in the IP, hostname, DNS servers, and the path to your SSH key. See `private/README.md` for the full structure.

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

### 5. Create a playbook

Create `<hostname>.yml` in the project root. Include only the roles that apply to that server:

```yaml
---
- name: Deploy <hostname>
  become: yes
  hosts: webservers
  roles:
    - basic
    - web
  vars_files:
    - roles/basic/vars/private/linuxusers.yml
```

### 6. Run the playbook

```bash
ansible-playbook -i private/<hostname>.yml <hostname>.yml
```

No password prompts — Ansible authenticates via the key in `ansible_ssh_private_key_file`, and `become` uses passwordless sudo.

---

## Project structure

Each server has its own playbook (e.g. `web01.whservice.nl.yml`) in the project root. Private inventory files live in `private/` and private variable files live under each role's `vars/private/`. Neither directory is committed to git.

## Roles

| Role | What it does |
|------|-------------|
| `basic` | Hostname, base packages, unattended-upgrades, Linux users, SSH hardening, firewall, fail2ban |
| `web` | OpenLiteSpeed, PHP 8.3, Composer (global), nvm (system-wide), certbot, Let's Encrypt directory |
| `db` | MariaDB, phpMyAdmin |
| `mail` | Postfix, Dovecot, OpenDKIM, SpamAssassin, Roundcube, fail2ban jails for SMTP brute force |

## Creating password hashes

Never store plaintext passwords. Generate a hash with:

```bash
mkpasswd --method=sha-512
```

Paste the result into `linuxusers.yml` as the `password` field.
