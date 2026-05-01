# Design: Extract `basic` Role from `web` Role

Date: 2026-05-01

## Goal

Extract all server-agnostic tasks from the `web` role into a new `basic` role. Apply both roles explicitly in `site.yml` for webservers. Replace the weekly apt cron with `unattended-upgrades`. Add passwordless `sudo su` for sudoers users via `community.general.sudoers`.

---

## New `basic` Role Structure

```
roles/basic/
  tasks/
    main.yml           # hostname, base packages, unattended-upgrades, includes below
    manage-users.yml   # groups, users, sudoers entries
    setup-ssh.yml      # sshd_custom.conf drop-in + AllowUsers
    setup-firewall.yml # iptables script + fail2ban
  handlers/
    main.yml           # restart ssh, setup iptables dns, start firewall
  files/
    sshd_custom.conf   # moved from roles/web/files/
    start_firewall.sh  # moved from roles/web/files/
```

### `tasks/main.yml`

1. Set hostname (`hostname` module)
2. Install base packages: `unzip`, `unattended-upgrades`
3. Configure unattended-upgrades: copy `/etc/apt/apt.conf.d/20auto-upgrades` enabling `APT::Periodic::Update-Package-Lists "1"` and `APT::Periodic::Unattended-Upgrade "1"`
4. `include_tasks: manage-users.yml`
5. `include_tasks: setup-ssh.yml`
6. `include_tasks: setup-firewall.yml`

---

## User Management + Visudo (`manage-users.yml`)

1. Create/remove groups (loop over `linux_groups`)
2. Create/remove users (loop over `linux_users`)
3. Manage sudoers entries (loop over `linux_users`):
   - Use `community.general.sudoers` module
   - When `item.state == "present"` and `"sudo" in item.groups`: create entry with `commands: /bin/su`, `nopassword: true`
   - When `item.state == "absent"` or user not in sudo group: `state: absent` on the sudoers entry
   - Entry name: `{{ item.name }}`

Requires `community.general` collection — add `requirements.yml` at repo root.

---

## SSH Setup (`setup-ssh.yml`)

Moved verbatim from `roles/web/tasks/main.yml`:
- Copy `sshd_custom.conf` to `/etc/ssh/sshd_config.d/custom.conf`
- Ensure `sshd_config` includes `sshd_config.d/custom.conf`
- Loop over `linux_users` to append SSH-allowed users (`AllowUsers`)

---

## Firewall Setup (`setup-firewall.yml`)

Moved verbatim from `roles/web/tasks/setup-firewall-ids.yml`:
- Copy `start_firewall.sh` to `/usr/local/bin/`
- Install `fail2ban`

---

## Handlers (`handlers/main.yml`)

Moved from `roles/web/handlers/main.yml`:
- `restart ssh`
- `setup iptables dns`
- `start firewall`

The `web` role's `handlers/main.yml` is replaced with a file containing only `---` (no handlers remain there for now).

---

## Changes to `web` Role

### `tasks/main.yml` — removed tasks

- Set hostname
- Install default packages (unzip)
- Weekly update cron
- Copy SSH config / SSH AllowUsers block
- `include_tasks: manage-users.yml`
- `include_tasks: setup-firewall-ids.yml`

### `tasks/manage-users.yml`

File deleted (moved to `basic` role).

### `tasks/setup-firewall-ids.yml`

File deleted (moved to `basic` role as `setup-firewall.yml`).

### `files/` — removed

- `sshd_custom.conf` (moved to `roles/basic/files/`)
- `start_firewall.sh` (moved to `roles/basic/files/`)

---

## `site.yml` Changes

```yaml
- name: Deploy and configure webservers
  become: yes
  hosts: webservers
  roles:
    - basic
    - web
  vars_files:
    - roles/web/vars/private/linuxusers.yml
```

`dbservers` and `mailservers` plays unchanged for now (basic role not yet applied to them).

---

## `requirements.yml` (new)

```yaml
collections:
  - name: community.general
```

---

## Variables

`linux_groups` moves from `roles/web/vars/main.yml` to `roles/basic/vars/main.yml`.

`linux_users` moves from `roles/web/vars/private/linuxusers.yml` to `roles/basic/vars/private/linuxusers.yml`. The `private/` directory in the basic role gets its own `README.md` (copied from the web role's private README).

`site.yml` `vars_files` updated to point to `roles/basic/vars/private/linuxusers.yml`.

`roles/web/vars/main.yml` retains only the non-user variables (`certvars`, and the now-unused `apache_*` defaults can be cleaned from `roles/web/defaults/main.yml` too).
