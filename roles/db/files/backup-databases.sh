#!/usr/bin/env bash
# Writes a gzipped dump of every database on this server, and throws away the ones that are too old to keep.
#
#   backup-databases [DIRECTORY] [DAYS_TO_KEEP]
#
# Without a directory it writes to /home/dbbackups, where the nightly run puts them too: run by hand it should
# land where the backups live, not in the home directory of whoever happened to run it.
#
# Run as the backup user, which MariaDB knows by its Linux name (unix_socket): no password anywhere, and nothing
# to read out of a file or a process list. It may read every database and nothing else, so a dump is all it can do.
#
# A dump is written to a temporary name and renamed when it is whole, so a run that fails halfway leaves no file
# that looks like a backup. The old ones are only thrown away after a run in which every database was dumped:
# failing backups shouldn't quietly eat the last good ones.
#
# A systemd timer runs this every night; see mariadb-backup.timer.

set -euo pipefail

directory=${1:-/home/dbbackups}
keep_days=${2:-14}
stamp=$(date +%Y%m%d-%H%M%S)

say() { printf '%s\n' "$*"; }

client=$(command -v mariadb || command -v mysql) || { say "Neither mariadb nor mysql is installed." >&2; exit 1; }
dump=$(command -v mariadb-dump || command -v mysqldump) || { say "Neither mariadb-dump nor mysqldump is installed." >&2; exit 1; }

mkdir -p "$directory"
cd "$directory"
say "Writing to $directory, as $(id -un), keeping $keep_days days."

# What MariaDB keeps about itself: information_schema and performance_schema are made anew at every start, and
# sys is a set of views over them. The mysql database is dumped: it holds the accounts and their grants.
databases=$("$client" -N -B -e "SHOW DATABASES" </dev/null |
  grep -Ev '^(information_schema|performance_schema|sys)$' || true)
if [[ -z $databases ]]; then
  say "No databases to back up." >&2
  exit 1
fi

failed=0
for database in $databases; do
  file="$database-$stamp.sql.gz"
  # --single-transaction so the dump is one moment in time without locking the site out of its tables;
  # --no-tablespaces because reading those needs rights a backup user has no business having.
  if (umask 077; "$dump" --single-transaction --quick --no-tablespaces --events --routines --triggers \
      --default-character-set=utf8mb4 "$database" </dev/null | gzip -6 > "$file.part"); then
    mv "$file.part" "$file"
    say "$(du -h "$file" | cut -f1)	$file"
  else
    rm -f "$file.part"
    say "Dumping $database failed." >&2
    failed=$((failed + 1))
  fi
done

if (( failed )); then
  say "$failed of $(wc -w <<< "$databases") databases failed, so nothing older was thrown away." >&2
  exit 1
fi

# Only now: with every database dumped, what is older than this many days can go.
old=$(find "$directory" -maxdepth 1 -name '*.sql.gz' -type f -mtime "+$keep_days" -print -delete | wc -l)
say "Backed up $(wc -w <<< "$databases") databases; removed $old older than $keep_days days."
