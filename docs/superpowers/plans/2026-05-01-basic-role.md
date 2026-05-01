# Basic Role Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all server-agnostic tasks from the `web` role into a new `basic` role, replace the weekly apt cron with `unattended-upgrades`, and add passwordless `sudo su` via `community.general.sudoers`.

**Architecture:** A new `basic` role handles hostname, base packages, user/group management (incl. sudoers), SSH hardening, and firewall. The `web` role retains only web-specific tasks. `site.yml` lists `[basic, web]` for webservers explicitly.

**Tech Stack:** Ansible 2.16, `community.general` collection (sudoers module, already installed at 8.3.0)

**Verify baseline before starting:**
```bash
cd /home/guido/nvme0n1p1/code/ansible-ubuntu-server
ansible-playbook --syntax-check -i inventory-example.yml site.yml
```
Expected output: `playbook: .../site.yml`

---

## File Map

### Created
- `requirements.yml` — collection dependency declaration
- `roles/basic/tasks/main.yml` — hostname, base packages, unattended-upgrades, includes
- `roles/basic/tasks/manage-users.yml` — groups, users, sudoers entries
- `roles/basic/tasks/setup-ssh.yml` — sshd drop-in config + AllowUsers
- `roles/basic/tasks/setup-firewall.yml` — iptables script + fail2ban
- `roles/basic/handlers/main.yml` — restart ssh, setup iptables dns, start firewall
- `roles/basic/vars/main.yml` — linux_groups (moved from web)
- `roles/basic/vars/private/linuxusers.yml` — linux_users (moved from web)
- `roles/basic/vars/private/README.md` — updated private vars docs
- `roles/basic/files/sshd_custom.conf` — moved from web
- `roles/basic/files/start_firewall.sh` — moved from web
- `roles/basic/files/20auto-upgrades` — unattended-upgrades apt config

### Modified
- `roles/web/tasks/main.yml` — stripped to web-only tasks
- `roles/web/handlers/main.yml` — cleared (handlers moved to basic)
- `roles/web/vars/main.yml` — linux_groups removed
- `roles/web/defaults/main.yml` — unused apache_* variables removed
- `site.yml` — add basic role + update vars_files path

### Deleted
- `roles/web/tasks/manage-users.yml`
- `roles/web/tasks/setup-firewall-ids.yml`
- `roles/web/files/sshd_custom.conf`
- `roles/web/files/start_firewall.sh`
- `roles/web/vars/private/linuxusers.yml`

---

## Task 1: requirements.yml and role skeleton

**Files:**
- Create: `requirements.yml`
- Create dirs: `roles/basic/tasks/`, `roles/basic/handlers/`, `roles/basic/vars/private/`, `roles/basic/files/`

- [ ] **Step 1: Create requirements.yml**

```yaml
collections:
  - name: community.general
```

Save to `requirements.yml`.

- [ ] **Step 2: Create basic role directories**

```bash
mkdir -p roles/basic/tasks roles/basic/handlers roles/basic/vars/private roles/basic/files
```

Expected: no output, directories created.

- [ ] **Step 3: Commit**

```bash
git add requirements.yml roles/basic/
git commit -m "scaffold: add basic role directory structure and requirements.yml"
```

---

## Task 2: Move vars and files into basic role

**Files:**
- Create: `roles/basic/vars/main.yml`
- Create: `roles/basic/vars/private/linuxusers.yml`
- Create: `roles/basic/vars/private/README.md`
- Create: `roles/basic/files/sshd_custom.conf`
- Create: `roles/basic/files/start_firewall.sh`
- Create: `roles/basic/files/20auto-upgrades`

- [ ] **Step 1: Create roles/basic/vars/main.yml**

```yaml
---
linux_groups:
  - name: "webusers"
    state: "present"
```

- [ ] **Step 2: Copy linuxusers.yml to basic role**

```bash
cp roles/web/vars/private/linuxusers.yml roles/basic/vars/private/linuxusers.yml
```

- [ ] **Step 3: Create roles/basic/vars/private/README.md**

