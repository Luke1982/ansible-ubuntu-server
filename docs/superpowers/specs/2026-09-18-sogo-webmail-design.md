# Design: SOGo replaces Roundcube

Date: 2026-09-18. Builds on [the mailctl design](2026-09-14-mailctl-design.md) and [DNS records at TransIP and `mailctl doctor`](2026-09-18-mailctl-dns-design.md).

## Goal

The `mail` role installs SOGo instead of Roundcube: webmail, calendars, address books and phone sync (ActiveSync) for every mail account. Filters, vacation replies, forwarding and password changes work from SOGo. Calendar and contacts apps (Thunderbird, Apple Calendar/Contacts, DAVx5 and so on) sync with SOGo over CalDAV and CardDAV. Each domain reaches SOGo at `https://webmail.<domain>`, served by OpenLiteSpeed. `mail.<domain>` stays the name for IMAP and submission.

Roundcube is removed from servers that have it.

It must work on both supported releases: Ubuntu 24.04 (Dovecot 2.3) and Ubuntu 26.04 (Dovecot 2.4).

## Decisions

| Question | Decision |
|---|---|
| "External CalDAV/CardDAV" | Outside apps sync with SOGo's calendars and address books. SOGo does not subscribe to calendars hosted elsewhere |
| SOGo source on 24.04 | SOGo's own repository (`packages.sogo.nu/nightly/5/ubuntu noble`). Ubuntu 24.04 has no `sogo` package |
| SOGo source on 26.04 | Ubuntu's `sogo` package (5.12.4, universe). SOGo's repository has no 26.04 build |
| User directory | The mail accounts in MariaDB, read by SOGo through a view. No LDAP server |
| ActiveSync | On (`sogo-activesync`). Replaces the old, unused Z-Push template |
| Web address | `webmail.<domain>` for every mail domain, each with its own Let's Encrypt certificate |
| Keeping the domains up to date | `mailctl webmail sync`, run daily by a systemd timer and at the end of every playbook run |
| Roundcube's database | Kept. Nothing is migrated from it |
| Webmail language | Dutch, time zone Europe/Amsterdam; users can change both |

---

## Packages and repositories

Per release, in `roles/mail/defaults/main.yml` (like `dovecot_versions`), because the two package sources differ in more than their address:

```yaml
sogo_releases:
  "24.04":
    repository: https://packages.sogo.nu/nightly/5/ubuntu
    # SOGo's builds leave the database driver to a separate package.
    packages: [sogo, sogo-activesync, sope4.9-gdl1-mysql]
    resources: /usr/lib/GNUstep/SOGo/WebServerResources
  "26.04":
    repository: ""
    packages: [sogo, sogo-activesync]
    resources: /usr/share/GNUstep/SOGo/WebServerResources
```

`resources` is where the package puts SOGo's images, scripts and styles; OpenLiteSpeed serves them directly.

On 24.04 the role installs SOGo's signing key from the repo (`files/sogo-archive-key.asc`, fingerprint `74FFC6D72B925A34B5D356BDF8A27B36A6E2EAE9`, checked when it was added) as `/etc/apt/keyrings/sogo.asc`, and adds `deb [signed-by=/etc/apt/keyrings/sogo.asc] <repository> noble noble`. The key is in git, so a server never trusts a key it downloaded itself. The repository only holds SOGo and the libraries it needs, so it isn't pinned. unattended-upgrades only installs security updates, so the nightly SOGo changes only when someone runs `apt upgrade`.

The packages are installed with `memcached` and **without recommended packages**: SOGo's own build recommends `apache2 | nginx`, which would take ports 80 and 443 from OpenLiteSpeed.

## Removing Roundcube

- First the role answers dbconfig-common's "remove the database?" questions with no (debconf), so purging keeps Roundcube's database. Then `roundcube`, `roundcube-core`, `roundcube-plugins` and the `roundcube-mysql`/`roundcube-sqlite3` backend are purged. The database stays in MariaDB for anyone who wants their Roundcube contacts back.
- `/etc/fail2ban/jail.d/roundcube-auth.conf` is removed.
- From the repo: `tasks/configure-roundcube.yml`, `files/jail-roundcube-auth.conf` and `templates/autodiscover-vhost.conf.j2` (unused Apache/Z-Push leftover).
- Roundcube's Sieve filters: users' existing `roundcube` script stays active until they save filters or a vacation reply in SOGo. SOGo then writes and activates its own `sogo` script, and the old filters stop running. They aren't converted; `mailctl filters show` still displays them. The server-wide spam-to-Junk script is unaffected either way.

