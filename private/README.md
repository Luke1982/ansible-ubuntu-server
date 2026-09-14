# Private inventory files

This directory holds per-server inventory files. These are gitignored and must be created manually.

Copy `inventory-example.yml` from the project root and fill in your values:

```yaml
all:
  children:
    webservers:
      hosts:
        192.168.1.10:
          hostname: "web01.example.com"
          dns_servers: 127.0.0.53
    dbservers:
      hosts:
        192.168.1.10:
    mailservers:
      hosts:
        192.168.1.10:
          hostname: "web01.example.com"
          defaultdomain: example.com
          emaildomains:
            - example.com
            - other.com
  vars:
    ansible_python_interpreter: /usr/bin/python3
    ansible_user: ansible
    ansible_ssh_private_key_file: ~/.ssh/id_rsa
```

A host can appear in multiple groups if it serves multiple roles (web + db + mail on the same machine).
