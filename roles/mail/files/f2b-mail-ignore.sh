#!/bin/bash
# fail2ban ignorecommand for the mail jails. Installed by
# roles/mail/tasks/configure-fail2ban.yml; settings in /etc/default/f2b-mail-ignore.
#
# Separates a brute-force source from a legitimate user whose mail client still
# has an old password saved. Both look the same in the log: repeated auth
# failures, forever.
#
# EXEMPT an address only if all three hold within the log window:
#   1. it has >= MIN_SUCCESS successful logins         -> an established device
#   2. it fails on <= MAX_FAIL_USERS distinct accounts -> not spraying
#   3. it tries <= MAX_INVALID_USERS distinct accounts that don't exist
#                                                      -> not enumerating
#
# Condition 1 carries most of the weight. Sources that already hold a working
# password -- bought, phished or stuffed -- log in first try and generate no
# auth failures at all, so they never reach this jail. (Checked against a real
# compromise: of 73 confirmed stuffing addresses, not one ever failed an auth.)
# The jail therefore only ever sees guessers, which have no successful login,
# and real devices, which have plenty. Conditions 2 and 3 stop an address that
# cracked one mailbox from sheltering behind its own successes while guessing
# at others.
#
# Condition 2 counts DISTINCT accounts, not attempts, so the common real case
# stays exempt: one phone with three mailboxes configured, one of them holding
# a stale password and retrying it forever. That account never succeeds from
# that address, only fails, so a stricter rule -- "every account that fails
# here must also have succeeded here" -- bans the customer.
#
# Thresholds were fitted to one server's traffic and replayed over 30 days of
# its mail log: 17/17 known brute-force addresses banned, 15/15 known customer
# addresses exempt. Re-validate against real logs before changing them.
#
# Exit 0 = ignore (do not ban). Exit non-zero = go ahead and ban.

IP="$1"
[ -z "$IP" ] && exit 1

# Where the settings live. Override to test, or to run a second configuration.
SETTINGS="${F2B_MAIL_SETTINGS:-/etc/default/f2b-mail-ignore}"
# shellcheck source=/dev/null
[ -r "$SETTINGS" ] && . "$SETTINGS"

MIN_SUCCESS="${MIN_SUCCESS:-2}"
MAX_FAIL_USERS="${MAX_FAIL_USERS:-3}"
MAX_INVALID_USERS="${MAX_INVALID_USERS:-1}"
LOG_DAYS="${LOG_DAYS:-30}"
LOGS="${LOGS:-/var/log/mail.log /var/log/mail.log.1}"
VALID_CACHE="${VALID_CACHE:-/var/lib/fail2ban/valid-mail-users.txt}"
MYCNF="${MYCNF:-/etc/postfix/virtual-mailbox-maps.cf}"

# Refresh the list of accounts that exist, at most once an hour. Postfix's map
# already holds credentials for the mail database, so there is no second copy
# of the password to keep in step.
if [ ! -s "$VALID_CACHE" ] || [ -n "$(find "$VALID_CACHE" -mmin +60 2>/dev/null)" ]; then
    DBU=$(grep -oP '^user\s*=\s*\K.*'     "$MYCNF" 2>/dev/null)
    DBP=$(grep -oP '^password\s*=\s*\K.*' "$MYCNF" 2>/dev/null)
    DBN=$(grep -oP '^dbname\s*=\s*\K.*'   "$MYCNF" 2>/dev/null)
    # Ubuntu ships MariaDB's client as either name.
    CLIENT=$(command -v mysql || command -v mariadb)
    if [ -n "$DBU" ] && [ -n "$CLIENT" ]; then
        tmp=$(mktemp) || exit 1
        if "$CLIENT" -u"$DBU" -p"$DBP" "$DBN" -N -B \
             -e "SELECT email FROM virtual_users;" > "$tmp" 2>/dev/null \
           && [ -s "$tmp" ]; then
            install -m 600 "$tmp" "$VALID_CACHE"
        fi
        rm -f "$tmp"
    fi
fi
# Fail closed: without the account list condition 3 cannot be judged, so ban.
[ -s "$VALID_CACHE" ] || exit 1

# Dovecot's login lines for this address. Ubuntu's mail log is the systemd
# journal unless rsyslog is installed, so fall back to it; the message text is
# the same either way.
mail_lines() {
    local present="" f
    for f in $LOGS; do
        [ -r "$f" ] && present="$present $f"
    done
    if [ -n "$present" ]; then
        grep -F -h -- "$IP" $present 2>/dev/null
    else
        journalctl -u dovecot --since "-${LOG_DAYS} days" \
                   --output=cat --no-pager 2>/dev/null |
            grep -F -- "$IP"
    fi
}

mail_lines |
gawk -v ip="$IP" \
     -v min_succ="$MIN_SUCCESS" \
     -v max_fu="$MAX_FAIL_USERS" \
     -v max_iu="$MAX_INVALID_USERS" \
     -v validfile="$VALID_CACHE" '
  BEGIN {
      while ((getline line < validfile) > 0) if (line != "") valid[line] = 1
      close(validfile)
  }
  {
      # Only lines this address produced. Compared as text, so that dots are
      # not wildcards and one address is not a prefix of another.
      if (!match($0, /rip=[^,[:space:]]+/)) next
      if (substr($0, RSTART + 4, RLENGTH - 4) != ip) next

      if (!match($0, /user=<[^>]*>/)) next
      user = substr($0, RSTART + 6, RLENGTH - 7)
      if (user == "") next
  }
  # Dovecot 2.3 prefixes these with the login process ("imap-login: Login:"),
  # 2.4 does not, so neither pattern insists on it.
  /(^|[[:space:]])Login: /  { nsucc++; next }
  /auth failed/ {
      failusers[user] = 1
      if (!(user in valid)) invalidusers[user] = 1
  }
  END {
      nf = 0; ni = 0
      for (u in failusers)    nf++
      for (u in invalidusers) ni++
      if (nsucc + 0 >= min_succ && nf <= max_fu && ni <= max_iu) {
          printf "exempt %d %d %d\n", nsucc + 0, nf, ni
          exit 0
      }
      printf "ban %d %d %d\n", nsucc + 0, nf, ni
      exit 1
  }' | {
      # gawk is in a pipeline, so its exit status is out of reach: read the
      # verdict instead. No verdict at all (no gawk, no log) means ban.
      read -r verdict nsucc nf ni
      if [ "$verdict" = "exempt" ]; then
          logger -t f2b-mail-ignore \
            "EXEMPT $IP (successes=$nsucc failing_accounts=$nf invalid_accounts=$ni) - stale client"
          exit 0
      fi
      exit 1
  }
