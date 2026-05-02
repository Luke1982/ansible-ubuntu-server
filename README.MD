# Ansible server setup

## Prerequisites

- A fresh Ubuntu server (local VM or remote)
- An `ansible` user created on the server with a home directory
- Your SSH public key copied to that user's `~/.ssh/authorized_keys` (use `ssh-copy-id ansible@<ip>`)
- The `ansible` user has passwordless sudo (add to `/etc/sudoers.d/ansible`: `ansible ALL=(ALL) NOPASSWD: ALL`)
- `community.general` collection installed locally: `ansible-galaxy collection install -r requirements.yml`

## Project structure

Each server has its own playbook (e.g. `web01.whservice.nl.yml`) and each domain has its own private inventory file in `private/`. Neither is committed to git.

## Setting up a new server

### 1. Create a private inventory file

Copy `inventory-example.yml` to `private/<hostname>.yml` and fill in the IP, hostname, dns_servers, and adjust `ansible_ssh_private_key_file` to point to the key you use to reach the `ansible` user.

### 2. Create a playbook

Create `<hostname>.yml` in the project root. Include only the plays that apply:

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

### 3. Create private var files

**`roles/basic/vars/private/linuxusers.yml`** — Linux users. See the README in that directory for the structure. This file is gitignored.

**`roles/web/vars/private/`** — Virtual hosts config (`apachevhosts.yml`). See the README in that directory.

**`roles/mail/vars/private/mailvars.yml`** — Mail server config. See the README in that directory.

### 4. Deploy

```bash
ansible-playbook -i private/<hostname>.yml <hostname>.yml
```

No password prompts — Ansible authenticates via the key in `ansible_ssh_private_key_file`, and `become` uses passwordless sudo.

## Roles

| Role | What it does |
|------|-------------|
| `basic` | Hostname, base packages, unattended-upgrades, users, SSH hardening, firewall, fail2ban |
| `web` | OpenLiteSpeed, PHP 8.3, Let's Encrypt directory, certbot |
| `db` | MariaDB, phpMyAdmin |
| `mail` | Postfix, Dovecot, OpenDKIM, SpamAssassin, Roundcube |

## Creating password hashes

Never store plaintext passwords. Generate a hash with:

```bash
mkpasswd --method=sha-512
```

Paste the result into `linuxusers.yml` as the `password` field.

## TODO

### Web
- Setup Zabbix

### Mail
- Setup Z-push (90% done)
- Setup webmail subdomains