## SOGo

### Database

A `sogo` database and a `sogo` MariaDB user (password `sogo_db_pass`, new in `mailvars.yml`). The user has all rights on `sogo.*` only: SOGo creates its tables itself. At the start the role checks that `sogo_db_pass` is set and only has letters, digits and `. _ ~ -`: it goes into database URLs in `sogo.conf`, where other characters would need escaping that SOGo may not undo.

SOGo reads the accounts through a view in its own database, so it needs no rights on `mailserver`. The view has the definer's (root's) rights on the table underneath:

```sql
CREATE OR REPLACE SQL SECURITY DEFINER VIEW sogo.sogo_users AS
SELECT email AS c_uid, email AS c_name, password AS c_password, email AS c_cn, email AS mail,
       SUBSTRING_INDEX(email, '@', -1) AS c_domain
FROM mailserver.virtual_users;
```

It reads a single table with no joins, so it stays updatable: password changes from SOGo go through it. The file is `files/setup_sogo_view.sql`, imported like the other schemas. A mailctl test loads it into the test database and checks, as a user with only the `sogo` rights, that the view lists the accounts and that a password change through it reaches `virtual_users`.

SOGo's own tables use the combined-store URLs (`OCSStoreURL`, `OCSAclURL`, `OCSCacheFolderURL`) as well as `SOGoProfileURL`, `OCSFolderInfoURL`, `OCSSessionsFolderURL`, `OCSEMailAlarmsFolderURL` and `OCSAdminURL`, all `mysql://sogo:<password>@127.0.0.1:3306/sogo/<table>`.

### `/etc/sogo/sogo.conf`

Templated from `templates/sogo.conf.j2`, mode 0640, `root:sogo` (it holds the database password). Main settings:

| Area | Settings |
|---|---|
| Login | One SQL user source: `canAuthenticate = YES`, `userPasswordAlgorithm = SHA512-CRYPT`, `prependPasswordScheme = YES`. Existing hashes carry their scheme (`{SHA512-CRYPT}` from mailctl, `{SHA256-CRYPT}` from Roundcube's old password plugin); SOGo reads the scheme in any case (`NSString+Crypto.m`), so both verify. SOGo writes the scheme as configured, so in capitals: new passwords are stored as `{SHA512-CRYPT}…`, exactly as `mailctl address password` stores them. Users log in with their full address |
| Password change | `SOGoPasswordChangeEnabled = YES` |
| Address book privacy | The user source is the shared address book (`isAddressBook = YES`) with `DomainFieldName = c_domain`. SOGo then gives each user the domain in that column and adds `c_domain = <their domain>` to every address book search (`SQLSource.m`, `visibleDomainsQualifierFromDomain`), so people only find accounts of their own domain, also when sharing a calendar. Customers on the same server can't see each other |
| Reading mail | `SOGoIMAPServer = imap://127.0.0.1:143`. Dovecot treats loopback as secure, so no TLS is needed. The folder names match the ones mailctl creates: `Sent`, `Drafts`, `Trash`, `Junk`. `NGImap4ConnectionStringSeparator = "."`, Dovecot's separator for Maildir |
| Sending | `SOGoSMTPServer = smtp://{{ hostname }}:587/?tls=YES` and `SOGoSMTPAuthenticationType = PLAIN`: SOGo logs in to submission as the user, as Roundcube did. The postfwd sending limits and the "send as" rules keep applying to webmail. The certificate matches `hostname`. Invitations and sharing notices are sent too (`SOGoAppointmentSendEMailNotifications`, `SOGoACLsSendEMailNotifications`) |
| Filters | `SOGoSieveServer = sieve://127.0.0.1:4190`, `SOGoSieveScriptsEnabled`, `SOGoVacationEnabled`, `SOGoVacationPeriodEnabled` and `SOGoForwardEnabled` all `YES`, `SOGoSieveFolderEncoding = UTF-8`. Vacation start and end dates are checked by the Sieve script itself with the `date` extension, which Dovecot has, so the `sogo-tool update-autoreply` job (and the Sieve admin account it needs) isn't used |
| Calendars and contacts | `SOGoCalendarDAVAccessEnabled` and `SOGoAddressBookDAVAccessEnabled` `YES` |
| ActiveSync | `SOGoMaximumPingInterval` and `SOGoMaximumSyncInterval` 280 seconds, `SOGoInternalSyncInterval` 30. SOGo's guide suggests 3540 for push, but OpenLiteSpeed closes a connection that has been quiet for 300 seconds (`connTimeout`, a server-wide setting this role doesn't change), so phones get changes pushed and simply renew their request every few minutes |
| Password guessing | `SOGoMaximumFailedLoginCount = 5`, `SOGoMaximumFailedLoginInterval = 60`, `SOGoFailedLoginBlockInterval = 300`: after five failures, attempts less than a minute apart are refused unchecked for five minutes. This applies to every way of logging in (webmail, calendar and contact apps, ActiveSync), unlike fail2ban below, which only sees webmail logins. The price: someone who keeps failing for an account once a minute keeps it locked, without learning its password |
| Sessions | `SOGoMemcachedHost = 127.0.0.1` |
| Daemon | `WOPort = {{ sogo_address }}` (`127.0.0.1:20000`) |
| Defaults | `SOGoMailDomain = {{ defaultdomain }}`, `SOGoLanguage = {{ sogo_language }}` (`Dutch`), `SOGoTimeZone = {{ sogo_timezone }}` (`Europe/Amsterdam`) |

