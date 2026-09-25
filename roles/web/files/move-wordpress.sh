#!/usr/bin/env bash
# Copies a WordPress site, with a dump of its database, to another server.
#
# It asks for the site's directory (the one with wp-config.php; the current directory when you press Enter), reads
# the database credentials from its wp-config.php, dumps the database there, asks where the site goes, shows this
# user's public SSH key so you can add it to the other server's ~/.ssh/authorized_keys, and then rsyncs the whole
# directory across. It makes a key first when this user has none, and uses the existing one otherwise.
#
#   move-wordpress
#
# Afterwards it asks for the database name, user and password on the other server and puts them in the wp-config.php
# there. Then it offers to import the dump into that database, which drops it first when it exists, so it asks you to
# type its name before doing so.
#
# Needs rsync, ssh and mysqldump (or mariadb-dump). The dump is deleted here once it has been copied: it sits in the web
# root, where anyone could download it. The import deletes it on the other server; without one, remove it yourself.

set -euo pipefail

say() { printf '%s\n' "$*" >&2; }
fail() { say "✗ $*"; exit 1; }

ask() {
  local prompt=$1 default=${2-} answer
  while :; do
    read -rp "$prompt${default:+ [$default]}: " answer </dev/tty
    answer=${answer:-$default}
    [[ -n $answer ]] && { printf '%s' "$answer"; return; }
  done
}

