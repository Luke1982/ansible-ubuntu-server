# Design: SOGo replaces Roundcube

Date: 2026-09-18

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
| ActiveSync | On (`sogo-activesync`). Replaces the old, unused Z-Push template |
| Web address | `webmail.<domain>` for every mail domain, each with its own Let's Encrypt certificate |
| Keeping the domains up to date | `mailctl webmail sync`, run daily by a systemd timer and at the end of every playbook run |
| Roundcube's database | Kept. Nothing is migrated from it |

---

## Packages and repositories

A per-release map in `roles/mail/defaults/main.yml`, like `dovecot_versions`:

```yaml
sogo_repositories:
  "24.04": "https://packages.sogo.nu/nightly/5/ubuntu"   # SOGo's own builds
  "26.04": ""                                            # Ubuntu's package
```

On 24.04 the role downloads SOGo's signing key (fingerprint `74FFC6D72B925A34B5D356BDF8A27B36A6E2EAE9`, from keys.openpgp.org), checks the fingerprint, stores it dearmored in `/usr/share/keyrings/sogo.gpg`, and adds `deb [signed-by=/usr/share/keyrings/sogo.gpg] <url> noble noble`. The repository only holds SOGo and the libraries it needs, so it isn't pinned. unattended-upgrades only installs security updates, so the nightly SOGo changes only when someone runs `apt upgrade`.

Installed on both releases: `sogo`, `sogo-activesync`, `memcached`. `roundcube` and `roundcube-plugins` leave the package list.

## Removing Roundcube

- `roundcube`, `roundcube-core`, `roundcube-plugins` and the `roundcube-mysql`/`roundcube-sqlite3` backend are purged. First the role answers dbconfig-common's "remove the database?" questions with no (debconf), so purging keeps Roundcube's database. The database stays in MariaDB for anyone who wants their Roundcube contacts back.
- `/etc/fail2ban/jail.d/roundcube-auth.conf` is removed.
- From the repo: `tasks/configure-roundcube.yml`, `files/jail-roundcube-auth.conf` and `templates/autodiscover-vhost.conf.j2` (unused Apache/Z-Push leftover).
- Roundcube's Sieve filters: users' existing `roundcube` script stays active until they save filters or a vacation reply in SOGo. SOGo then writes and activates its own `sogo` script, and the old filters stop running. They aren't converted; `mailctl filters show` still displays them. The server-wide spam-to-Junk script is unaffected either way.

## SOGo

### Database

A `sogo` database and a `sogo` MariaDB user (password `sogo_db_pass`, new in `mailvars.yml`, checked by an `assert` at the start of the role). The user has all rights on `sogo.*` only: SOGo creates its tables itself.

SOGo reads the accounts through a view in its own database, so it needs no rights on `mailserver`. The view has the definer's (root's) rights on the table underneath:

```sql
CREATE OR REPLACE SQL SECURITY DEFINER VIEW sogo.sogo_users AS
SELECT email AS c_uid, email AS c_name, password AS c_password, email AS c_cn, email AS mail,
       SUBSTRING_INDEX(email, '@', -1) AS c_domain
FROM mailserver.virtual_users;
```

It reads a single table with no joins, so it stays updatable: password changes from SOGo go through it. The file is `files/setup_sogo_view.sql`, imported like the other schemas.

SOGo's own tables use the combined-store URLs (`OCSStoreURL`, `OCSAclURL`, `OCSCacheFolderURL`) as well as `SOGoProfileURL`, `OCSFolderInfoURL`, `OCSSessionsFolderURL` and `OCSEMailAlarmsFolderURL`, all `mysql://sogo:<url-encoded password>@127.0.0.1:3306/sogo/<table>`.

### `/etc/sogo/sogo.conf`

Templated from `templates/sogo.conf.j2`, mode 0640, `root:sogo` (it holds the database password). Main settings:

