# Moving a server over Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move a customer off an old hand-built server onto one set up with this repository, driven from a single terminal on the old server.

**Architecture:** Two halves over one SSH connection. `migrate.py` is uploaded to the old server and runs there: it finds out what is on the machine, shows a dashboard, and drives the move. `migratectl` on the new server answers what it already has and does the work there by calling `mailctl` and `domainctl` as commands. They speak one JSON object per line over the connection; file and database copying go over the same connection with `rsync` and a piped dump, not through the protocol.

**Tech Stack:** Python 3 standard library only on both sides (no typer, no rich, no third-party modules anywhere in these two files). `mailctl` and `domainctl` keep their own stack and are called as commands. Tests: pytest, in `shared/tests/`.

**Spec:** [docs/superpowers/specs/2026-10-20-server-migration-design.md](../specs/2026-10-20-server-migration-design.md)

## Global Constraints

- `shared/migrate/migrate.py` is **one file, standard library only, Python 3.6 syntax**: no dataclasses, no walrus, no `X | Y` annotations, no `subprocess.run(capture_output=)`. It must run unchanged on an old server after `scp`.
- `shared/migrate/migratectl.py` is one file, standard library only; it may assume the Python of a server this repository built (3.12), and it calls `mailctl` and `domainctl` as commands, never imports them.
- Neither file writes anything on the old server except `~/.ssh/migrate_key*` and `/var/lib/migrate/state.json`.
- The old side connects as `ansible` and runs `sudo -n migratectl` on the new side. Never root over SSH.
- The JSON the old side writes for mail is the format in `roles/mail/files/mailctl/mailctl/core/transfer.py` (`version: 1`, `domains`, `addresses`, `forwards`, `spam`, `sieve`).
- Every message a person reads is plain language: what is wrong, what it means, what to do. No jargon, no history.
- Tests run from the repository: `cd shared && python -m pytest`, `cd roles/mail/files/mailctl && python -m pytest tests`, `cd roles/web/files/domainctl && python -m pytest tests`.

## Review Focus

Five things the spec implies that no task's main tests would otherwise exercise. Each has its test in the task that owns the code.

1. **An old server with no mail at all** (no MySQL, or none of the four tables): discovery reports what it looked for and the sites still move — Task 5.
2. **A `wp-config.php` that isn't neatly written** (single and double quotes, spaces inside `define(`, `DB_HOST` with a port or a socket path): either the credentials are read correctly or the site is reported as unreadable, never moved with wrong ones — Task 6.
3. **A password hash the new server can't check**: the dashboard asks for that account's password, Enter skips it, and a skipped account is named in the report — Task 10.
4. **A new server where the site's web root already holds files**: it asks first, and "no" leaves both sides exactly as they were — Task 8.
5. **The connection dropping in the middle of a site**: the state file never says a site moved when its files or database didn't, so a second run offers it again — Task 12.

---

### Task 1: mailctl imports the filters it can read

**Files:**
- Modify: `roles/mail/files/mailctl/mailctl/commands/transfer.py` (the `sieve.check` block in `import_server`)
- Modify: `roles/mail/files/mailctl/mailctl/commands/filters.py` (the same block in `import_filters`)
- Test: `roles/mail/files/mailctl/tests/test_cli_manage.py`, `roles/mail/files/mailctl/tests/test_transfer.py`

**Interfaces:**
- Consumes: `sieve.check(scripts: list[Script]) -> list[str]`, one line per filter that fails, starting with the filter's name.
- Produces: nothing new; `import_server` and `import_filters` keep their signatures.

- [ ] **Step 1: Write the failing tests**

```python
def test_import_takes_the_filters_it_can_read(mailctl, example_domain, tmp_path, fake_command):
    """One filter the server can't read costs that filter, not the whole file."""
    fake_command("sievec", 'case "$1" in *broken*) echo "line 1: syntax error" >&2; exit 1;; esac')
    # a file with two accounts, one filter each, one of them broken
    output = mailctl.ok("import", str(server_file), "--yes")
    assert "broken" in output and "line 1: syntax error" in output
    assert "Imported" in output
    # the good filter is there, the account of the broken one exists without it
```

