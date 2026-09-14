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

The mail role also requires these variables on the host in your private inventory file (`private/<hostname>.yml`, see `inventory-example.yml`):

| Variable | Description | Example |
|----------|-------------|---------|
| `hostname` | FQDN of the mail server | `mail.example.com` |
| `defaultdomain` | Primary mail domain | `example.com` |

```yaml
all:
  hosts:
    mail.example.com:
      ansible_host: 192.168.1.10
      hostname: "mail.example.com"
      defaultdomain: example.com
```

## Sending limits (optional)

Authenticated users can send to at most 300 recipients per hour and 1000 per
day (defaults in `roles/mail/defaults/main.yml`). Override them, or give single
accounts other limits, in `mailvars.yml` or the inventory:

```yaml
mail_send_limits:
  - recipients: 300
    seconds: 3600
  - recipients: 1000
    seconds: 86400

mail_send_limits_by_account:
  newsletter@example.com:
    - recipients: 2000
      seconds: 3600
  noreply@example.com: []   # no limit
```

A user over a limit gets a temporary `450 4.7.1 Sending limit reached` error for
the remaining recipients. The counters are kept in memory by postfwd and reset
when it restarts.