```markdown
## In this directory
Should be the file:

- linuxusers.yml

## linuxusers.yml
Need the following structure:
```yaml
linux_users:
  - create_home: yes
    groups: "sudo"
    password: "$6$..."
    name: "username"
    state: "present"
    ssh_access: true
```
Where:
- `create_home` — whether to create a home directory, yes or no
- `groups` — comma-separated group string, e.g. "sudo" or "sudo,webusers"
- `password` — hashed password from `mkpasswd --method=sha-512`
- `name` — username
- `state` — present or absent
- `ssh_access` — true or false, controls AllowUsers in sshd
```

- [ ] **Step 4: Copy sshd_custom.conf to basic role**

```bash
cp roles/web/files/sshd_custom.conf roles/basic/files/sshd_custom.conf
```

- [ ] **Step 5: Copy start_firewall.sh to basic role**

```bash
cp roles/web/files/start_firewall.sh roles/basic/files/start_firewall.sh
```

- [ ] **Step 6: Create roles/basic/files/20auto-upgrades**

```
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
```

- [ ] **Step 7: Commit**

```bash
git add roles/basic/
git commit -m "feat(basic): add vars, private vars, and static files"
```

---

## Task 3: Create basic/handlers/main.yml

**Files:**
- Create: `roles/basic/handlers/main.yml`

- [ ] **Step 1: Create roles/basic/handlers/main.yml**

```yaml
---

- name: restart ssh
  service:
    name: ssh
    state: restarted

- name: setup iptables dns
  replace:
    path: /usr/local/bin/start_firewall.sh
    regexp: "PUT_DNS_SERVERS_HERE"
    replace: "{{ dns_servers }}"
  notify: start firewall

- name: start firewall
  command:
    cmd: bash /usr/local/bin/start_firewall.sh
```

- [ ] **Step 2: Syntax-check**

```bash
ansible-playbook --syntax-check -i inventory-example.yml site.yml
```

Expected: `playbook: .../site.yml` (site.yml still uses web only — this verifies no breakage yet)

- [ ] **Step 3: Commit**

```bash
git add roles/basic/handlers/main.yml
git commit -m "feat(basic): add handlers"
```

---

## Task 4: Create basic/tasks/manage-users.yml

**Files:**
- Create: `roles/basic/tasks/manage-users.yml`

- [ ] **Step 1: Create roles/basic/tasks/manage-users.yml**

```yaml
---

- name: Setup groups
  group:
    name: "{{ item.name }}"
    state: "{{ item.state }}"
  loop: "{{ linux_groups }}"

- name: Manage regular linux users
  user:
    name: "{{ item.name }}"
    state: "{{ item.state }}"
    groups: "{{ item.groups }}"
    password: "{{ item.password }}"
    create_home: "{{ item.create_home }}"
  loop: "{{ linux_users }}"

- name: Manage sudoers entry for sudo users
  community.general.sudoers:
    name: "{{ item.name }}"
    user: "{{ item.name }}"
    commands: /bin/su
    nopassword: true
    state: "{{ 'present' if item.state == 'present' and 'sudo' in (item.groups.split(',') | map('trim') | list) else 'absent' }}"
  loop: "{{ linux_users }}"
```

- [ ] **Step 2: Commit**

```bash
git add roles/basic/tasks/manage-users.yml
git commit -m "feat(basic): add user, group, and sudoers management"
```

---

## Task 5: Create basic/tasks/setup-ssh.yml

**Files:**
- Create: `roles/basic/tasks/setup-ssh.yml`

- [ ] **Step 1: Create roles/basic/tasks/setup-ssh.yml**

