# roles/mail/vars/private

This directory should contain `mailvars.yml`. It is gitignored.

## mailvars.yml

```yaml
---
mailuser_db_pass: "your-strong-password-here"
sogo_db_pass: "another-strong-password"
```

| Variable | Description |
|----------|-------------|
| `mailuser_db_pass` | Password for the `mailuser` MariaDB account used by Postfix and Dovecot |
| `sogo_db_pass` | Password for the `sogo` MariaDB account used by SOGo. Only letters, digits and `. _ ~ -`: it goes into database URLs |

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

## SOGo (optional)

Defaults in `roles/mail/defaults/main.yml`; override them in `mailvars.yml` or the inventory:

| Variable | Default | Description |
|----------|---------|-------------|
| `sogo_language` | `Dutch` | Language new webmail users start with |
| `sogo_timezone` | `Europe/Amsterdam` | Time zone new webmail users start with |
| `sogo_workers` | `10` | SOGo processes. Every phone using ActiveSync keeps one busy while it waits for changes: raise it when many phones do |
| `letsencrypt_email` | none | Email address for the Let's Encrypt account of the webmail certificates, used when certbot has no account on the server yet |

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
