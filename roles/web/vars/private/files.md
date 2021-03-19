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