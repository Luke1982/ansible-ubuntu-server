# roles/web/vars/private

This directory should contain `linuxusers.yml`. It is gitignored.

## linuxusers.yml

Defines the web server's Linux users (typically one per hosted site). Uses the same format as `roles/basic/vars/private/linuxusers.yml`.

```yaml
linux_groups:
  - name: "webusers"
    state: "present"

linux_users:
  - name: "siteuser"
    state: "present"
    groups: "webusers"
    password: "$6$..."
    create_home: yes
    ssh_access: false
    ftp_access: true
```

| Field | Description |
|-------|-------------|
| `name` | Username |
| `state` | `present` or `absent` |
| `groups` | Comma-separated group string |
| `password` | Hashed password — generate with `mkpasswd --method=sha-512` |
| `create_home` | `yes` or `no` |
| `ssh_access` | `true` or `false` |
| `ftp_access` | `true` or `false` |

> Virtual host configuration (PHP-FPM pools, document roots) is not yet automated. Virtual hosts are configured manually in the OpenLiteSpeed admin panel after provisioning.