| Area | Settings |
|---|---|
| Login | One SQL user source: `canAuthenticate = YES`, `userPasswordAlgorithm = sha512-crypt`, `prependPasswordScheme = YES`. Existing hashes carry their scheme (`{SHA512-CRYPT}` from mailctl, `{SHA256-CRYPT}` from Roundcube's old password plugin), so both verify. New passwords are stored as `{SHA512-CRYPT}…`, exactly as `mailctl address password` stores them. Users log in with their full address |
| Password change | `SOGoPasswordChangeEnabled = YES` |
| Address book privacy | The user source is the shared address book (`isAddressBook = YES`) with `DomainFieldName = c_domain`, so people only find accounts of their own domain, both in the address book and when sharing a calendar. Customers on the same server can't see each other |
| Reading mail | `SOGoIMAPServer = imap://127.0.0.1:143`. Dovecot treats loopback as secure, so no TLS is needed. The folder names match the ones mailctl creates: `Sent`, `Drafts`, `Trash`, `Junk`. `NGImap4ConnectionStringSeparator` matches Dovecot's hierarchy separator |
| Sending | `SOGoSMTPServer = smtp://{{ hostname }}:587/?tls=YES` and `SOGoSMTPAuthenticationType = PLAIN`: SOGo logs in to submission as the user, as Roundcube did. The postfwd sending limits and the "send as" rules keep applying to webmail. The certificate matches `hostname` |
| Filters | `SOGoSieveServer = sieve://127.0.0.1:4190`, `SOGoSieveScriptsEnabled`, `SOGoVacationEnabled`, `SOGoVacationPeriodEnabled` (start and end dates) and `SOGoForwardEnabled` all `YES`, `SOGoSieveFolderEncoding = UTF-8` |
| Calendars and contacts | `SOGoCalendarDAVAccessEnabled` and `SOGoAddressBookDAVAccessEnabled` `YES` |
| ActiveSync | Enabled by the package, with `SOGoMaximumPingInterval`, `SOGoMaximumSyncInterval` and `SOGoInternalSyncInterval` at SOGo's recommended values for long-polling phones |
| Sessions | `SOGoMemcachedHost = 127.0.0.1` |
| Daemon | `WOPort = 127.0.0.1:20000`, `WOWorkersCount = {{ sogo_workers }}` (default 10: every phone on ActiveSync keeps a worker busy while it waits for mail) |
| Defaults | `SOGoMailDomain = {{ defaultdomain }}`, `SOGoLanguage = {{ sogo_language }}` (default `Dutch`), `SOGoTimeZone = {{ sogo_timezone }}` (default `Europe/Amsterdam`). Users can change their own language and time zone |

### Dovecot: the ACL extension

SOGo's ActiveSync needs an IMAP server that offers the ACL, UIDPLUS, QRESYNC and ANNOTATE (or X-GUID) extensions (SOGo installation guide, "System Requirements"). Dovecot has all of them, but only offers ACL with its `acl` and `imap_acl` plugins loaded, and this role doesn't load them yet. Both Dovecot templates get:

- 2.3 (`dovecot-local-2.3.conf.j2`): `acl` added to the global `mail_plugins`, `imap_acl` to the `protocol imap` block, `plugin { acl = vfile }`
- 2.4 (`dovecot-local-2.4.conf.j2`): `acl = yes` in the global and in each protocol's `mail_plugins` (a protocol's list replaces the global one), `imap_acl = yes` for imap, `acl_driver = vfile`

There are no ACL files, so every user keeps full rights to their own folders and nothing changes for them. Sharing mail folders between users would also need a shared namespace, and is out of scope.

No LDAP server: the SOGo guide's requirements list assumes one, but SOGo authenticates users and provides the shared address book from SQL just as well ("SOGo can use a SQL-based database server for authentication", same guide). The mail accounts already live in MariaDB, so SOGo reads them there.

### Cron and services

`/etc/cron.d/sogo` (from the package) gets its session clean-up job (`sogo-tool expire-sessions`) switched on. The `sogo` and `memcached` services are enabled and started; a handler restarts `sogo` when `sogo.conf` changes.

## `webmail.<domain>` in OpenLiteSpeed

The `web` role installs OpenLiteSpeed, but its config (listeners and sites) is set up by hand in WebAdmin. The mail role adds to that config without changing what's there.

### Once, by Ansible (`tasks/configure-webmail.yml`)