```python
def test_filters_import_takes_the_filters_it_can_read(mailctl, example_domain, tmp_path, fake_command):
    """Same for 'mailctl filters import': the file is not refused over one filter."""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd roles/mail/files/mailctl && python -m pytest tests -k "takes_the_filters_it_can_read" -v`
Expected: FAIL — the import raises `MailctlError` and nothing is created.

- [ ] **Step 3: Change both commands**

Where each raises over `problems`, instead: drop the filters whose name appears in a problem line, `ui.warn` one line per dropped filter (`Left out the filter NAME of ADDRESS: REASON`), and carry on with the rest. When every filter is dropped, say so and import the rest of the file. The accounts, domains and forwards are never affected by a filter.

- [ ] **Step 4: Run the full mailctl suite**

Run: `cd roles/mail/files/mailctl && python -m pytest tests -q`
Expected: PASS, including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add roles/mail/files/mailctl
git commit -m "fix(mailctl): import the filters it can read, name the ones it can't"
```

---

### Task 2: `domainctl wp adopt`

**Files:**
- Modify: `roles/web/files/domainctl/domainctl/commands/wordpress.py` (new `adopt` command)
- Modify: `roles/web/files/domainctl/domainctl/core/wordpress.py` (new `adopt_config`, `import_dump`)
- Test: `roles/web/files/domainctl/tests/test_wordpress.py`, `roles/web/files/domainctl/tests/test_cli.py`

**Interfaces:**
- Consumes: `wordpress.create_database(name, password)`, `wordpress.database_taken(name) -> str | None`, `wordpress.drop_database(name)`, `config.docroot_of(user)`.
- Produces:
  - `core.wordpress.adopt_config(config, user: str, database: str, db_user: str, password: str) -> None` — changes `DB_NAME`, `DB_USER`, `DB_PASSWORD` and `DB_HOST` in the site's existing `wp-config.php` and leaves every other line, including the salts and `$table_prefix`, as it is.
  - `core.wordpress.import_dump(database: str, dump: IO[str]) -> None` — reads a dump (a file or standard input) into that database.
  - CLI: `domainctl wp adopt USER [--database NAME] [--dump FILE] [--replace] [--yes]`, where `--dump -` reads standard input and `--replace` empties a database that is already there, after asking unless `--yes`.

- [ ] **Step 1: Write the failing tests**

```python
def test_adopt_keeps_everything_in_wp_config_but_the_database_lines(config):
    """Salts, table prefix and a site's own constants are what makes it that site."""
    # wp-config.php with single quotes, double quotes, spaces in define( , salts and WP_HOME
    wordpress.adopt_config(config, "example", "example_db", "example_db", "secret")
    text = (config.docroot_of("example") / "wp-config.php").read_text()
    assert "define( 'DB_NAME', 'example_db' );" in text
    assert "$table_prefix = 'xyz_';" in text and "WP_HOME" in text and "AUTH_KEY" in text

def test_adopt_refuses_a_database_that_is_already_there(domainctl, ...):
    assert "already" in domainctl.fails("wp", "adopt", "example")

def test_adopt_replaces_a_database_when_told_to(domainctl, ...):
    assert "Replaced" in domainctl.ok("wp", "adopt", "example", "--replace", "--yes", "--dump", str(dump))

