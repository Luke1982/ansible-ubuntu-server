## In this directory
Should be the files

- linuxusers.yml
- apachevhosts.yml

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

## apachevhosts.yml
Needs the following structure:

```yaml
apache_vhosts:
  testhost:
    serveradmin: "admin@testhost.nl"
    servername: "testhost.nl"
    serveralias: "www.testhost.nl"
    documentroot: "/home/testuser/public_html/www"
    docroot_owner: "testuser"
    docroot_owngroup: "testuser"
    ssl: "yes"
    phpver: "7.4"
    ssldir: testhost.nl
    phppool:
      max_children: 200
      start_servers: 64
      min_spare_servers: 40
      max_spare_servers: 80
```
Where

- `serveradmin` is the server admin e-mail address
- `servername` is the main server name
- `serveralias` is the space-separated list of aliasses
- `documentroot` will be used as the DocumentRoot
- `docroot_owner`: The owner, will be used in the PHP-FPM poolname
- `docroot_owngroup` The owning group of the DocumentRoot
- `ssl` SSL enabled for this host (not yet implemented)