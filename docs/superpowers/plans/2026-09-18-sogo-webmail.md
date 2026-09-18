# SOGo webmail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The mail role installs SOGo (webmail, calendars, contacts, ActiveSync) instead of Roundcube, served at `https://webmail.<domain>` by OpenLiteSpeed, with `mailctl webmail sync` keeping the sites and certificates in step with the mail domains.

**Architecture:** Ansible installs and configures SOGo, Dovecot's ACL plugins, fail2ban and the hooks in OpenLiteSpeed's config (Part A). mailctl writes one OpenLiteSpeed site per domain whose `webmail.` record points to the server and gets its certificate with certbot (Part B). Part B builds on the TransIP/`doctor` work (spec `2026-09-18-mailctl-dns-design.md`), which changes the same mailctl files and isn't committed yet: Part B starts from that work once it is.

**Tech Stack:** Ansible (ansible-core 2.16 here), SOGo 5.12, OpenLiteSpeed 1.9, Dovecot 2.3/2.4, fail2ban, Python 3.12+/Typer/Rich (mailctl), pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-sogo-webmail-design.md`

## Global Constraints

- Ubuntu 24.04 and 26.04 both work; per-release differences live in `roles/mail/defaults/main.yml` maps.
- SOGo packages install without recommends (`apache2 | nginx` would take ports 80/443).
- `sogo_db_pass` only has letters, digits and `. _ ~ -`.
- SOGo listens on `sogo_address` (`127.0.0.1:20000`) only.
- SOGo is never served over plain http.
- mailctl's own conventions: messages in plain English, `MailctlError` with hint for expected failures, every command has `--help` with an example, tests with the throwaway MariaDB and `fake_command`.
- Work happens in `.worktrees/sogo-webmail` (branch `sogo-webmail`); never edit the main checkout, where another session is working.
- Verification scratch space: the session scratchpad (`$SP`); OpenLiteSpeed's `-t` also uses its hard-coded `/tmp/lshttpd`, removed at the end.

---

## Part A: Ansible (now)

### Task A1: SOGo defaults and checks

**Files:** Modify `roles/mail/defaults/main.yml`, `roles/mail/tasks/main.yml`

- [ ] Add `sogo_releases` (repository, packages, resources per release, exactly as in the spec), `sogo_release`, `sogo_address: 127.0.0.1:20000`, `sogo_workers: 10`, `sogo_language: Dutch`, `sogo_timezone: Europe/Amsterdam`.
- [ ] Release assert covers `sogo_releases` too.
- [ ] Assert `sogo_db_pass is defined and sogo_db_pass is match('^[A-Za-z0-9._~-]+$')`, message naming `mailvars.yml` and the allowed characters.
- [ ] Drop `roundcube`, `roundcube-plugins` from the package list; includes: `remove-roundcube.yml` and `configure-sogo.yml` after Dovecot, `configure-fail2ban.yml` after SOGo (its jail reads SOGo's log), `configure-webmail.yml` last.
- [ ] Verify: `ansible-playbook --syntax-check web01.whservice.nl.yml`.

### Task A2: Remove Roundcube

**Files:** Create `roles/mail/tasks/remove-roundcube.yml`; delete `tasks/configure-roundcube.yml`, `files/jail-roundcube-auth.conf`, `templates/autodiscover-vhost.conf.j2`; modify `tasks/configure-fail2ban.yml` (old jail file in the removal list).

- [ ] `package_facts`; when `roundcube-core` is installed, debconf `roundcube-core` `roundcube/dbconfig-remove` and `roundcube/purge` = false (dbconfig-common's questions for `dbc_go roundcube`, checked in roundcube-core 1.6.11's maintainer scripts).
- [ ] `apt state=absent purge=yes` for `roundcube roundcube-core roundcube-plugins roundcube-mysql roundcube-pgsql roundcube-sqlite3`.
- [ ] Verify: syntax check; `grep -rn -i roundcube roles/ README.MD inventory-example.yml` shows only intended mentions (removal task, comments, mailctl test fixtures that use "roundcube" as a script name).
- [ ] Commit.

### Task A3: SOGo install and configuration

**Files:** Create `roles/mail/tasks/configure-sogo.yml`, `roles/mail/templates/sogo.conf.j2`, `roles/mail/files/sogo-archive-key.asc`, `roles/mail/files/setup_sogo_view.sql`; modify `roles/mail/handlers/main.yml` (`restart sogo`).

- [ ] Key file: the key fetched from keys.openpgp.org, `gpg --show-keys` shows fingerprint `74FFC6D72B925A34B5D356BDF8A27B36A6E2EAE9`.
- [ ] Tasks: key + `apt_repository` (`signed-by=/etc/apt/keyrings/sogo.asc`, `{{ ansible_distribution_release }}` twice) when `sogo_release.repository | length > 0`; `apt` `sogo_release.packages + ['memcached']`, `install_recommends: no`; database `sogo` (utf8mb4) and user `sogo@localhost` with `sogo.*:ALL`; copy + import `setup_sogo_view.sql` as the other schemas; template `sogo.conf` (root:sogo 0640, notify restart sogo); `PREFORK={{ sogo_workers }}` in `/etc/default/sogo` (notify); cron line `* * * * * sogo /usr/sbin/sogo-tool expire-sessions 60 > /dev/null 2>&1` replacing the commented one; memcached and sogo enabled + started.
- [ ] `sogo.conf.j2`: every setting in the spec's table, database URLs with `sogo_db_pass`, plist syntax (`key = value;`, strings with specials quoted).
- [ ] View SQL exactly as the spec.
- [ ] Verify: syntax check; render `sogo.conf.j2` with test values through `ansible localhost -m template` into `$SP` and parse it with Python's `plistlib`-compatible check (OpenStep plist: balanced braces/parens, every entry ends in `;`) — a small parser script in `$SP`.

### Task A4: The SOGo view is right (test)

**Files:** Create `roles/mail/files/mailctl/tests/test_sogo_view.py`; modify `tests/conftest.py` only if a helper is needed.

- [ ] Test: create `sogo` database, import `setup_sogo_view.sql` as root, create a `sogo` user with `sogo.*:ALL`, insert a domain and two accounts in two domains; as `sogo`: `SELECT c_uid, c_name, c_cn, mail, c_domain, c_password` returns them with `c_domain` the part after `@`; `UPDATE sogo_users SET c_password = %s WHERE c_uid = %s` changes `mailserver.virtual_users.password` for that account only; `SELECT * FROM mailserver.virtual_users` as `sogo` is refused.
- [ ] Run on both venvs; commit A3 + A4 together.

### Task A5: Dovecot ACL

**Files:** Modify `roles/mail/templates/dovecot-local-2.3.conf.j2`, `dovecot-local-2.4.conf.j2`.

- [ ] 2.3: `mail_plugins = $mail_plugins fts fts_xapian acl`; `protocol imap { mail_plugins = $mail_plugins last_login imap_acl }`; `acl = vfile` in `plugin {}`; comment why (SOGo's ActiveSync needs the ACL capability; no ACL files, so owners keep full rights).
- [ ] 2.4: `acl = yes` in global and lmtp/imap `mail_plugins` blocks, `imap_acl = yes` in imap; `acl_driver = vfile`.
- [ ] Verify 2.3 with `doveconf -n -c` from noble's `dovecot-core` (+ `dovecot-imapd`, `dovecot-lmtpd`, `dovecot-sieve`, `dovecot-mysql`, `dovecot-fts-xapian`) unpacked in `$SP`, templates rendered; if the unpacked binary can't run here, say so and leave it to the VPS. 2.4: the VPS.
- [ ] Commit.

### Task A6: fail2ban for SOGo

**Files:** Create `roles/mail/files/jail-sogo-auth.conf`, `roles/mail/files/filter-sogo-auth.local`; modify `tasks/configure-fail2ban.yml`, `files/jail-dovecot.conf` (comment).

- [ ] Filter `.local`: `failregex` taking the last address before `' for user '…' might not have worked`: `^ sogod \[\d+\]: SOGoRootPage Login from '(?:.*[,'])?\s*<HOST>' for user '[^']*' might not have worked(?: - password policy: \d*  grace: -?\d*  expire: -?\d*  bound: -?\d*)?\s*$`.
- [ ] Jail: `[sogo-auth]` enabled, `backend = auto`, `port = http,https`, `logpath = /var/log/sogo/sogo.log`, maxretry 5, findtime 600, bantime -1.
- [ ] Tasks: copy jail and filter (notify restart fail2ban); make sure the log file exists (`copy content="" force=no`, owner sogo) so fail2ban starts even before SOGo logs anything.
- [ ] Verify with `fail2ban-regex` (from Ubuntu's `fail2ban` package, unpacked in `$SP`, stock `sogo-auth.conf` + our `.local`) against lines: plain client, `made-up, real`, `made-up,real`, a forwarded value with a quote in it, a successful-login line (no match). The banned address must always be the real one.
- [ ] Commit.

### Task A7: OpenLiteSpeed hooks

**Files:** Create `roles/mail/tasks/configure-webmail.yml`; modify `roles/mail/handlers/main.yml` (`restart openlitespeed`: `/usr/local/lsws/bin/lswsctrl restart`), `roles/mail/defaults/main.yml` (`ols_root: /usr/local/lsws`, `webmail_root: /var/www/webmail`).

- [ ] Tasks: stat + assert `httpd_config.conf` exists; slurp; listener names per port with `regex_findall('(?ms)^listener\\s+(\\S+)\\s*\\{[^}]*?^\\s*address\\s+\\S*:80\\s*$')` (and 443); assert at least one 443 listener; directory `conf/webmail` + empty `vhosts.conf`, `http-maps.conf`, `https-maps.conf` (`force: no`); `blockinfile` top-level include; `blockinfile` per listener with its name in the marker, `insertafter: '^listener\s+NAME\s*\{'`; document root with `.well-known/acme-challenge`; notify restart openlitespeed.
- [ ] Verify the listener regex in Python against: WebAdmin-style config with `listener Default{` on 8088, `HTTP` on `*:80`, `HTTPS` on `*:443`, a second `[::]:443` listener, a `map` line after the address.
- [ ] Verify the whole result with the unpacked `openlitespeed -t`: a copy of its stock `httpd_config.conf` with those listeners (spare ports), the blocks inserted exactly as `blockinfile` would (run the tasks against localhost with `ols_root` pointing into `$SP`, `--check` off, connection local).
- [ ] Commit.

### Task A8: Docs for Part A

**Files:** `README.MD`, `inventory-example.yml`, `roles/mail/vars/private/README.md`.

- [ ] Roles table: SOGo (webmail, calendars, contacts, ActiveSync) instead of Roundcube; `sogo_db_pass` documented with its character rule; inventory comment no longer mentions Roundcube.
- [ ] Commit.

---

## Part B: mailctl (after the TransIP/`doctor` work is committed on master)

Start: rebase `sogo-webmail` on master; run both test suites; read the committed `dns_check`, `checks.py`, `zone.py`, `config.py`, `conftest.py` and adjust the names below to what's there.

### Task B1: Config

- [ ] `Config`: `letsencrypt_email: str = ""`, `sogo_address: str = "127.0.0.1:20000"`, `sogo_resources: Path = Path("/usr/lib/GNUstep/SOGo/WebServerResources")`, `ols_root: Path = Path("/usr/local/lsws")`, `webmail_root: Path = Path("/var/www/webmail")`, `letsencrypt_dir: Path = Path("/etc/letsencrypt")`. Test config fixture points them into `tmp_path` (`ols_root` with `bin/lswsctrl` from `fake_command`'s directory).
- [ ] `install-mailctl.yml` writes `letsencrypt_email`, `sogo_address`, `sogo_resources`.
- [ ] Tests: `test_config.py` loads the new keys and their defaults.

### Task B2: Webmail records and "points here"

- [ ] `dns_check.webmail_host(domain) -> str` (`webmail.DOMAIN`); `recommended_records` adds A/AAAA for it and SRV `_caldavs._tcp`, `_carddavs._tcp` `0 1 443 webmail.DOMAIN`; the SRV check covers them.
- [ ] `dns_check.points_here(resolver, name, server_ips) -> set[IPAddress]`: the addresses of `name` that aren't the server's; raises `LookupFailed`; empty result only when the name has addresses and all are the server's (`NotPointing` otherwise, carrying `foreign` and `missing`). Final shape follows what reads best next to the committed code.
- [ ] Zone rules (TransIP): A/AAAA at `webmail` replace A, AAAA, CNAME at that name.
- [ ] Tests first, both venvs.

### Task B3: `core/webmail.py`

- [ ] `sites(config) -> list[str]` (hosts with a site file), `has_certificate(config, host) -> bool`, `render_site(config, host, secure) -> str`, `render_vhosts(config, hosts) -> str`, `render_maps(hosts) -> str`, `write(config) -> bool` (all generated files, atomic, returns whether anything changed), `restart(config)`, `wait_until_served(config, host, addresses)`, `request_certificate(config, host)`, `delete_certificate(config, host)`, `sync(config, domains, server_ips, resolver) -> list[Outcome]`, `remove(config, domain) -> bool`.
- [ ] `Outcome(host, state, detail, fixes)` with states live, new (certificate just issued), waiting (DNS), failed (certificate), removed, unknown (lookup failed).
- [ ] Tests first: the spec's table row by row; one failing certificate doesn't stop the others; lookup failure changes nothing; a domain gone from the database is removed with its certificate; map files; certbot arguments (`--config-dir`, `--webroot`, `--cert-name`, email or `--register-unsafely-without-email`); certbot's `Detail:` lines in the failure; no restart when nothing changed.
- [ ] The generated site config checked with `openlitespeed -t` and a running OpenLiteSpeed on spare ports with a stand-in backend (DAV methods, headers, ActiveSync path, redirects, exact-map precedence, http-only site serves only challenges).

### Task B4: Commands

- [ ] `commands/webmail.py`: `webmail sync` (lists outcomes; last line "Changed webmail for N domains." or "Webmail is up to date."), registered in `cli.py`, in `COMMANDS` of the CLI tests.
- [ ] `domain add` note; `domain delete` calls `webmail.remove` through `attempt`; Webmail check in the domain checks (status and doctor); logins heading.
- [ ] CLI tests for each.

### Task B5: Ansible for mailctl webmail

- [ ] `files/mailctl-webmail-sync.service` (oneshot, `ExecStart=/usr/local/bin/mailctl webmail sync`), `.timer` (`OnCalendar=daily`, `RandomizedDelaySec=1h`, `Persistent=true`); tasks in `configure-webmail.yml` to install and start them and run the sync (`changed_when: "'Changed webmail' in webmail_sync.stdout"`); deploy hook restarts OpenLiteSpeed for `*/webmail.*` lineages; `letsencrypt_email` default `""`.
- [ ] Docs: README mailctl table gets `webmail sync`; DNS records for webmail; `letsencrypt_email`.

### Task B6: Finish

- [ ] Full test suites on both venvs, syntax check, OpenLiteSpeed checks again.
- [ ] Review rounds until clean (see the user's request).
- [ ] Merge back: rebase on master, fast-forward master (only when the main checkout has nothing uncommitted in the files involved), remove the worktree; delete `/tmp/lshttpd`.