1. Fail with a clear message when `/usr/local/lsws/conf/httpd_config.conf` doesn't exist (the `web` role must run first). Also fail when it has no listener on port 443: "Add an HTTPS listener in OpenLiteSpeed first".
2. Find the listeners on ports 80 and 443 by their `address` lines (`*:80`, `[::]:443` and so on).
3. Create `/usr/local/lsws/conf/webmail/` with empty `vhosts.conf`, `http-maps.conf` and `https-maps.conf` if they're missing. OpenLiteSpeed starts fine with those files before the first sync.
4. Add include lines to `httpd_config.conf` (`lineinfile`, so they're added once):
   - `include /usr/local/lsws/conf/webmail/vhosts.conf` at the top level
   - `include /usr/local/lsws/conf/webmail/http-maps.conf` inside every port 80 listener
   - `include /usr/local/lsws/conf/webmail/https-maps.conf` inside every port 443 listener

   OpenLiteSpeed and WebAdmin both read `include` lines, including inside blocks (checked in their source: `plainconf.cpp` and `PlainConfParser.php`).
5. Create `/var/www/webmail` (the document root, holding only Let's Encrypt's challenge files).
6. After mailctl is installed (its `config.json` now also holds `letsencrypt_email`): install the daily timer and run `mailctl webmail sync`.

### Per domain, by `mailctl webmail sync`

The generated files under `/usr/local/lsws/conf/webmail/` are the record of which domains have webmail. Nothing else is stored.

For each domain in `virtual_domains`, sync checks whether `webmail.<domain>` resolves to one of this server's addresses (the same check `status` uses for `mail.<domain>`):

| DNS | Site | Certificate | What sync does |
|---|---|---|---|
| Points here | none | none | Writes the site on port 80 only, restarts OpenLiteSpeed gracefully, gets the certificate (`certbot certonly --webroot -w /var/www/webmail --cert-name webmail.<domain> -d webmail.<domain>`), then switches the site to https and restarts again |
| Points here | http only | none | Tries the certificate again |
| Points here | https | present | Nothing (certbot's own timer renews) |
| Points elsewhere or doesn't exist | any | any | Removes the site and deletes the certificate (`certbot delete`) |
| Lookup fails | any | any | Leaves it as it is and warns |

A site whose domain is no longer in `virtual_domains` is removed the same way. `mailctl domain delete` removes the domain's site and certificate straight away.

Certbot uses the server's Let's Encrypt account. When there isn't one, it registers one with `letsencrypt_email` (optional inventory variable) or, without it, with `--register-unsafely-without-email`. A failed certificate is a warning for that domain; the other domains carry on. The next daily run tries again, which stays well under Let's Encrypt's limits.

Each domain gets a virtual host `webmail.<domain>` in `vhosts.conf`, with its own config file `webmail.<domain>.conf`:

- document root `/var/www/webmail`, no scripts
- an external app of type proxy to `127.0.0.1:20000`
- `/.well-known/acme-challenge/` served from the document root (also on port 80 once https is on)
- `/.well-known/caldav` and `/.well-known/carddav` redirect to `/SOGo/dav/`, so apps find calendars and contacts from the server name alone
- `/SOGo.woa/WebServerResources/` served directly from SOGo's installed files
- `/SOGo` proxied to SOGo, and `/Microsoft-Server-ActiveSync` to SOGo's `/SOGo/Microsoft-Server-ActiveSync`, with the request headers SOGo builds its links from (`x-webobjects-server-url https://webmail.<domain>`, `-server-name`, `-server-port`, `-server-protocol`) and `X-Forwarded-For`
- `/` redirects to `/SOGo/`
- once the certificate exists: `vhssl` with `/etc/letsencrypt/live/webmail.<domain>/`, and port 80 redirects to https

`http-maps.conf` maps every site; `https-maps.conf` only the sites with a certificate. OpenLiteSpeed never loads a site whose certificate is missing.

The existing certbot deploy hook (`templates/1-post-deploy-hook-letsencrypt.sh.j2`) also restarts OpenLiteSpeed gracefully when a `webmail.*` certificate is renewed.

The timer is `mailctl-webmail-sync.timer`: daily with a random delay of up to an hour, `Persistent=true`. Its service runs `mailctl webmail sync`, which logs to the journal.

## mailctl changes

| Command | Change |
|---|---|
| `webmail sync` | New. Does the above and prints one line per domain: live, waiting for DNS, certificate failed (with certbot's reason), removed |
| `domain add` | The recommended records gain A/AAAA for `webmail.<domain>` and SRV records `_caldavs._tcp` and `_carddavs._tcp` (`0 1 443 webmail.<domain>`), so apps set up from just an email address find the server. Afterwards: "Webmail comes up at https://webmail.<domain> within a day of these records being published. To do it now: mailctl webmail sync" |
| `domain delete` | Also removes the domain's webmail site and certificate |
| `status <domain>` | Two more checks. **Webmail**: webmail.<domain> points here and the site is live on https, or what's missing (a warning, because mail works without it). **Calendar apps**: the SRV records (a warning when they're missing) |
| `status <address>` | The logins heading becomes "Last login (IMAP, webmail and phones)": SOGo logs in over IMAP from 127.0.0.1 for webmail and ActiveSync |

`dns_check.Resolver` gets an `srv()` lookup. `Config` gets `letsencrypt_email` (optional, from Ansible) and path defaults for the OpenLiteSpeed webmail directory, the document root, SOGo's port and the Let's Encrypt live directory, so tests can point them at temporary directories. The new code is `core/webmail.py` (plan, write and remove sites; certbot and OpenLiteSpeed calls through `system.run`) and `commands/webmail.py`.

## Security

- **fail2ban:** a `sogo-auth` jail (`files/jail-sogo-auth.conf`) using fail2ban's own `sogo-auth` filter on `/var/log/sogo/sogo.log`, ports http and https, with the same retry and ban settings as the Roundcube jail had. SOGo logs the address from `X-Forwarded-For`, which the proxy sets, so the real client gets banned. ActiveSync logins go through the same log.
- **Firewall:** no change. SOGo (20000), memcached (11211) and ManageSieve (4190) only listen on or are only reached over localhost, and ports 80 and 443 are already open.
- `jail-dovecot.conf`'s comment about Roundcube now refers to SOGo: webmail logs in to IMAP from localhost, so the dovecot jail never bans it.

## Files

| File | Change |
|---|---|
| `roles/mail/tasks/main.yml` | Package list; asserts; includes `configure-sogo.yml` and `configure-webmail.yml` instead of `configure-roundcube.yml`, and a new `remove-roundcube.yml` |
| `roles/mail/tasks/remove-roundcube.yml` | New |
| `roles/mail/tasks/configure-sogo.yml` | New: repository (24.04), database, view, `sogo.conf`, cron, services |
| `roles/mail/tasks/configure-webmail.yml` | New: OpenLiteSpeed includes, document root, timer, first sync |
| `roles/mail/tasks/configure-fail2ban.yml` | SOGo jail instead of Roundcube's; removes the old jail file |
| `roles/mail/templates/dovecot-local-2.3.conf.j2`, `dovecot-local-2.4.conf.j2` | `acl` and `imap_acl` plugins, `vfile` driver |
| `roles/mail/tasks/install-mailctl.yml` | `letsencrypt_email` in config.json |
| `roles/mail/templates/sogo.conf.j2` | New |
| `roles/mail/templates/1-post-deploy-hook-letsencrypt.sh.j2` | Restarts OpenLiteSpeed for `webmail.*` certificates |
| `roles/mail/files/setup_sogo_view.sql` | New |
| `roles/mail/files/jail-sogo-auth.conf` | New |
| `roles/mail/files/mailctl-webmail-sync.service`, `.timer` | New |
| `roles/mail/handlers/main.yml` | `restart sogo`, `restart memcached`, `restart openlitespeed` |
| `roles/mail/defaults/main.yml` | `sogo_repositories`, `sogo_workers`, `sogo_language`, `sogo_timezone` |
| `roles/mail/files/mailctl/…` | `core/webmail.py`, `commands/webmail.py`, changes to `dns_check`, `domain`, `status`, `config`, and tests |
| Removed | `tasks/configure-roundcube.yml`, `files/jail-roundcube-auth.conf`, `templates/autodiscover-vhost.conf.j2` |
| `README.MD`, `inventory-example.yml`, `roles/mail/vars/private/README.md` | SOGo instead of Roundcube; `sogo_db_pass`; `letsencrypt_email`; the webmail DNS records and `mailctl webmail sync` |

## Testing

- **mailctl:** unit tests like the existing ones, against the throwaway MariaDB and with fake `certbot` and `lswsctrl` commands (the existing `fake_command` fixture): the sync table above row by row, a failed certificate leaving other domains alone, a lookup failure changing nothing, generated site files, the map files only listing https sites that have a certificate, the new records and checks in `dns_check`, and `domain add`/`delete`/`status` output. Run on the 24.04 and 26.04 library versions, as now.
- **Ansible:** `ansible-playbook --syntax-check`, then the whole role on two throwaway VMs (Ubuntu 24.04 and 26.04 cloud images under libvirt on this machine, 2 GB RAM and 10 GB disk each; deleted afterwards). On each VM:
  - a Roundcube install from the current role is replaced: packages gone, database still there
  - log in to SOGo with a mailctl-created account and with a Roundcube-era `{SHA256-CRYPT}` password
  - send and receive mail from SOGo; a message over the sending limit is refused
  - a filter, a vacation reply with an end date, and a forward work, as seen by `mailctl filters show` and by mail actually delivered
  - change the password in SOGo, then log in over IMAP with the new one
  - the address book only shows the user's own domain
  - CalDAV and CardDAV via `.well-known` with a real client (or `curl` PROPFIND/REPORT/MKCALENDAR); an ActiveSync `OPTIONS` and `FolderSync`
  - five wrong SOGo logins get the client's address banned
  - `mailctl webmail sync` against Pebble (Let's Encrypt's test server) set in certbot's `cli.ini` on the VM, with the `webmail.*` names in the VM's resolver
  - playbook run twice: the second run changes nothing
- **Real Let's Encrypt** can only be checked on a real server with real DNS. That happens when you roll it out.

### Checked on the VMs before anything is built on it

These are the assumptions most likely to be wrong. The plan checks them first, and the design changes if one fails:

1. OpenLiteSpeed's proxy passes the DAV methods (`PROPFIND`, `REPORT`, `MKCALENDAR` and so on; its method table has them) and the `RequestHeader` values set in a context through to SOGo.
2. An exact `map` for `webmail.<domain>` wins over a catch-all `*` map already on a listener.
3. `DomainFieldName` alone keeps address book searches within the user's domain, without SOGo's per-domain configuration. If it doesn't, the fallback is `isAddressBook = NO`: no shared address book, and people type full addresses when sharing.
4. Purging Roundcube with the debconf answers above keeps its database.
5. `SOGoVacationPeriodEnabled` enforces the end date in the Sieve script itself, with no `sogo-tool` job and no Dovecot master user. If it doesn't, vacation replies have no end date and users switch them off themselves.
6. With the `acl` plugins loaded, `doveconf` accepts both Dovecot configs, Dovecot's `CAPABILITY` lists `ACL`, and delivery, Sieve `fileinto` and the full-text search index still work.
7. SOGo logs the client's address from `X-Forwarded-For` and not `127.0.0.1`. Otherwise the fail2ban jail can't ban anyone.

## Out of scope

- A certificate for `mail.<domain>`: Dovecot and Postfix present the `hostname` certificate, so a mail app set up with `mail.<domain>` gets a name mismatch. This isn't new and doesn't change here, but it would be a natural next step with the same certbot machinery.
- Email reminders for calendar events (`sogo-ealarms-notify`): they need an account to send from. Apps and SOGo's own pop-up reminders still work.
- ActiveSync autodiscover (`autodiscover.<domain>`): phones are set up with `webmail.<domain>` as the server.
- Moving Roundcube contacts or filters into SOGo.
- Sharing mail folders between users (needs a Dovecot shared namespace). Calendars and address books can be shared.
- Webmail on the server's own hostname.
- Managing the rest of OpenLiteSpeed's config (listeners, other sites) from Ansible.