def test_adopt_reads_the_dump_from_standard_input(domainctl, ...):
    """So the old server can pipe mysqldump straight into it over SSH."""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd roles/web/files/domainctl && python -m pytest tests -k adopt -v`
Expected: FAIL — no such command.

- [ ] **Step 3: Implement `adopt_config`, `import_dump` and the `adopt` command**

`adopt_config` rewrites only the four `define()` lines, matching either quote and any spacing, the way `move-wordpress.sh` does; a `wp-config.php` without a `DB_NAME` line is an error naming the file. `import_dump` pipes into `mariadb <database>`. The command: database name defaults to the site's user name, the password is `wordpress.new_password()`, and it ends by saying the database, the user and that the site's `wp-config.php` now points at it.

- [ ] **Step 4: Run the domainctl suite**

Run: `cd roles/web/files/domainctl && python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add roles/web/files/domainctl
git commit -m "feat(domainctl): wp adopt, for a WordPress site that comes from another server"
```

---

### Task 3: `migratectl` and the protocol

**Files:**
- Create: `shared/migrate/migratectl.py`
- Create: `shared/tests/test_migratectl.py`
- Create: `roles/basic/tasks/setup-migratectl.yml`
- Modify: `roles/basic/tasks/main.yml` (include it)

**Interfaces:**
- Produces:
  - `answer(request: dict) -> dict` — one request in, one answer out. Every answer has `ok: bool`; a failure has `error: str` in plain language.
  - `main()` — reads one JSON object per line from standard input, writes one JSON object per line to standard output, and never writes anything else to standard output.
  - Request `{"request": "hello"}` → `{"ok": true, "version": 1, "server": "<hostname>", "tools": {"mailctl": true, "domainctl": false}}`.
  - Installed as `/usr/local/bin/migratectl`, mode 0755, by the basic role.

- [ ] **Step 1: Write the failing tests**

```python
def test_hello_says_which_tools_the_server_has(fake_command):
    answer = migratectl.answer({"request": "hello"})
    assert answer["ok"] is True and answer["version"] == 1
    assert answer["tools"]["mailctl"] is True

def test_a_request_it_doesnt_know_is_an_error_not_a_crash():
    assert migratectl.answer({"request": "dance"})["error"].startswith("I don't know")

def test_every_line_in_is_a_line_out(capsys):
    """Two requests on standard input, two answers on standard output, nothing else."""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd shared && python -m pytest tests/test_migratectl.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement `answer`, `main` and the Ansible task**

Dispatch on `request`; unknown requests and any exception become `{"ok": false, "error": ...}` so one bad item never kills the connection. The Ansible task copies the file to `/usr/local/bin/migratectl`.

- [ ] **Step 4: Run the tests**

Run: `cd shared && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/migrate shared/tests roles/basic
git commit -m "feat(migrate): migratectl answers one request per line on the new server"
```

---

### Task 4: `migratectl status` — what the new server already has

**Files:**
- Modify: `shared/migrate/migratectl.py`
- Test: `shared/tests/test_migratectl.py`

**Interfaces:**
- Produces: request `{"request": "status", "items": [...]}` → `{"ok": true, "items": {"<id>": {"state": "new|exists|refused", "detail": "..."}}}`, where an item is `{"id": "mail:example.nl", "kind": "mail_domain", "name": "example.nl"}`, `{"id": "site:example.nl", "kind": "site", "name": "example.nl", "user": "example"}` or `{"id": "cron:example:0", "kind": "cron", "user": "example"}`.
- Consumes: `mailctl domain list`, `domainctl list`, `wordpress.database_taken` through `domainctl` (never MariaDB directly).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_mail_domain_the_server_has_is_exists_with_what_is_in_it(fake_command):
    assert status("mail:example.nl")["state"] == "exists"
    assert "accounts" in status("mail:example.nl")["detail"]

def test_a_site_whose_user_is_taken_by_another_site_is_refused(fake_command):
    assert status("site:example.nl")["state"] == "refused"

def test_a_server_without_mailctl_refuses_mail_items(fake_command):
    """A web-only server says so instead of failing on a command that isn't there."""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd shared && python -m pytest tests/test_migratectl.py -k status -v`
Expected: FAIL.

- [ ] **Step 3: Implement the `status` handler**

- [ ] **Step 4: Run the tests** — `cd shared && python -m pytest -q`, PASS.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(migrate): the new server says what it already has"
```

---

### Task 5: Finding the mail on the old server

**Files:**
- Create: `shared/migrate/migrate.py` (first content: the discovery of mail)
- Create: `shared/tests/test_migrate_mail.py`

