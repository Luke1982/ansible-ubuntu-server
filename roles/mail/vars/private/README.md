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
| `sogo_workers` | `10` | SOGo processes. Every phone using ActiveSync keeps one busy while it waits for changes: raise it when many phones do. Dovecot allows twice as many IMAP connections per user and address, since SOGo connects from 127.0.0.1 for everyone |
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

The websites' mail has limits of its own: 300 recipients per hour and 1000 per
day for each site, and 1000 per hour and 3000 per day for all of them together.
PHP hands it to `site-sendmail`, which tells Postfix which site it is from (the
user whose home directory the script runs in), so one hacked site can't send
the others' share. The sendmail command and other mail servers' port 25 are
closed to the user PHP runs as (`nobody`), so a script can't go around it;
blocked attempts show up in the kernel log as `website-smtp-blocked`. A site
over its limit gets `false` from PHP's `mail()`.

```yaml
mail_website_send_limits:    # each site
  - recipients: 300
    seconds: 3600
mail_websites_send_limits:   # all sites together
  - recipients: 1000
    seconds: 3600
```

## Mail queue warning (optional)

Every 10 minutes `mailctl queue check` counts the messages waiting in Postfix's
queue. From 100 it mails `info@majorlabel.nl` who they are from, and again every
4 hours while the queue stays that long. A long queue is how an account or a
site that sends spam shows itself.

```yaml
mail_queue_warn_at: 100
mail_queue_warn_to: info@example.com
```
