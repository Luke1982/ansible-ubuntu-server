## In this directory
Should be the files

- linuxusers.yml

## linuxusers.yml
Need the following structure:
```yaml
linux_users: 
  - create_home: yes
    group: "webusers"
    password: "$6$C6u7dfOf$OtkMTtZRYQinT2cJcoMZlIpMN5gpAYpRtqSlFxalouftyEhXQKICZhGCgiVD8I3drPX7bhli9WhwmDHDP4.H91"
    name: "testuser"
    state: "present"
    ssh_access: true
    ftp_access: true
```
Where:

- `create_home` decides whether there should be a home directory for the user, 'yes' or 'no'
- `group` determines the group for the user
- `password` is the password you got from running `mkpasswd --method=sha-512`
- `name` is obviously the username
- `state` 'present' for users that should be present, 'absent' for users that should be deleted
- `ssh_access` true or false
- `ftp_access` true or false