**Interfaces:**
- Produces, in `migrate.py`:
  - `mail_database(run) -> str or None` — the database that has all four tables, or None.
  - `mail_inventory(run, database, domain=None) -> Mail` — `.file` is what `mailctl import` reads, for one domain or all of them, with `version`, `domains`, `addresses`, `forwards`, `spam`, `sieve` and nothing else; `.left_out` and `.notes` sit beside it, never inside it, because a file with an unknown key is refused whole.
  - `filters_of(run, address) -> list` — `doveadm sieve list`/`get`, falling back to the account's sieve directory.
  - `run(*args, stdin=None) -> str` — the one place a command is run; tests pass their own.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_mail_database_is_the_one_with_the_four_tables():
def test_the_inventory_is_the_file_mailctl_reads():
    """Same keys, same shapes, so mailctl's own reader takes it."""
def test_one_domain_leaves_the_others_out():
def test_a_server_without_those_tables_says_what_it_looked_for():   # Review Focus 1
    assert mail_database(no_mysql) is None
def test_a_catch_all_address_is_left_out_and_reported():
```

- [ ] **Step 2: Run them to verify they fail** — `cd shared && python -m pytest tests/test_migrate_mail.py -v`, FAIL.

- [ ] **Step 3: Implement the discovery**

Queries as in `roles/mail/files/export-mailserver.sh`, in Python: the client's `--batch --skip-column-names` output, splitting on tabs and unescaping `\n`, `\t`, `\0`, `\\`. Catch-alls (`@domain`) and addresses with no local part are left out of `addresses`/`forwards` and returned under `left_out` with the reason.

- [ ] **Step 4: Run the tests** — PASS.

- [ ] **Step 5: Commit** — `feat(migrate): find the mail setup on the old server`

---

### Task 6: Finding the sites, their WordPress and their databases

**Files:**
- Modify: `shared/migrate/migrate.py`
- Create: `shared/tests/test_migrate_sites.py`

**Interfaces:**
- Produces:
  - `sites(run) -> list` — one dict per vhost: `name`, `aliases`, `docroot`, `wordpress` (bool), `database`, `db_user`, `db_password`, `prefix`, `size`, `problem`.
  - `wp_settings(path) -> dict or None` — the four `define()` values and `$table_prefix` from a `wp-config.php`.
  - `database_sizes(run) -> dict` — bytes per database.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_sites_come_from_apaches_own_view_of_itself():     # apache2ctl -S output as a fixture
def test_a_site_with_wp_config_is_wordpress_with_its_database():
def test_wp_config_is_read_whatever_the_quoting(tmp_path):     # Review Focus 2
    """Single and double quotes, spaces in define(, DB_HOST with a port and with a socket."""
def test_a_wp_config_it_cannot_read_marks_the_site_as_a_problem():  # never half-read credentials
def test_a_site_without_wp_config_is_reported_and_not_wordpress():
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.

- [ ] **Step 3: Implement the discovery**

`apache2ctl -S` first; when it isn't there, every `*.conf` under `/etc/apache2/sites-enabled/` read for `ServerName`, `ServerAlias` and `DocumentRoot`. `DB_HOST` keeps whatever it says (`host`, `host:port`, `host:/socket`).

- [ ] **Step 4: Run the tests** — PASS.

- [ ] **Step 5: Commit** — `feat(migrate): find the sites and the databases behind them`

---

### Task 7: Finding the cron jobs

**Files:**
- Modify: `shared/migrate/migrate.py`
- Create: `shared/tests/test_migrate_cron.py`

**Interfaces:**
- Produces: `cron_jobs(run, sites) -> list` — one dict per job: `user`, `line`, `site` (the site its paths point into, or None), `interpreter`, `problem`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_job_is_matched_to_the_site_its_paths_point_into():
def test_a_job_that_points_nowhere_known_is_kept_with_no_site():
def test_the_php_of_a_panel_is_recognised_as_the_interpreter():   # /opt/plesk/php/8.1/bin/php
def test_comments_and_empty_lines_are_not_jobs():
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement it** — `crontab -l -u USER` per user with a shell, plus every file in `/etc/cron.d`.
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): find the cron jobs and what they belong to`