```yaml
---

- name: Copy custom SSH config file over
  copy:
    src: sshd_custom.conf
    dest: /etc/ssh/sshd_config.d/custom.conf
    mode: 0644
    owner: root
    group: root

- name: Make sure SSH uses custom config file
  lineinfile:
    insertafter: "EOF"
    line: "Include sshd_config.d/custom.conf"
    path: /etc/ssh/sshd_config
    state: present
  notify: restart ssh

- name: Only allow ssh for set users
  replace:
    path: /etc/ssh/sshd_config.d/custom.conf
    regexp: '^AllowUsers\s([\s\w][^#\n]+)'
    replace: AllowUsers \1 {{ item.name }}
  loop: "{{ linux_users }}"
  when: item.ssh_access == true
  notify: restart ssh
```

- [ ] **Step 2: Commit**

```bash
git add roles/basic/tasks/setup-ssh.yml
git commit -m "feat(basic): add SSH hardening tasks"
```

---

## Task 6: Create basic/tasks/setup-firewall.yml

**Files:**
- Create: `roles/basic/tasks/setup-firewall.yml`

- [ ] **Step 1: Create roles/basic/tasks/setup-firewall.yml**

```yaml
---

- name: Copy IPtables setup script over
  copy:
    src: start_firewall.sh
    dest: /usr/local/bin/start_firewall.sh
    mode: 0644
    owner: root
    group: root
  notify: setup iptables dns

- name: Install fail2ban
  apt:
    pkg:
      - fail2ban
    state: present
```

- [ ] **Step 2: Commit**

```bash
git add roles/basic/tasks/setup-firewall.yml
git commit -m "feat(basic): add firewall and fail2ban tasks"
```

---

## Task 7: Create basic/tasks/main.yml — basic role complete

**Files:**
- Create: `roles/basic/tasks/main.yml`

- [ ] **Step 1: Create roles/basic/tasks/main.yml**

```yaml
---

- name: Set the hostname
  hostname:
    name: "{{ hostname }}"

- name: Install base packages
  apt:
    pkg:
      - unzip
      - unattended-upgrades
    state: present

- name: Configure unattended-upgrades
  copy:
    src: 20auto-upgrades
    dest: /etc/apt/apt.conf.d/20auto-upgrades
    mode: 0644
    owner: root
    group: root

- include_tasks: manage-users.yml
- include_tasks: setup-ssh.yml
- include_tasks: setup-firewall.yml
```

- [ ] **Step 2: Commit**

```bash
git add roles/basic/tasks/main.yml
git commit -m "feat(basic): add main task orchestrator — basic role complete"
```

---

## Task 8: Strip web/tasks/main.yml

**Files:**
- Modify: `roles/web/tasks/main.yml`

- [ ] **Step 1: Replace roles/web/tasks/main.yml with web-only tasks**

```yaml
---
# Web role tasks

- name: Install certbot
  apt:
    pkg:
      - certbot
    state: present

- include_tasks: setup-openlitespeed.yml

- name: Enable Ondrej PHP PPA
  apt_repository:
    repo: ppa:ondrej/php
    state: present

- name: Install PHP and some PHP modules
  apt:
    pkg:
      - php8.3
      - php8.3-fpm
      - php8.3-cli
      - php8.3-mysql
      - php8.3-gd
      - php8.3-imagick
      - php8.3-xml
      - php8.3-zip
      - php8.3-intl
      - php8.3-mbstring
      - php8.3-curl
      - php8.3-imap
    state: present

- name: Create Let's Encrypt's live directory
  file:
    path: /etc/letsencrypt/live
    state: directory
    owner: root
    group: root
    mode: 0600

- include_tasks: setup-php-fpm.yml
```

Note: `certbot` extracted from the old "Install default packages" task (which also included `unzip`) — unzip moves to basic, certbot stays in web.

- [ ] **Step 2: Commit**

```bash
git add roles/web/tasks/main.yml
git commit -m "refactor(web): strip generic tasks, keep web-only tasks"
```

---

## Task 9: Remove moved files from web role and clean up

**Files:**
- Delete: `roles/web/tasks/manage-users.yml`
- Delete: `roles/web/tasks/setup-firewall-ids.yml`
- Delete: `roles/web/files/sshd_custom.conf`
- Delete: `roles/web/files/start_firewall.sh`
- Delete: `roles/web/vars/private/linuxusers.yml`
- Modify: `roles/web/handlers/main.yml`
- Modify: `roles/web/vars/main.yml`
- Modify: `roles/web/defaults/main.yml`

