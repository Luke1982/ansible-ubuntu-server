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

> Sites are not set up from this file. A site's Linux user is made by `domainctl add DOMAIN` on the server, with
> no password, along with its directories, permissions, virtual host and certificate. See "Managing web sites with
> domainctl" in the project README.
>
> Use `linuxusers.yml` for the people who log in to the server: give them `ssh_access: true` and a password hash.
> A user listed here that `domainctl` also made is left alone as long as `state: present`.
