# roles/mail/files/private

This directory is gitignored (except this README). It is where you place the
learned **Bayes** spam database when migrating the mail server to a new host.

## Migrating learned spam (Bayes) to a new server

The mail role stores SpamAssassin's Bayes data in the MySQL `bayes` database, so
all the spam/ham learning lives there — not in files on disk. A fresh provision
creates an **empty** Bayes schema, so without this step the new server starts
with no learning at all.

### 1. Dump the Bayes database on the OLD server

```bash
mysqldump bayes > bayes_dump.sql
```

### 2. Place the dump here

Copy `bayes_dump.sql` into this directory:

```
roles/mail/files/private/bayes_dump.sql
```

The filename must be exactly `bayes_dump.sql`.

### 3. Run the playbook

The mail role automatically detects the dump and imports it into the new
server's `bayes` database after creating the schema. If the file is absent, the
import is skipped and Bayes simply starts empty — so it's safe to leave this
directory without a dump on a brand-new server.

### 4. Verify (on the new server)

```bash
sa-learn --dump magic
```

You should see your real `nspam` / `nham` counts rather than zeros.

> The dump can contain a large number of learned tokens but no secrets. It is
> gitignored mainly to keep the repository clean and avoid committing host data.