- [ ] **Step 1: Delete moved task files**

```bash
git rm roles/web/tasks/manage-users.yml roles/web/tasks/setup-firewall-ids.yml
```

- [ ] **Step 2: Delete moved files**

```bash
git rm roles/web/files/sshd_custom.conf roles/web/files/start_firewall.sh
```

- [ ] **Step 3: Delete moved private vars**

```bash
git rm roles/web/vars/private/linuxusers.yml
```

- [ ] **Step 4: Clear roles/web/handlers/main.yml**

```yaml
---
```

- [ ] **Step 5: Update roles/web/vars/main.yml — remove linux_groups**

```yaml
---
# Variables for the web role

certvars:
  key_size: 4096
  passphrase:
  key_type: RSA
  country_name: NL
  email_address: no@need.nl
  organization_name: No need
```

- [ ] **Step 6: Update roles/web/defaults/main.yml — remove unused apache_* variables**

```yaml
---
```

- [ ] **Step 7: Commit**

```bash
git add roles/web/handlers/main.yml roles/web/vars/main.yml roles/web/defaults/main.yml
git commit -m "refactor(web): remove moved files, clear stale vars and handlers"
```

---

## Task 10: Update site.yml

**Files:**
- Modify: `site.yml`

- [ ] **Step 1: Update site.yml**

```yaml
---
# Main playbook

- name: Deploy and configure webservers
  become: yes
  hosts: webservers
  roles:
    - basic
    - web
  vars_files:
    - roles/basic/vars/private/linuxusers.yml

- name: Deploy database servers
  become: yes
  hosts: dbservers
  roles:
    - db

- name: Deploy mailservers
  become: yes
  hosts: mailservers
  roles:
    - mail
  vars_files:
    - roles/mail/vars/private/mailvars.yml
```

- [ ] **Step 2: Run syntax-check — must pass cleanly**

```bash
ansible-playbook --syntax-check -i inventory-example.yml site.yml
```

Expected: `playbook: .../site.yml`

If it fails, check: vars_files path exists, role names are correct, no missing include_tasks targets.

- [ ] **Step 3: Commit**

```bash
git add site.yml
git commit -m "feat: wire basic role into site.yml for webservers"
```

---

## Task 11: Final verification

- [ ] **Step 1: Full syntax-check**

```bash
ansible-playbook --syntax-check -i inventory-example.yml site.yml
```

Expected: `playbook: .../site.yml`

- [ ] **Step 2: Verify basic role file tree**

```bash
find roles/basic -type f | sort
```

Expected output:
```
roles/basic/files/20auto-upgrades
roles/basic/files/sshd_custom.conf
roles/basic/files/start_firewall.sh
roles/basic/handlers/main.yml
roles/basic/tasks/main.yml
roles/basic/tasks/manage-users.yml
roles/basic/tasks/setup-firewall.yml
roles/basic/tasks/setup-ssh.yml
roles/basic/vars/main.yml
roles/basic/vars/private/README.md
roles/basic/vars/private/linuxusers.yml
```

- [ ] **Step 3: Verify web role no longer has moved files**

```bash
find roles/web -type f | sort
```

Expected — these should NOT appear:
- `roles/web/files/sshd_custom.conf`
- `roles/web/files/start_firewall.sh`
- `roles/web/tasks/manage-users.yml`
- `roles/web/tasks/setup-firewall-ids.yml`
- `roles/web/vars/private/linuxusers.yml`

- [ ] **Step 4: Verify sudoers task renders correctly (dry run)**

```bash
ansible-playbook --syntax-check -i inventory-example.yml site.yml && echo "PASS"
```

Expected: `playbook: .../site.yml` then `PASS`

- [ ] **Step 5: Final commit if any loose files remain**

```bash
git status
```

If clean: done. If dirty: stage and commit with `"chore: final cleanup"`.