---

### Task 8: `migratectl apply` for a site

**Files:**
- Modify: `shared/migrate/migratectl.py`
- Test: `shared/tests/test_migratectl.py`

**Interfaces:**
- Produces:
  - `{"request": "apply", "item": {...site...}, "answers": {...}}` → `{"ok": true, "docroot": "/home/example/public_html/www", "database": "example", "db_user": "example", "password": "...", "steps": [...]}`, or `{"ok": false, "ask": {"question": "...", "about": "files"}}` when the web root already holds files or the database is already there.
  - `{"request": "finish_site", "item": {...}}` → gives the files to the site's user and runs `domainctl sync`.
- Consumes: `domainctl add DOMAIN --user USER --no-dns --yes`, `domainctl wp adopt` (Task 2), `domainctl sync`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_new_site_is_made_and_says_where_the_files_go(fake_command):
def test_a_web_root_with_files_asks_before_anything_is_touched(fake_command):   # Review Focus 4
    answer = migratectl.answer(apply_site)
    assert answer["ok"] is False and answer["ask"]["about"] == "files"
    assert fake_command_calls("rsync") == []        # nothing happened
def test_answering_no_leaves_both_sides_as_they_were(fake_command):
def test_finish_gives_the_files_to_the_site_user(fake_command):
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement the handlers.**
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): the new server makes the site and says where the files go`

---

### Task 9: `migratectl apply` for a mail domain and for cron

**Files:**
- Modify: `shared/migrate/migratectl.py`
- Test: `shared/tests/test_migratectl.py`

**Interfaces:**
- Produces:
  - `apply` for `kind: "mail_domain"`: writes the JSON it is given to a file readable only by root, runs `mailctl import FILE --yes --verbose`, deletes the file, and answers with what mailctl said, plus `needs_password: [address, ...]` taken from mailctl's own output.
  - `apply` for `kind: "mail_passwords"`: `{"passwords": {address: password}}` → `mailctl address password` per account, so a password typed on the old server is hashed here.
  - `apply` for `kind: "cron"`: installs the job for the site's user with the paths and interpreter it is given, running from that moment.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_mail_domain_is_imported_and_the_file_is_deleted(fake_command, tmp_path):
def test_the_file_is_never_readable_by_anybody_else(fake_command):   # it holds hashes
def test_an_account_that_needs_a_password_is_named_back(fake_command):
def test_a_password_typed_on_the_old_server_is_hashed_here(fake_command):
def test_a_cron_job_is_installed_for_the_sites_user(fake_command):
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement the handlers.**
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): the new server imports a domain and installs a cron job`

---

### Task 10: The connection, the key and the handshake

**Files:**
- Modify: `shared/migrate/migrate.py`
- Create: `shared/tests/test_migrate_connection.py`

**Interfaces:**
- Produces:
  - `Connection(host, user="ansible", key=None)` with `ask(request: dict) -> dict`, which raises `Stopped` with a plain message when the connection fails.
  - `own_key() -> str` — makes `~/.ssh/migrate_key` when there is none and returns the public key to paste.
  - `check(connection) -> dict` — the `hello` answer, or a message saying the key still has to be pasted into `/home/ansible/.ssh/authorized_keys` on the new server.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_key_is_made_once_and_kept(tmp_path):