source_dir=$(ask "WordPress directory to copy" "$PWD")
source_dir=${source_dir/#\~/$HOME}
cd "$source_dir" 2>/dev/null || fail "There's no directory $source_dir."
[[ -f wp-config.php ]] || fail "There's no wp-config.php in $PWD, so it isn't a WordPress directory."
command -v rsync >/dev/null || fail "rsync isn't installed. Install it with: apt install rsync"
command -v ssh >/dev/null || fail "ssh isn't installed. Install it with: apt install openssh-client"
dump_cmd=$(command -v mariadb-dump || command -v mysqldump) || fail "Neither mariadb-dump nor mysqldump is installed."

# One define('NAME', 'value') from wp-config.php, with either kind of quotes and \' \" \\ unescaped.
wp_setting() {
  sed -nE "s/^[[:space:]]*define[[:space:]]*\([[:space:]]*['\"]$1['\"][[:space:]]*,[[:space:]]*(['\"])(.*)\1[[:space:]]*\)[[:space:]]*;.*/\2/p" \
    wp-config.php | head -n1 | sed -E 's/\\([\\'\''"])/\1/g'
}

# The credentials of wp-config.php as a MySQL options file, so the password doesn't show up in the process list.
# DB_HOST can be 'host', 'host:port' or 'host:/path/to/socket'. Sets db_name, and fails when the file has none.
write_cnf() {
  db_name=$(wp_setting DB_NAME)
  local user pass host
  user=$(wp_setting DB_USER)
  pass=$(wp_setting DB_PASSWORD)
  host=$(wp_setting DB_HOST)
  if [[ -z $db_name || -z $user ]]; then
    printf '%s\n' "✗ DB_NAME and DB_USER couldn't be read from $PWD/wp-config.php." >&2
    return 1
  fi
  (
    umask 077
    {
      echo "[client]"
      echo "user=$user"
      echo "password=\"${pass//\"/\\\"}\""
      case ${host:-localhost} in
        *:/*) echo "host=${host%%:*}"; echo "socket=${host#*:}" ;;
        *:*) echo "host=${host%%:*}"; echo "port=${host##*:}" ;;
        *) echo "host=${host:-localhost}" ;;
      esac
    } > "$1"
  )
}

work=$(mktemp -d)
# The dump goes however the script ends: left in the web root, anyone could download it, and the next run would copy it.
dump=
trap 'rm -rf "$work"; [[ -z $dump ]] || rm -f "$dump"' EXIT
write_cnf "$work/my.cnf" || exit 1

dump="$db_name-$(date +%Y%m%d-%H%M%S).sql"
say "→ Dumping database '$db_name' to $dump"
(umask 077; "$dump_cmd" --defaults-extra-file="$work/my.cnf" --single-transaction --quick --no-tablespaces \
  --default-character-set=utf8mb4 "$db_name" > "$dump") || { rm -f "$dump"; fail "The database dump failed."; }

# This user's key: the first one there is, or a new ed25519 one.
key=
for candidate in ~/.ssh/id_ed25519 ~/.ssh/id_ecdsa ~/.ssh/id_rsa; do
  if [[ -f $candidate && -f $candidate.pub ]]; then key=$candidate; break; fi
done
if [[ -z $key ]]; then
  key=~/.ssh/id_ed25519
  say "→ There's no SSH key for $(whoami) yet; making one at $key"
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  ssh-keygen -q -t ed25519 -N "" -C "$(whoami)@$(hostname)" -f "$key"
else
  say "→ Using the SSH key at $key"
fi

say ""
remote_user=$(ask "User on the new server")
remote_host=$(ask "Host of the new server")
remote_port=$(ask "SSH port" 22)
remote_dir=$(ask "Directory on the new server")
remote_dir=${remote_dir%/}

say ""
say "Add this public key to ~$remote_user/.ssh/authorized_keys on $remote_host:"
say ""
cat "$key.pub" >&2
say ""
read -rsn1 -p "Press any key once it's there… " </dev/tty
say ""

ssh_opts=(-i "$key" -p "$remote_port" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
until ssh "${ssh_opts[@]}" -o BatchMode=yes "$remote_user@$remote_host" true; do
  say "✗ Couldn't log in to $remote_user@$remote_host with that key."
  say "  On $remote_host, ~$remote_user/.ssh must be 700 and authorized_keys 600, both owned by $remote_user."
  read -rsn1 -p "Press any key to try again, or q to stop… " reply </dev/tty
  say ""
  [[ $reply == q ]] && fail "Stopped. Nothing was copied."
done

say "→ Copying the site to $remote_user@$remote_host:$remote_dir/"
quoted_dir=$(printf '%q' "$remote_dir")
ssh "${ssh_opts[@]}" "$remote_user@$remote_host" "mkdir -p $quoted_dir"
# A wp-config.php already there has the new server's credentials, from an earlier run: leave it be.
excludes=()
if ssh "${ssh_opts[@]}" "$remote_user@$remote_host" "test -e $quoted_dir/wp-config.php"; then
  say "  $remote_dir/wp-config.php is already there, so it's kept, apart from the database settings."
  excludes=(--exclude=/wp-config.php)
fi
# Dumps of earlier runs that ended before theirs was deleted, so they don't end up on the new server too.
excludes+=(--include="/$dump" --exclude="/$db_name-[0-9]*-[0-9]*.sql")
# Directory times are left out: a directory there that belongs to someone else can't have them set.
status=0
rsync -az --omit-dir-times --info=progress2 "${excludes[@]}" -e "ssh $(printf '%q ' "${ssh_opts[@]}")" \
  ./ "$remote_user@$remote_host:$remote_dir/" || status=$?
case $status in
  0) ;;
  23|24)
    say "! Some files weren't copied; rsync names them above. A file or directory there that belongs to another user"
    say "  (root, say) can't be written: give it to $remote_user on $remote_host and run this again to copy the rest."
    ;;
  *) fail "Copying the site failed (rsync exit code $status)." ;;
esac
rm -f "$dump"
say "✓ The site and $dump are in $remote_dir on $remote_host; the dump here is deleted."


# Runs on the new server, in the site's directory. The values for 'configure' come before it on stdin, so the password
# never shows up in a process list.
#   settings     prints DB_NAME and DB_USER from the wp-config.php there
#   configure    sets DB_NAME, DB_USER and, when one was given, DB_PASSWORD in that wp-config.php
#   check        prints the database name and whether it exists, logging in with wp-config.php's credentials
#   import FILE  drops the database, makes it anew from FILE and deletes FILE
read -r -d '' remote_db_script <<'EOF' || true
set -euo pipefail
cd "$1"
case $2 in
  settings)
    wp_setting DB_NAME
    wp_setting DB_USER
    exit
    ;;
  configure)
    # Each value in single quotes, with \ and ' escaped the way PHP reads them back.
    N=$new_name U=$new_user P=$new_pass perl -pi -e '
      BEGIN { %v = (DB_NAME => $ENV{N}, DB_USER => $ENV{U}); $v{DB_PASSWORD} = $ENV{P} if length $ENV{P} }
      s{^\s*define\s*\(\s*([\x27"])(DB_NAME|DB_USER|DB_PASSWORD)\1\s*,.*$}{
        my $k = $2; exists $v{$k} ? do { (my $x = $v{$k}) =~ s/([\\\x27])/\\$1/g; "define( \x27$k\x27, \x27$x\x27 );" } : $&
      }e' wp-config.php
    [[ $(wp_setting DB_NAME) == "$new_name" && $(wp_setting DB_USER) == "$new_user" ]] ||
      { echo "✗ wp-config.php has no DB_NAME or DB_USER line to change." >&2; exit 1; }
    exit
    ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
write_cnf "$work/my.cnf"
client=$(command -v mariadb || command -v mysql) || { echo "✗ Neither the mariadb nor the mysql client is installed." >&2; exit 1; }
sql() { "$client" --defaults-extra-file="$work/my.cnf" -N -e "$1" </dev/null; }
case $2 in
  check)
    printf '%s\n' "$db_name"
    sql "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '${db_name//\'/\'\'}'"
    ;;
  import)
    quoted=\`${db_name//\`/\`\`}\`
    sql "DROP DATABASE IF EXISTS $quoted; CREATE DATABASE $quoted CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    "$client" --defaults-extra-file="$work/my.cnf" "$db_name" < "$3"
    rm -f "$3"
    ;;
esac
EOF
remote_db() {
  {
    declare -f wp_setting write_cnf
    printf 'new_name=%q new_user=%q new_pass=%q\n' "${new_name-}" "${new_user-}" "${new_pass-}"
    printf '%s\n' "$remote_db_script"
  } | ssh "${ssh_opts[@]}" "$remote_user@$remote_host" "bash -s -- $(printf '%q ' "$remote_dir" "$@")"
}

# The database settings for the new server, starting from what its wp-config.php says now.
{ read -r current_name && read -r current_user; } < <(remote_db settings) || true
say ""
say "The database on $remote_host (the user has to exist there already, with rights on that database):"
new_name=$(ask "Database name" "${current_name:-$db_name}")
new_user=$(ask "Database user" "${current_user:-$(wp_setting DB_USER)}")
read -rsp "Database password (Enter keeps the one in wp-config.php): " new_pass </dev/tty
say ""
remote_db configure || fail "Couldn't change the database settings in $remote_dir/wp-config.php."
say "✓ $remote_dir/wp-config.php on $remote_host now uses database '$new_name' and user '$new_user'."

finish() {
  say "  Import it there with: mysql $new_name < $dump (then delete $dump)."
  exit 0
}

say ""
read -rp "Import the dump into '$new_name' on $remote_host now? [y/N] " reply </dev/tty
[[ $reply == [yY]* ]] || finish

{ read -r remote_db_name && read -r remote_db_exists; } < <(remote_db check) || true
if [[ -z ${remote_db_name-} || -z ${remote_db_exists-} ]]; then
  say "✗ Couldn't log in to the database on $remote_host as '$new_user'. Is the user there, with that password?"
  finish
fi

say ""
if [[ $remote_db_exists != 0 ]]; then
  say "! The database '$remote_db_name' on $remote_host already exists. Importing DROPS it, with every table in it,"
  say "  and makes it anew from $dump. What's in it now can't be brought back."
else
  say "  The database '$remote_db_name' on $remote_host doesn't exist yet; it's made from $dump."
fi
read -rp "Type the database name to go ahead: " reply </dev/tty
[[ $reply == "$remote_db_name" ]] || { say "  That isn't '$remote_db_name', so nothing was changed."; finish; }

say "→ Importing $dump into '$remote_db_name' on $remote_host"
remote_db import "$dump" || { say "✗ The import failed. $dump is still in $remote_dir."; exit 1; }
say "✓ Done. '$remote_db_name' on $remote_host now holds the fresh dump, and $dump is deleted there."
