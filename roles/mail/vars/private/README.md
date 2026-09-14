# roles/mail/vars/private

This directory should contain `mailvars.yml`. It is gitignored.

## mailvars.yml

```yaml
---
mailuser_db_pass: "your-strong-password-here"
```

| Variable | Description |
|----------|-------------|
| `mailuser_db_pass` | Password for the `mailuser` MariaDB account used by Postfix and Dovecot |

## Inventory variables

The mail role also requires these variables in your private inventory file (`private/<hostname>.yml`):

| Variable | Description | Example |
|----------|-------------|---------|
| `hostname` | FQDN of the mail server | `mail.example.com` |
| `defaultdomain` | Primary mail domain | `example.com` |
| `emaildomains` | List of all domains to accept mail for | see below |

```yaml
mailservers:
  hosts:
    192.168.1.10:
      hostname: "mail.example.com"
      defaultdomain: example.com
      emaildomains:
        - example.com
        - other.com
```
