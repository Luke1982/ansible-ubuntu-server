# web03: mail autodiscover fixes to carry into Ansible (2026-11-03)

Hand-made changes on web03 that are live now. The Ansible role that writes these
files must get the same changes, or the next run undoes them and autodiscover breaks
again for every domain.

## 1. OLS templates: `conf/templates/mailautodiscover.conf` and `mailautodiscover-http.conf`

Path: `/usr/local/lsws/conf/templates/`. Backups on the server: `*.conf.bak-20261103`.
Same change in both files, in `extprocessor mailautodiscover-php`:

| Setting | Old | New |
|---|---|---|
| `maxConns` | 2 | 4 |
| `env PHP_LSAPI_CHILDREN` | 2 | 8 |
| `persistConn` | 1 | 0 |
| `memSoftLimit` | 256M | 1024M |
| `memHardLimit` | 320M | 1200M |

Why:
- **Memory limit:** lsphp83 crashed on every start with `Fatal Error Unable to allocate
  shared memory segment of 134217728 bytes: mmap: Cannot allocate memory`
  (`/usr/local/lsws/logs/stderr.log`). OPcache's 128M shared memory doesn't fit inside a
  320M memory cap. Requests hung with "possible dead lock" in `error.log`, and Outlook
  timed out. Keep the hard limit at 1024M or more, or turn OPcache off for this processor.
- **persistConn / children:** the extprocessor is defined inside the template, so every
  member vhost (about 27) in every OLS worker gets its own copy on the same socket. With
  persistent connections, the two PHP children stayed tied to one copy. All other
  requests waited and timed out (`Reached max children process limit: 2`).
  Possible later improvement: define the extprocessor once at server level in
  `httpd_config.conf` instead of in the template.

## 2. `/var/www/mailautodiscover/autodiscover.php`

Backup on the server: `/root/autodiscover.php.bak-20261103`. Added right after
`require __DIR__ . '/mailsettings.php';`:

```php
// Outlook's third step is a GET over plain HTTP that expects a redirect to the
// HTTPS address, where it then posts its request with the email address in it.
// Answering it directly gives Outlook settings without a login name, and it
// then guesses the part before the @.
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST' && ($_SERVER['HTTPS'] ?? '') !== 'on') {
    $host = strtolower(explode(':', $_SERVER['HTTP_HOST'] ?? '')[0]);
    header('Location: https://' . $host . '/autodiscover/autodiscover.xml', true, 302);
    exit;
}
```

Why: Outlook (classic, MAPICPL 16.0) did `GET http://autodiscover.DOMAIN/autodiscover/autodiscover.xml`
with no body. The script answered it with IMAP/SMTP settings without a `<LoginName>`.
Outlook then logged in as just `info`, the login failed, and fail2ban banned the client.

Check after deploying:
- `curl -sI http://autodiscover.DOMAIN/autodiscover/autodiscover.xml` gives a 302 to https.
- A POST with an `<EMailAddress>` to the https URL gives 200 within 0.1s and includes `<LoginName>`.

## 3. Not changed, worth deciding in Ansible

- **fail2ban:** the client IP `217.103.156.88` was banned by the `dovecot` jail during setup.
  It was unbanned and added at runtime only (`addignoreip`, for the dovecot, postfix and
  sogo-auth jails), so it is lost when fail2ban restarts. Decide whether customer office
  IPs belong in `ignoreip`.
- **Root domain:** `https://DOMAIN/autodiscover/autodiscover.xml` gives a 502 when the root
  domain sits behind Cloudflare (speetotaaltechniek.nl does). Outlook tries that address
  first. It moves on, but it costs time.
## 4. SOGo workers and MariaDB connections (done live, needs Ansible)

- **`/etc/default/sogo`:** `PREFORK=10` changed to `PREFORK=30` (backup
  `/etc/default/sogo.bak-20261103`), and SOGo restarted. Each ActiveSync device keeps one
  worker busy for up to 280s (`SOGoMaximumPingInterval`), so 30 workers handle about 25
  devices. All 30 workers together now use about 1 GB of RAM. Rule of thumb: workers =
  devices + 5.
- **MariaDB:** `max_connections` raised from 151 to 300 **at runtime only**
  (`SET GLOBAL`), so it resets when MariaDB restarts. Put `max_connections = 300` in the
  Ansible-managed MariaDB config. Each SOGo worker keeps its own DB connections, and the
  peak before this change was already 32.

## Client-side tips (Outlook classic on Windows)

If Outlook remembers a failed autodiscover attempt, it skips the HTTPS POST. To reset it:
```
del /q "%LOCALAPPDATA%\Microsoft\Outlook\*Autod*.xml"
reg delete "HKCU\Software\Microsoft\Office\16.0\Outlook\AutoDiscover" /f
reg add "HKCU\Software\Microsoft\Office\16.0\Outlook\AutoDiscover" /v ExcludeExplicitO365Endpoint /t REG_DWORD /d 1 /f
reg add "HKCU\Software\Microsoft\Office\16.0\Outlook\AutoDiscover" /v ExcludeHttpsRootDomain /t REG_DWORD /d 1 /f
```

## 5. `/etc/sogo/sogo.conf`: ActiveSync batch size (done live, needs Ansible)

Backup: `/etc/sogo/sogo.conf.bak-20261103`. Added after `SOGoInternalSyncInterval = 30;`:

```
  /* Initial sync of a big mailbox otherwise comes back as one huge answer
     (180 MB seen), which Outlook gives up on: send it in batches. */
  SOGoMaximumSyncWindowSize = 100;
  SOGoMaximumSyncResponseSize = 5172;
```

Why: without these settings SOGo answered the first Sync of a mailbox with one response
of up to 180 MB that took 60–200s to build. Outlook disconnects after about 57s ("client
disconnected during delivery of response" in `/var/log/sogo/sogo.log`). Folders arrived,
mail never did. With the limits, each Sync returns within about 1s and Outlook keeps
asking for the next batch.