The number of SOGo processes is `PREFORK={{ sogo_workers }}` in `/etc/default/sogo`, not `WOWorkersCount` in `sogo.conf`: both packages start `sogod` with `-WOWorkersCount $PREFORK`, which overrides the file. Default 10: a phone on ActiveSync keeps a process busy while it waits for changes, and the web interface and apps need the others.

`/etc/cron.d/sogo` (from the package) gets its session clean-up job (`sogo-tool expire-sessions 60`) switched on. `sogo` and `memcached` are enabled and started; a handler restarts `sogo` when `sogo.conf` or `/etc/default/sogo` changes.

SOGo caches a successful login for five minutes (`SOGoCacheCleanupInterval`, default 300), so after `mailctl address password` the old password keeps working in SOGo for up to five minutes. IMAP and submission take the new one at once.

### Dovecot: the ACL extension

SOGo's ActiveSync needs an IMAP server that offers the ACL, UIDPLUS, QRESYNC and ANNOTATE (or X-GUID) extensions (SOGo installation guide, "System Requirements"). Dovecot has all of them, but only offers ACL with its `acl` and `imap_acl` plugins loaded, and this role doesn't load them yet. Both Dovecot templates get:

- 2.3 (`dovecot-local-2.3.conf.j2`): `acl` added to the global `mail_plugins`, `imap_acl` to the `protocol imap` block, `plugin { acl = vfile }`
- 2.4 (`dovecot-local-2.4.conf.j2`): `acl = yes` in the global and in each protocol's `mail_plugins` (a protocol's list replaces the global one), `imap_acl = yes` for imap, `acl_driver = vfile`

There are no ACL files, so every user keeps full rights to their own folders and nothing changes for them. Sharing mail folders between users would also need a shared namespace, and is out of scope.

No LDAP server: the SOGo guide's requirements list assumes one, but SOGo authenticates users and provides the shared address book from SQL just as well ("SOGo can use a SQL-based database server for authentication", same guide).

## `webmail.<domain>` in OpenLiteSpeed

The `web` role installs OpenLiteSpeed, but its config (listeners and sites) is set up by hand in WebAdmin. The mail role adds to that config without changing what's there.

### Once, by Ansible (`tasks/configure-webmail.yml`)

1. Fail with a clear message when `/usr/local/lsws/conf/httpd_config.conf` doesn't exist (the `web` role must run first), or when it has no listener on port 443: "Add an HTTPS listener in OpenLiteSpeed first".
2. Find the listeners on ports 80 and 443 by their `address` lines (`*:80`, `[::]:443` and so on).
3. Create `/usr/local/lsws/conf/webmail/` with empty `vhosts.conf`, `http-maps.conf` and `https-maps.conf` if they're missing, so OpenLiteSpeed starts before the first sync.
4. Add include lines to `httpd_config.conf`, each in a block with its own marker (`blockinfile`), so a second listener on the same port gets its own:
   - `include /usr/local/lsws/conf/webmail/vhosts.conf` at the end, at the top level
   - `include /usr/local/lsws/conf/webmail/http-maps.conf` inside every port 80 listener
   - `include /usr/local/lsws/conf/webmail/https-maps.conf` inside every port 443 listener

   OpenLiteSpeed reads `include` lines anywhere, also inside blocks (`plainconf.cpp`), and WebAdmin keeps them (`PlainConfParser.php`).