def test_a_refused_connection_says_to_paste_the_key(fake_ssh):
def test_the_handshake_refuses_a_new_server_that_speaks_another_version(fake_ssh):
def test_an_account_whose_password_is_needed_is_asked_for_and_enter_skips_it(fake_ssh, monkeypatch):  # Review Focus 3
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement it** — one `ssh` process per request (`ssh -i key ansible@host sudo -n migratectl`), input and output one JSON line.
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): the old server talks to the new one over its own key`

---

### Task 11: The dashboard

**Files:**
- Modify: `shared/migrate/migrate.py`
- Create: `shared/tests/test_migrate_dashboard.py`

**Interfaces:**
- Produces:
  - `dashboard(inventory, status) -> str` — the list as it is printed: one line per item, the kind, the name, what the new server said, and what it will cost (files, database size).
  - `pick(lines, typed) -> list` — which items the numbers, ranges and `all` the person typed come down to.
  - `run(connection, items, ask) -> Report` — moves them one by one, calling `ask` when the new server asks something.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_item_is_on_one_line_within_eighty_columns():
def test_what_the_new_server_already_has_is_shown_but_not_offered():
def test_numbers_ranges_and_all_pick_the_right_items():
def test_a_question_from_the_new_server_stops_only_that_item():
def test_one_item_that_fails_does_not_stop_the_rest():
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement it** — plain text, no curses; 80 columns.
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): the dashboard on the old server`

---

### Task 12: Moving a site's files and database, and doing it again

**Files:**
- Modify: `shared/migrate/migrate.py`
- Test: `shared/tests/test_migrate_sites.py`

**Interfaces:**
- Produces:
  - `move_site(connection, site, again=False) -> dict` — asks the new server for the site, rsyncs the files (`rsync -az --omit-dir-times -e "ssh -i KEY" --rsync-path "sudo -n rsync"`), pipes `mysqldump` into `sudo -n domainctl wp adopt --dump -` over the same connection, then asks the new server to finish the site.
  - `State` in `/var/lib/migrate/state.json`: an item is written as moved only when every step of it answered ok.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_files_go_with_rsync_as_root_on_the_other_side():
def test_the_dump_is_piped_and_never_written_to_disk():
def test_a_second_run_offers_a_site_whose_files_did_not_arrive():   # Review Focus 5
def test_doing_it_again_replaces_the_database_only_when_told():
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): move a site's files and database, and again before the flip`

---

### Task 13: The report at the end

**Files:**
- Modify: `shared/migrate/migrate.py`
- Create: `shared/tests/test_migrate_report.py`

**Interfaces:**
- Produces: `report(result) -> str` — what moved, what was skipped and why, then the `imapsync` line per account (with the password left to fill in), the DNS records each domain needs (`mailctl dns show DOMAIN` run on the new server through the connection), the reminder to run `domainctl add` again per site once DNS points at the new server, and the lines a person still has to do by hand: catch-alls, send-as permissions without a forward, non-WordPress sites, databases that belong to no site.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_imapsync_line_is_printed_for_every_account_that_moved():
def test_what_was_left_out_is_named_with_the_reason():
def test_a_run_that_moved_nothing_says_so_and_prints_no_lines():
```

- [ ] **Step 2: Run them to verify they fail** — FAIL.
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Commit** — `feat(migrate): the report that ends a run`

---

### Task 14: Putting it in the hands of whoever runs it

**Files:**
- Modify: `README.MD` (a section: moving a server over)
- Modify: `shared/migrate/migrate.py` (`--help`, and `main()`)
- Test: `shared/tests/test_migrate_dashboard.py`

**Interfaces:**
- Produces: `migrate.py [--host NEW] [--domain example.nl] [--again] [--help]`, run as root on the old server.

- [ ] **Step 1: Write the failing test**

```python
def test_help_says_how_to_get_the_file_onto_the_old_server_and_what_it_needs():
```

- [ ] **Step 2: Run it to verify it fails** — FAIL.
- [ ] **Step 3: Write `main()` and the README section**

The README says: copy `shared/migrate/migrate.py` to the old server, run it as root, paste the key it shows into `/home/ansible/.ssh/authorized_keys` on the new server, and what it does and doesn't move.

- [ ] **Step 4: Run every suite**

Run: `cd shared && python -m pytest -q && cd ../roles/mail/files/mailctl && python -m pytest tests -q && cd ../../../web/files/domainctl && python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `docs: moving a server over`
