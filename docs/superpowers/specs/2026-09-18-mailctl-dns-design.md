# Design: DNS records at TransIP and `mailctl doctor`

Date: 2026-09-18. Extends [the mailctl design](2026-09-14-mailctl-design.md).

## Goal

`mailctl` publishes a domain's mail records in its DNS at TransIP, including the SRV records mail programs use to find their server settings, and checks that the server and its domains are set up right.

How mail reaches the server:

- Mail for `DOMAIN` is delivered to `mail.DOMAIN` (MX), whose A and AAAA records point to this server.
- Mail programs use `mail.DOMAIN` for IMAP and submission.
- The server sends mail as its hostname (Postfix's `myhostname`): the hostname must point to the server, and the server's addresses back to the hostname (reverse DNS).
- Postfix and Dovecot present one certificate, `/etc/letsencrypt/live/HOSTNAME/fullchain.pem`, which includes the hostname and every `mail.DOMAIN`.

Autoconfig and autodiscover (the web addresses Thunderbird and Outlook try before the SRV records) are `mailctl autodiscover publish`, per domain; see the section at the end.

### The server's addresses

"This server's addresses" are the ones it sends mail from: the source addresses of its default IPv4 and IPv6 routes (`ip -json route get` for an internet address of each kind, field `prefsrc`), as the kernel picks them for Postfix. This replaces `ip -json address show scope global`, which also listed failover addresses and extra IPv6 addresses (SLAAC next to a static one) that mail doesn't use: publishing those for `mail.DOMAIN`, or requiring reverse DNS and SPF for them, would be wrong. A kind of address without a route is left out; no route at all is an error.

---

## Records

`dns_check.recommended_records` adds the SRV records of RFC 6186 and RFC 8314. The ones with implicit TLS have priority 0 and are preferred; STARTTLS has priority 10:

```
SRV _imaps._tcp.DOMAIN        0 1 993 mail.DOMAIN
SRV _imap._tcp.DOMAIN         10 1 143 mail.DOMAIN
SRV _submissions._tcp.DOMAIN  0 1 465 mail.DOMAIN
SRV _submission._tcp.DOMAIN   10 1 587 mail.DOMAIN
```

So `domain add` and `dns show` print them too. POP3 isn't offered (no records, as before).

---

## Commands

| Command | What it does |
|---|---|
| `dns show [DOMAIN]` | The records the domain needs, as `domain add` prints them |
| `dns publish [DOMAIN]` | Publishes them at TransIP: shows the changes, asks, then makes them. `--dry-run` only shows them, `--yes` doesn't ask |
| `dns credentials` | Enters or replaces the TransIP login and key |
| `doctor [DOMAIN]` | Checks the server, and every domain or the one given. Exit status 1 when a check finds a problem |

`domain add` also says, once the records are shown, whether the certificate includes `mail.DOMAIN` yet (a warning when it doesn't) and, when TransIP credentials are saved, the `dns publish` command. `status DOMAIN` shows the same domain checks as `doctor`.

---

## TransIP

### Credentials

mailctl asks for the TransIP login and key itself; Ansible doesn't deal with them.

- `dns credentials [--login LOGIN] [--key-stdin]` enters or replaces them. In a terminal it explains where to make the key pair (the API settings of the account in the control panel) and which addresses to whitelist (this server's), asks for the login and for the private key, pasted without being shown (read up to the `-----END` line). Without a terminal, the key comes from standard input with `--key-stdin`.
- `dns publish` without saved credentials asks for them the same way in a terminal, then continues. Without a terminal it fails with the `dns credentials` command to use.
- The key is normalised (line breaks lost or added in copying are fixed, as TransIP's own library does) and must be an unencrypted `PRIVATE KEY` or `RSA PRIVATE KEY`, readable by `openssl pkey`.
- Before saving, mailctl logs in with it (a read-only token) and makes a request (`GET /api-test`): TransIP may give out a whitelist-only token and refuse it when it's used. First with a token that only works from the addresses on the API whitelist, as TransIP's library does; when TransIP refuses that, with a token that works anywhere (`global_key`), which a key made without the whitelist requirement needs. When both are refused, the first refusal is shown and nothing is saved. Refusals are 400, 401 and 403 at login and 401 and 403 later, and point to `dns credentials`; other failures, like an outage, are shown as they are.
- The pasted key is read up to the line holding `-----END`, also when it's all on one line.
- Saved in `/etc/mailctl/transip.key` (the key) and `/etc/mailctl/transip.json` (`{"login": ..., "global_key": ...}`), both root-only (0600), written to a temp file and renamed. Their paths are `transip_key` and `transip_settings` in the config. A refused login later points to `dns credentials`.

### API

`core/transip.py` talks to `https://api.transip.nl/v6` with `urllib`:

- Token: `POST /auth` with `login`, a random `nonce`, `read_only` (true for `--dry-run`), `expiration_time` "30 minutes", a `label` naming mailctl and the hostname, and `global_key`. The `Signature` header is the base64 of `openssl dgst -sha512 -sign KEY` over the exact body sent. One token per command, requested on first use.
- `GET /domains/DOMAIN/dns` and `PUT /domains/DOMAIN/dns` (`{"dnsEntries": [...]}`) to read and replace the zone. The whole zone is sent in one request, so a change is never half made; entries mailctl doesn't change are sent back exactly as read.
- `GET /domains/DOMAIN/nameservers`: when not all of them are TransIP's (`*.transip.net`, `.nl`, `.eu`), a warning says the records at TransIP aren't what the internet sees.
- The zone: the domain's own, or else the nearest parent domain's in the account (a `404` means not in the account), so `shop.example.nl` goes into `example.nl` as `mail.shop`, `_dmarc.shop` and so on. None: an error pointing to `dns show`.
- Just before the `PUT`, the zone is read again; when it changed since it was shown (for example in the control panel), nothing is saved.
- Errors show TransIP's `error` message. A refused token gets a hint about the login, the key and the whitelist; `429` says to wait 15 minutes. A `409` ("DNS entries are currently being saved") on reading is retried three times, two seconds apart. On saving it isn't: the zone mailctl would save was read before the change TransIP is saving, so saving it would undo that change. Answers that aren't what the API documents (not JSON, missing fields, a `null` value) are an error, so a zone is never sent back with a value mailctl couldn't read.

### Changes (`core/zone.py`)

Entries use TransIP's notation: names relative to the zone (`@` for the zone's domain), host names in content relative or absolute with a final dot. mailctl writes absolute names. New entries get an expire of 3600 seconds.

Entries are compared by type and meaning: host names made absolute and lowercased, IP addresses parsed, TXT values unquoted (`"a" "b"` joined) and DKIM records by their `p=` key. A wanted record that is already there, with any expire, stays untouched.

For each wanted record, the existing entries it replaces:

| Record | Replaces at the same name |
|---|---|
| MX (`@`) | every MX |
| A, AAAA (`mail`) | every A, AAAA and CNAME |
| DKIM TXT (`mail._domainkey`) | every TXT and CNAME |
| SRV | every SRV and CNAME |
| SPF TXT (`@`) | the `v=spf1` TXT records only; other TXT records at `@` stay. One SPF record that already allows the server (a bare `mx`, or `ip4`/`ip6` covering every published address, before `all`) stays. One that doesn't gets the addresses it's missing as `ip4:`/`ip6:` terms after `v=spf1`, so other senders it allows keep working. Not `mx`: that takes a DNS lookup, and a record near SPF's limit of 10 would go over it and fail for every sender. Several (invalid) are replaced by `v=spf1 mx ~all` |
| DMARC TXT (`_dmarc`) | nothing when there is one DMARC record or a CNAME: the policy is the owner's choice. Several DMARC records (invalid) are replaced |
| (none) | `autoconfig` and `autodiscover` (A, AAAA, CNAME) and `_autodiscover._tcp` (SRV, CNAME) are removed: Thunderbird and Outlook look there before the SRV records, so a previous provider's records would set mail programs up for that provider |

`dns publish` needs the server's public addresses for `mail.DOMAIN` (it refuses private ones). Without a DKIM key it publishes the rest and warns. After publishing it says how long resolvers may still use the removed records (their longest expire), points to `mailctl doctor DOMAIN`, and warns when the certificate doesn't include `mail.DOMAIN`.

---

## Checks

### Domain (in `doctor` and `status DOMAIN`)

MX, SPF, DKIM and DMARC as before, plus:

- **SRV**: every recommended SRV record has a published record with target `mail.DOMAIN` and the right port (priority and weight don't matter). A record pointing elsewhere (or to `.`, "no service") is a problem; missing records are a warning, since mail still works. The records shown are the ones that aren't right.
- **Certificate**: the certificate includes `mail.DOMAIN` (exactly, or as a `*.DOMAIN` wildcard). Otherwise a problem: mail programs get a certificate warning. When the certificate can't be read, a warning.

### Server (`doctor` only)

- **Hostname**: the hostname resolves to each of the server's public addresses (all addresses when it has no public ones). Otherwise a problem, with the A and AAAA records to publish.
- **Reverse DNS**: each of those addresses has a PTR record with the hostname. Otherwise a problem: many receiving servers refuse mail from an address without matching reverse DNS. The provider of the server sets it.
- **Certificate**: read with `openssl x509 -noout -enddate -ext subjectAltName`. A problem when the file is missing or empty (Ansible creates empty files before certbot has run), expired, or without the hostname; a warning when it expires within 14 days (certbot renews 30 days ahead, so renewal is failing).

The certificate path is `certificate_file` in the config, default `/etc/letsencrypt/live/HOSTNAME/fullchain.pem`, the file Postfix and Dovecot use.

The resolver gets `srv(name)` and `ptr(address)`.

---

## Code

- `core/transip.py`: token, requests, errors. `send` is replaceable, so tests fake the API.
- `core/zone.py`: `Entry`, `plan(domain, entries, records) -> Plan(remove, add, unchanged, result)`.
- `core/certificate.py`: read and parse the certificate, name matching, the certificate checks.
- `core/dns_check.py`: SRV records and check, `check_server`.
- `core/system.py`: `run_binary` for the signature.
- `commands/dns.py`, `commands/doctor.py`; `commands/checks.py` holds the domain and server checks and their output, used by `status` and `doctor`.

Core imports: `certificate` → `dns_check`, `system`; `zone` → `dns_check`; `transip` → `zone`, `system`.

## Testing

- Unit: SRV records and check, server checks with the fake resolver; certificate parsing and wildcard matching from real `openssl` output of generated certificates; zone plans (replacing TransIP's defaults, keeping unrelated TXT records, SPF merging, DMARC kept, CNAME conflicts, relative and absolute names, quoted and split TXT values, IPv6 notation, nothing to do); the TransIP client against a fake API (signature verified with `openssl dgst -verify`, read-only tokens, error messages and hints, 409 retries).
- CLI: `dns show`, `dns publish` with `--dry-run`, `--yes` and without a terminal, not set up, unknown domain, foreign nameservers; `doctor` output and exit status; `domain add` notes.

---

## Autoconfig and autodiscover (`mailctl autodiscover publish DOMAIN`)

Added later the same day. Thunderbird asks `autoconfig.DOMAIN/mail/config-v1.1.xml`; Outlook posts to `autodiscover.DOMAIN/autodiscover/autodiscover.xml`, over HTTPS. A site at those names hands them the settings for `mail.DOMAIN`.

### OpenLiteSpeed

- **No `include`.** WebAdmin makes a server config that includes other files read-only (`ConfigDataLoader::loadPlainRoot`, `CData::IsReadOnly`), and the config is managed in WebAdmin. So mailctl edits `httpd_config.conf` itself (`core/openlitespeed.py`), in the layout WebAdmin writes (`CNode::PrintBuf`), and only its own blocks: a small parser finds blocks by their braces, skipping comments and `<<<MARKER` multi-line values (rewrite rules have braces; the end marker is the first word of a line, in any case, as OpenLiteSpeed reads it), and treats a line starting with `}` as a block's end. Template settings are replaced whole, multi-line values included.
- **Writing safely.** OpenLiteSpeed's installer gives `conf/` to `lsadm`, WebAdmin's user, and mailctl runs as root. So the config is opened with `O_NOFOLLOW` (a symbolic link is refused), the new config and the backup `httpd_config.conf.mailctl.bak` are written to new temp files and renamed into place (a link planted at either name is replaced, not followed), and owner and mode are copied onto the open file (`fchown`, `fchmod`).
- **Virtual host templates.** Each domain is a member `autodiscover.DOMAIN` (`vhDomain autodiscover.DOMAIN`, `vhAliases autoconfig.DOMAIN`) of one of two templates, which WebAdmin shows and manages too:
  - `mailautodiscover`: on the HTTP and HTTPS listeners, with `vhssl` at `/etc/letsencrypt/live/$VH_NAME/`
  - `mailautodiscover-http`: on the HTTP listeners only, for a domain without its certificate yet. A member on an HTTPS listener without a certificate of its own would get the listener's, and Outlook would show a certificate warning.
- mailctl adds the `vhTemplate` blocks when they're missing, and sets their `templateFile`, `listeners` (found by port: 80 with `secure 0`, 443 with `secure 1`) and `note` each time. Ansible installs the template files (`templates/openlitespeed-autodiscover.conf.j2` → `conf/templates/mailautodiscover.conf` and `mailautodiscover-http.conf`) and never touches `httpd_config.conf`.
- Template contents: web root `/var/www/mailautodiscover` (also certbot's; Ansible passes `autodiscover_root` to mailctl's config), no symlinks, restrained; a vhost-level lsapi app with the web role's lsphp83, which all members share (OpenLiteSpeed names vhost apps per user, `getUniAppName`, and reuses the first); two rewrite rules to the PHP scripts. `$VH_NAME` is expanded in the certificate paths (`getAbsolute`). OpenLiteSpeed won't serve a static file to a POST (`StaticFileHandler::notAllowed`), so Outlook's answer needs PHP.
- After a change: `lswsctrl restart` (graceful). Certbot's deploy hook restarts OpenLiteSpeed for renewed `autodiscover.*` certificates.

### The answers (`files/mailautodiscover/`)

The domain comes from the `Host` header (`autodiscover.`/`autoconfig.` stripped, checked to be a domain name; otherwise 404). Both answer with IMAP on 993 (implicit TLS) and SMTP on 465; Thunderbird also gets 143 and 587 with STARTTLS as alternatives. Outlook's login is the posted `EMailAddress`, its XML entities decoded and the result escaped. A request for another schema (phones asking for ActiveSync) gets a 404, so the phone asks the user; SOGo's ActiveSync is at `webmail.DOMAIN`.

### The command

1. The domain must be on this server; the templates must be installed and OpenLiteSpeed must have an HTTP listener on port 80 (and one on 443 for HTTPS).
2. **HTTPS first:** the certificate `/etc/letsencrypt/live/autodiscover.DOMAIN/fullchain.pem` must exist, not be expired, and include both names. That decides the template.
3. The DNS records: A/AAAA for both names at the server's addresses, through the same TransIP code as `dns publish` (asks for the login the first time). With `--no-dns`, or for a domain outside the TransIP account, the records are shown to publish by hand and the site is still set up.
4. Shows the site change and the DNS changes and asks once, for either (`--yes`, `--dry-run`). Then it checks that the config is still the one it read, writes it and restarts OpenLiteSpeed; when the restart fails, the previous config goes back, so the next run tries again. Then it saves the zone.
5. Without HTTPS: the reason, certbot's web root, the `certbot certonly --webroot` command for both names, and to run the command again afterwards.

`dns publish` and `dns show` include the two records for a domain that has the site (it's in either template), and otherwise remove a previous provider's.

### Not done

- Removing a domain's site (`domain delete` leaves it; remove its `member` block from the `mailautodiscover` templates in `httpd_config.conf`, or in WebAdmin once the config has no `include` lines).
- `doctor` checks for the site.
- ActiveSync autodiscover for phones, which could point them to SOGo at `webmail.DOMAIN`.