5. Create `/var/www/webmail` (the document root, holding only Let's Encrypt's challenge files).
6. Restart OpenLiteSpeed when any of this changed, install the daily timer, and run `mailctl webmail sync`.

### Per domain, by `mailctl webmail sync`

The generated files under `/usr/local/lsws/conf/webmail/` are the record of which domains have webmail. Nothing else is stored.

For each domain in `virtual_domains`, sync checks that `webmail.<domain>` resolves to this server: every A and AAAA record must be one of the server's addresses. One pointing elsewhere is enough for Let's Encrypt, or a visitor, to end up at the wrong server.

| DNS | Site | Certificate | What sync does |
|---|---|---|---|
| Points here | none | none | Writes the site for port 80 only, restarts OpenLiteSpeed gracefully (`lswsctrl restart`), waits until it serves a test file from the challenge folder under the new name, gets the certificate (`certbot certonly --webroot -w /var/www/webmail --cert-name webmail.<domain> -d webmail.<domain>`), then switches the site to https and restarts again |
| Points here | http only | none | Tries the certificate again |
| Points here | https | present | Nothing (certbot's own timer renews) |
| Points elsewhere or doesn't exist | any | any | Removes the site and deletes the certificate (`certbot delete`) |
| Lookup fails | any | any | Leaves it as it is and warns |

A site whose domain is no longer in `virtual_domains` is removed the same way, and `mailctl domain delete` removes the domain's site and certificate straight away.

The wait before certbot also catches a server where the listener includes don't work: sync then says OpenLiteSpeed doesn't serve the name on port 80, instead of spending one of Let's Encrypt's five failed validations per hour.

Certbot uses the server's Let's Encrypt account. When there isn't one, it registers one with `letsencrypt_email` (optional inventory variable) or, without it, with `--register-unsafely-without-email`. A failed certificate is a warning for that domain, with the `Detail:` lines of certbot's error; the other domains carry on. The next daily run tries again, which stays well under Let's Encrypt's limits. Certbot gets `--config-dir` explicitly, so mailctl and certbot agree on where certificates are.

Each domain gets a virtual host `webmail.<domain>` in `vhosts.conf`, with its own config file `webmail.<domain>.conf`. Until the certificate exists, the site only answers Let's Encrypt's challenges: SOGo is never offered over plain http, so no password crosses the internet unencrypted. With the certificate:

- `vhssl` with `/etc/letsencrypt/live/webmail.<domain>/`; port 80 redirects to https, except for the challenges
- an external app of type proxy to `{{ sogo_address }}`, with a response timeout above ActiveSync's 280 seconds
- `/SOGo` proxied to SOGo, and `/Microsoft-Server-ActiveSync` to SOGo's `/SOGo/Microsoft-Server-ActiveSync`, with the request headers SOGo builds its links from (`x-webobjects-server-url https://webmail.<domain>`, `-server-name`, `-server-port`, `-server-protocol`)
- `/.well-known/caldav` and `/.well-known/carddav` redirect to `/SOGo/dav/`, so apps find calendars and contacts from the server name alone
- `/SOGo.woa/WebServerResources/` served directly from SOGo's installed files
- `/` redirects to `/SOGo/`
- LiteSpeed's page cache is off for the site, so one user's mail can never be served to another

`http-maps.conf` maps every site; `https-maps.conf` only the sites with a certificate, so OpenLiteSpeed never loads a site whose certificate is missing.

The existing certbot deploy hook (`templates/1-post-deploy-hook-letsencrypt.sh.j2`) also restarts OpenLiteSpeed gracefully when a `webmail.*` certificate is renewed.

The timer is `mailctl-webmail-sync.timer`: daily with a random delay of up to an hour, `Persistent=true`. Its service runs `mailctl webmail sync`, which logs to the journal.

## mailctl changes

These build on the TransIP and `doctor` work, which adds SRV records, the resolver's `srv()`, and the domain checks shared by `status` and `doctor`.

| Command | Change |
|---|---|
| `webmail sync` | New. Does the above and prints one line per domain: live, waiting for DNS (with the records to publish), certificate failed (with certbot's reason), removed. A line saying what changed, so Ansible can tell a run that changed something from one that didn't |
| `domain add`, `dns show` | The recommended records gain A/AAAA for `webmail.<domain>` and the SRV records `_caldavs._tcp` and `_carddavs._tcp` (`0 1 443 webmail.<domain>`), so apps set up from just an email address find the server. `domain add` then says: "Webmail comes up at https://webmail.<domain> within a day of these records being published. To do it now: mailctl webmail sync" |
| `dns publish` | Publishes those records too. At TransIP, A/AAAA for `webmail` replace every A, AAAA and CNAME at that name, as for `mail` |
| `domain delete` | Also removes the domain's webmail site and certificate |
| `status <domain>`, `doctor` | A **Webmail** check: webmail.<domain> points here and the site is live on https, or what's missing. A warning, because mail works without it. The CalDAV and CardDAV SRV records are checked with the other SRV records |
| `status <address>` | The logins heading becomes "Last login (IMAP, webmail and phones)": SOGo logs in over IMAP from 127.0.0.1 for webmail and ActiveSync |

`Config` gets `letsencrypt_email` (optional), `sogo_address` and `sogo_resources` (written by Ansible), and path defaults for the OpenLiteSpeed webmail directory, `lswsctrl`, the document root and the Let's Encrypt directory, so tests can point them at temporary directories. The new code is `core/webmail.py` (which sites should exist, writing and removing them, certbot and OpenLiteSpeed calls through `system.run`) and `commands/webmail.py`. The "points here" test is one function in `dns_check`, used by sync and by the Webmail check.

## Security

- **fail2ban:** a `sogo-auth` jail (`files/jail-sogo-auth.conf`) on `/var/log/sogo/sogo.log`, ports http and https, with the same retry and ban settings as the Roundcube jail had. SOGo logs the client of a failed webmail login from `X-Forwarded-For` (SOPE's `WOHttpTransaction`). OpenLiteSpeed keeps whatever `X-Forwarded-For` the client sent and appends the address it really came from (`proxyconn.cpp`), but fail2ban's stock `sogo-auth` filter bans the *first* address in the list: a client could dodge bans by sending made-up addresses, or get someone else banned for good. So the role installs `filter.d/sogo-auth.local` with a `failregex` that takes the last address, the one OpenLiteSpeed adds.
- Calendar, contact and ActiveSync logins fail without a log line fail2ban can tell apart from a normal first request, so they're covered by SOGo's own throttle (above) instead.
- **Firewall:** no change. SOGo (20000), memcached (11211) and ManageSieve (4190) only listen on or are only reached over localhost, and ports 80 and 443 are already open.
- `jail-dovecot.conf`'s comment about Roundcube now refers to SOGo: webmail logs in to IMAP from localhost, so the dovecot jail never bans it.

## Files

| File | Change |
|---|---|
| `roles/mail/tasks/main.yml` | Package list; asserts; `remove-roundcube.yml`, `configure-sogo.yml` and, after mailctl, `configure-webmail.yml` instead of `configure-roundcube.yml` |
| `roles/mail/tasks/remove-roundcube.yml` | New |
| `roles/mail/tasks/configure-sogo.yml` | New: repository (24.04), packages, database, view, `sogo.conf`, `/etc/default/sogo`, cron, services |
| `roles/mail/tasks/configure-webmail.yml` | New: OpenLiteSpeed includes, document root, timer, first sync |
| `roles/mail/tasks/configure-fail2ban.yml` | SOGo jail and filter instead of Roundcube's; removes the old jail file |
| `roles/mail/templates/dovecot-local-2.3.conf.j2`, `dovecot-local-2.4.conf.j2` | `acl` and `imap_acl` plugins, `vfile` driver |
| `roles/mail/tasks/install-mailctl.yml` | `letsencrypt_email`, `sogo_address`, `sogo_resources` in config.json |
| `roles/mail/templates/sogo.conf.j2` | New |
| `roles/mail/templates/1-post-deploy-hook-letsencrypt.sh.j2` | Restarts OpenLiteSpeed for `webmail.*` certificates |
| `roles/mail/files/sogo-archive-key.asc` | New: SOGo's signing key |
| `roles/mail/files/setup_sogo_view.sql` | New |
| `roles/mail/files/jail-sogo-auth.conf`, `filter-sogo-auth.local` | New |
| `roles/mail/files/mailctl-webmail-sync.service`, `.timer` | New |
| `roles/mail/handlers/main.yml` | `restart sogo`, `restart openlitespeed` |
| `roles/mail/defaults/main.yml` | `sogo_releases`, `sogo_address`, `sogo_workers`, `sogo_language`, `sogo_timezone`, `letsencrypt_email` |
| `roles/mail/files/mailctl/…` | `core/webmail.py`, `commands/webmail.py`, changes to `dns_check`, `config`, `domain`, `status` and the domain checks, and tests |
| Removed | `tasks/configure-roundcube.yml`, `files/jail-roundcube-auth.conf`, `templates/autodiscover-vhost.conf.j2` |
| `README.MD`, `inventory-example.yml`, `roles/mail/vars/private/README.md` | SOGo instead of Roundcube; `sogo_db_pass`; `letsencrypt_email`; the webmail DNS records and `mailctl webmail sync` |

## Testing

There are no VMs: the role is tested on a VPS by hand. Everything that can be checked here is:

- **mailctl:** unit tests like the existing ones, against the throwaway MariaDB and with fake `certbot` and `lswsctrl` commands (the `fake_command` fixture): the sync table row by row, a failed certificate leaving other domains alone, a lookup failure changing nothing, strict "points here", the generated files, the map files only listing https sites that have a certificate, the new records and checks, and `domain add`/`delete`/`status` output. Run on the 24.04 and 26.04 library versions, as now.
- **The SOGo view:** loaded into the test database, read and updated as the `sogo` user (see Database).
- **OpenLiteSpeed:** the generated config is checked with OpenLiteSpeed itself (the `openlitespeed` binary from LiteSpeed's package, unpacked, run as a normal user on spare ports): `-t` accepts an `httpd_config.conf` with listeners that include the generated files, and a running server with a stand-in backend on the SOGo port shows that `PROPFIND`, `REPORT` and `MKCALENDAR` reach the backend, with the `x-webobjects-*` headers and an `X-Forwarded-For` ending in the real client; that `/Microsoft-Server-ActiveSync` reaches `/SOGo/Microsoft-Server-ActiveSync`; that `.well-known` and `/` redirect; that an exact map beats a catch-all `*` map on the same listener; and that a site without a certificate only serves challenge files.
- **fail2ban:** `fail2ban-regex` with the new filter against log lines with one, several and made-up forwarded addresses.
- **Dovecot:** the ACL changes checked with `doveconf` from Ubuntu 24.04's `dovecot-core`, unpacked here, as for the mailctl work. The 2.4 template is checked on the VPS.
- **Ansible:** `ansible-playbook --syntax-check`.

### On the VPS

Things only a real server shows, in the order to try them:

1. The playbook on a server with Roundcube: packages gone, Roundcube's database still there, SOGo running.
2. Log in to SOGo with a mailctl-created account and with a Roundcube-era `{SHA256-CRYPT}` password.
3. Send and receive; a message over the sending limit is refused.
4. A filter, a vacation reply with an end date, and a forward, as seen by `mailctl filters show` and by mail actually delivered.
5. Change the password in SOGo, then log in over IMAP with the new one.
6. The address book only shows the user's own domain.
7. A calendar and contacts app via `https://webmail.<domain>`; a phone with ActiveSync.
8. Five wrong webmail logins get the client's address banned.
9. `mailctl webmail sync` for a domain whose `webmail.` record points to the server: a real certificate.
10. The playbook a second time changes nothing.

## Out of scope

- Email reminders for calendar events (`sogo-ealarms-notify`): they need an account to send from. Apps and SOGo's own pop-up reminders still work.
- ActiveSync autodiscover (`autodiscover.<domain>`): phones are set up with `webmail.<domain>` as the server.
- Moving Roundcube contacts or filters into SOGo.
- Sharing mail folders between users (needs a Dovecot shared namespace). Calendars and address books can be shared.
- Webmail on the server's own hostname.
- Managing the rest of OpenLiteSpeed's config (listeners, other sites, timeouts) from Ansible.
