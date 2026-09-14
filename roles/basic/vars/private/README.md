# roles/basic/vars/private

This directory should contain `linuxusers.yml`. It is gitignored.

## linuxusers.yml

Defines which Linux users and groups to manage on the server.

```yaml
linux_groups:
  - name: "webusers"
    state: "present"

linux_users:
  - name: "alice"
    state: "present"
    groups: "sudo"
    password: "$6$..."
    create_home: yes
    ssh_access: true
    public_key: "ssh-rsa AAAA... alice@host"
```

### linux_groups

| Field | Description |
|-------|-------------|
| `name` | Group name |
| `state` | `present` or `absent` |

### linux_users

| Field | Description |
|-------|-------------|
| `name` | Username |
| `state` | `present` or `absent` |
| `groups` | Comma-separated group string, e.g. `"sudo"` or `"sudo,webusers"` |
| `password` | Hashed password — generate with `mkpasswd --method=sha-512` |
| `create_home` | `yes` or `no` |
| `ssh_access` | `true` or `false` — controls `AllowUsers` in sshd config |
| `public_key` | SSH public key installed in `authorized_keys` (optional) |
