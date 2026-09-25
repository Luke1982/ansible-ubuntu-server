#!/usr/bin/env bash
# Writes a mail server's domains, accounts, forwards, spam settings and Sieve filters to one JSON file, which
# 'mailctl import' reads on the new server. For a server without mailctl (with it, 'mailctl export' does the same).
#
# It reads the tables this role sets up (virtual_domains, virtual_users, virtual_aliases, virtual_sender_aliases and
# spamassassin.userpref) as root over MariaDB's socket, and the filters with doveadm. It changes nothing.
#
#   sudo ./export-mailserver.sh mailserver.json
#
# Needs jq and the mysql or mariadb client. The file holds the password hashes, so only root can read it.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: export-mailserver.sh [options] [FILE]

Writes this server's mail domains, accounts, forwards, spam settings and Sieve filters to FILE
(mailserver.json when left out), for 'mailctl import' on the new server.

Options:
  --mail-db NAME   The mail database (default: mailserver)
  --spam-db NAME   SpamAssassin's database (default: spamassassin)
  --no-sieve       Leave out the Sieve filters
  -h, --help       Show this help
EOF
}

say() { printf '%s\n' "$*" >&2; }
fail() { say "✗ $*"; exit 1; }

output=mailserver.json
mail_db=mailserver
spam_db=spamassassin
with_sieve=1
while (($#)); do
  case $1 in
    --mail-db) mail_db=${2:?--mail-db needs a name}; shift 2 ;;
    --spam-db) spam_db=${2:?--spam-db needs a name}; shift 2 ;;
    --no-sieve) with_sieve=0; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
    *) output=$1; shift ;;
  esac
done

[[ $(id -u) -eq 0 ]] || fail "Run it as root, with sudo: it reads the database as root and every account's filters."
command -v jq >/dev/null || fail "jq isn't installed. Install it with: apt install jq"
client=$(command -v mariadb || command -v mysql) || fail "Neither the mariadb nor the mysql client is installed."
if ((with_sieve)) && ! command -v doveadm >/dev/null; then
  fail "doveadm isn't installed, so the filters can't be read. Add --no-sieve to leave them out."
fi
for name in "$mail_db" "$spam_db"; do
  [[ $name =~ ^[A-Za-z0-9_]+$ ]] || fail "'$name' isn't a database name."
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The rows of a query as a JSON array of arrays. The client's batch mode writes tab-separated lines with tabs, line
# breaks, NULs and backslashes in values escaped; jq turns them back.
rows() {
  "$client" --batch --skip-column-names -e "$1" | jq -R -s '
    def unescape: gsub("\\\\(?<c>[\\\\nt0])";
      if .c == "n" then "\n" elif .c == "t" then "\t" elif .c == "0" then "\u0000" else "\\" end);
    split("\n") | map(select(length > 0) | split("\t") | map(unescape))'
}

rows "SELECT DISTINCT name FROM $mail_db.virtual_domains ORDER BY name" | jq 'map(.[0])' >"$work/domains"
rows "SELECT email, password FROM $mail_db.virtual_users ORDER BY email" \
  | jq 'map({address: .[0], password_hash: .[1]})' >"$work/addresses"

# A forward to itself keeps a copy in the mailbox; mailctl makes those itself. send_as says whether the destination
# may send as the source, which virtual_sender_aliases holds as a list of logins per address.
rows "SELECT source, destination FROM $mail_db.virtual_aliases
      WHERE LOWER(source) <> LOWER(destination) ORDER BY source, destination" >"$work/aliases"
rows "SELECT alias, users FROM $mail_db.virtual_sender_aliases" >"$work/senders"
jq -n --slurpfile aliases "$work/aliases" --slurpfile senders "$work/senders" '
  ($senders[0] | map({key: (.[0] | ascii_downcase), value: [.[1] | ascii_downcase | splits("[\\s,]+")]})
   | from_entries) as $allowed
  | $aliases[0] | map(. as [$source, $destination] | {
      source: $source, destination: $destination,
      send_as: (($allowed[$source | ascii_downcase] // []) | any(. == ($destination | ascii_downcase)))})' \
  >"$work/forwards"

# The whole server's settings are under $GLOBAL (or @GLOBAL when set up by hand), a domain's under %domain.
if [[ -n $("$client" --batch --skip-column-names -e "SHOW DATABASES LIKE '$spam_db'") ]]; then
  rows "SELECT username, preference, value FROM $spam_db.userpref ORDER BY prefid" | jq '
    map({target: (if .[0] == "$GLOBAL" or .[0] == "@GLOBAL" then "server" else .[0] | ltrimstr("%") end),
         setting: .[1], value: .[2]})' >"$work/spam_all"
  # mailctl imports settings for the server, a domain or an address; others, like a user's login name, stay here.
  jq 'map(select(.target == "server" or (.target | test("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$|^[^@\\s]+\\.[^@\\s]+$"))))' \
    "$work/spam_all" >"$work/spam"
  jq -r 'map(select(.target != "server" and (.target | test("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$|^[^@\\s]+\\.[^@\\s]+$") | not)))
         | .[] | "Left out the spam setting \(.setting) for \(.target): not an address or a domain."' \
    "$work/spam_all" >&2
else
  say "There's no database $spam_db, so there are no spam settings to export."
  echo '[]' >"$work/spam"
fi

: >"$work/sieve.jsonl"
if ((with_sieve)); then
  while IFS= read -r address; do
    if ! doveadm sieve list -u "$address" >"$work/list" 2>"$work/error"; then
      say "Couldn't read the filters of $address: $(tr '\n' ' ' <"$work/error")"
      continue
    fi
    while IFS= read -r line; do
      [[ -n ${line// /} ]] || continue
      name=${line% ACTIVE}
      active=false
      [[ $name != "$line" ]] && active=true
      if ! doveadm sieve get -u "$address" "$name" >"$work/script" 2>"$work/error"; then
        say "Couldn't read the filter $name of $address: $(tr '\n' ' ' <"$work/error")"
        continue
      fi
      sed -i '1{/^sieve script:$/d}' "$work/script"  # doveadm's field header, not part of the script
      jq -R -s --arg address "$address" --arg name "$name" --argjson active "$active" \
        '{address: $address, name: $name, active: $active, content: .}' <"$work/script" >>"$work/sieve.jsonl"
    done <"$work/list"
  done < <(jq -r '.[].address' "$work/addresses")
fi
jq -s . "$work/sieve.jsonl" >"$work/sieve"

umask 077
jq -n --slurpfile domains "$work/domains" --slurpfile addresses "$work/addresses" \
  --slurpfile forwards "$work/forwards" --slurpfile spam "$work/spam" --slurpfile sieve "$work/sieve" \
  '{version: 1, domains: $domains[0], addresses: $addresses[0], forwards: $forwards[0], spam: $spam[0],
    sieve: $sieve[0]}' >"$work/result"
# Also when the file was there already: it holds the password hashes.
install -m 600 "$work/result" "$output"

jq -r '"✓ Wrote \(.domains | length) domains, \(.addresses | length) accounts, \(.forwards | length) forwards, " +
       "\(.spam | length) spam settings and \(.sieve | length) filters to " + $file + "."' \
  --arg file "$output" "$output" >&2
say "  It holds the password hashes. Copy it to the new server and run: mailctl import $(basename "$output")"
