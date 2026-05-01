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
