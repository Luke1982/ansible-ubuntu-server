#!/bin/bash
# Tests for roles/mail/files/f2b-mail-ignore.sh, the fail2ban ignorecommand
# that separates a password guesser from a customer whose mail client still
# has an old password saved.
#
# Run from anywhere:   bash roles/mail/tests/f2b-mail-ignore/run-tests.sh
#
# The script under test is never installed here: each case builds a settings
# file in a temporary directory, points it at fixture logs and an account
# list, and checks the exit status. Exit 0 means "don't ban", non-zero "ban".

set -u

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files" && pwd)/f2b-mail-ignore.sh"

passed=0
failed=0
WORK=""

cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT

# --- fixture builders -------------------------------------------------------

# Dovecot 2.3 logs a successful login through the login process.
success_line() {  # ip user
    printf 'Sep 21 09:12:33 mail dovecot: imap-login: Login: user=<%s>, method=PLAIN, rip=%s, lip=10.0.0.1, mpid=1234, TLS, session=<abc>\n' "$2" "$1"
}

failure_line() {  # ip user
    printf 'Sep 21 09:12:33 mail dovecot: imap-login: Disconnected (auth failed, 1 attempts in 4 secs): user=<%s>, method=PLAIN, rip=%s, lip=10.0.0.1, TLS, session=<abc>\n' "$2" "$1"
}

# Dovecot 2.4 reshaped its log events: the login process no longer prefixes
# every line, and failures come through the auth connection. Both still carry
# user=<> and rip=.
success_line_24() {  # ip user
    printf 'Sep 21 09:12:33 mail dovecot: Login: user=<%s>, method=PLAIN, rip=%s, lip=10.0.0.1, mpid=1234, TLS\n' "$2" "$1"
}

failure_line_24() {  # ip user
    printf 'Sep 21 09:12:33 mail dovecot: conn unix:auth (pid=1,uid=0): auth<7>: Login aborted: Internal login failure (auth failed, 2 attempts in 5 secs): user=<%s>, method=PLAIN, rip=%s, lip=10.0.0.1\n' "$2" "$1"
}

# Starts a case: fresh temporary directory, empty log, and the accounts that
# exist in the mail database.
new_case() {  # valid account...
    cleanup
    WORK="$(mktemp -d)"
    : > "$WORK/mail.log"
    : > "$WORK/valid"
    local account
    for account in "$@"; do printf '%s\n' "$account" >> "$WORK/valid"; done
    settings
}

# Writes the settings file the script reads. Extra lines can be appended by a
# case that wants different thresholds.
settings() {
    cat > "$WORK/settings" <<SETTINGS
LOGS="$WORK/mail.log"
VALID_CACHE="$WORK/valid"
MYCNF="$WORK/unused.cf"
SETTINGS
}

log() { "$@" >> "$WORK/mail.log"; }

# --- assertions -------------------------------------------------------------

check() {  # name expected_status ip
    local name="$1" expected="$2" ip="$3" status
    F2B_MAIL_SETTINGS="$WORK/settings" "$SCRIPT" "$ip" >/dev/null 2>&1
    status=$?
    local expected_word="ban" got_word="ban"
    [ "$expected" -eq 0 ] && expected_word="exempt"
    [ "$status" -eq 0 ] && got_word="exempt"
    if [ "$status" -eq "$expected" ]; then
        printf '  ok    %s\n' "$name"
        passed=$((passed + 1))
    else
        printf '  FAIL  %s: expected %s, got %s (exit %d)\n' \
            "$name" "$expected_word" "$got_word" "$status"
        failed=$((failed + 1))
    fi
}

exempts() { check "$1" 0 "$2"; }
bans()    { check "$1" 1 "$2"; }

# --- the two populations the jail actually sees -----------------------------

# A customer's phone: it logs in all day and separately retries one saved
# password that is out of date.
new_case info@majorlabel.nl
log success_line 82.172.143.246 info@majorlabel.nl
log success_line 82.172.143.246 info@majorlabel.nl
log failure_line 82.172.143.246 info@majorlabel.nl
log failure_line 82.172.143.246 info@majorlabel.nl
log failure_line 82.172.143.246 info@majorlabel.nl
exempts "established client with a stale password" 82.172.143.246

# A guesser knows no password, so it never logs in.
new_case info@majorlabel.nl sales@majorlabel.nl
log failure_line 102.97.192.232 info@majorlabel.nl
log failure_line 102.97.192.232 info@majorlabel.nl
log failure_line 102.97.192.232 sales@majorlabel.nl
log failure_line 102.97.192.232 sales@majorlabel.nl
bans "guesser with no successful login" 102.97.192.232

# One success could be a guess that happened to land, or a fluke. Two is the
# threshold for calling an address established.
new_case info@majorlabel.nl
log success_line 203.0.113.5 info@majorlabel.nl
log failure_line 203.0.113.5 info@majorlabel.nl
bans "single success is below the threshold" 203.0.113.5

# The case that a stricter rule gets wrong. This phone has three mailboxes
# and only the third has an old password, so that account NEVER succeeds from
# this address -- it only ever fails. Demanding a matching success per account
# would ban the customer.
new_case a@majorlabel.nl b@majorlabel.nl c@majorlabel.nl
log success_line 84.241.200.10 a@majorlabel.nl
log success_line 84.241.200.10 b@majorlabel.nl
log failure_line 84.241.200.10 c@majorlabel.nl
log failure_line 84.241.200.10 c@majorlabel.nl
log failure_line 84.241.200.10 c@majorlabel.nl
exempts "one of three mailboxes holds the old password" 84.241.200.10

# --- an address that cracked one mailbox can't hide behind its successes ----

new_case a@majorlabel.nl b@majorlabel.nl c@majorlabel.nl d@majorlabel.nl e@majorlabel.nl
log success_line 198.51.100.7 a@majorlabel.nl
log success_line 198.51.100.7 a@majorlabel.nl
log failure_line 198.51.100.7 b@majorlabel.nl
log failure_line 198.51.100.7 c@majorlabel.nl
log failure_line 198.51.100.7 d@majorlabel.nl
log failure_line 198.51.100.7 e@majorlabel.nl
bans "spraying four accounts despite its own successes" 198.51.100.7

# A client still configured for a mailbox that was deleted. It happens, so one
# is allowed.
new_case info@majorlabel.nl
log success_line 84.241.200.11 info@majorlabel.nl
log success_line 84.241.200.11 info@majorlabel.nl
log failure_line 84.241.200.11 info@majorlabel.nl
log failure_line 84.241.200.11 removed@majorlabel.nl
exempts "one deleted mailbox still configured" 84.241.200.11

# Probing for accounts that were never there is what an attacker does.
new_case info@majorlabel.nl
log success_line 198.51.100.8 info@majorlabel.nl
log success_line 198.51.100.8 info@majorlabel.nl
log failure_line 198.51.100.8 sysadmin@majorlabel.nl
log failure_line 198.51.100.8 monitoring@majorlabel.nl
bans "probing two accounts that never existed" 198.51.100.8

# --- safety -----------------------------------------------------------------

# Without the account list, condition 3 cannot be judged, so the answer is ban.
new_case
log success_line 84.241.200.12 info@majorlabel.nl
log success_line 84.241.200.12 info@majorlabel.nl
log failure_line 84.241.200.12 info@majorlabel.nl
bans "no account list means ban" 84.241.200.12

# Successes belong to whoever made them.
new_case info@majorlabel.nl
log success_line 84.241.200.13 info@majorlabel.nl
log success_line 84.241.200.13 info@majorlabel.nl
log failure_line 102.97.192.240 info@majorlabel.nl
log failure_line 102.97.192.240 info@majorlabel.nl
bans "another address's successes don't count" 102.97.192.240

# 10.2.3.40 must not be read as 10.2.3.4.
new_case info@majorlabel.nl
log success_line 10.2.3.40 info@majorlabel.nl
log success_line 10.2.3.40 info@majorlabel.nl
log failure_line 10.2.3.4 info@majorlabel.nl
bans "an address is not a prefix of another" 10.2.3.4

# --- log sources ------------------------------------------------------------

new_case info@majorlabel.nl
log success_line_24 84.241.200.14 info@majorlabel.nl
log success_line_24 84.241.200.14 info@majorlabel.nl
log failure_line_24 84.241.200.14 info@majorlabel.nl
exempts "Dovecot 2.4 log lines are understood" 84.241.200.14

# These servers have no rsyslog: the mail log is the systemd journal.
new_case info@majorlabel.nl
mkdir -p "$WORK/bin"
cat > "$WORK/bin/journalctl" <<'FAKE'
#!/bin/bash
cat "$JOURNAL_FIXTURE"
FAKE
chmod 0755 "$WORK/bin/journalctl"
{
    success_line 84.241.200.15 info@majorlabel.nl
    success_line 84.241.200.15 info@majorlabel.nl
    failure_line 84.241.200.15 info@majorlabel.nl
} > "$WORK/journal"
cat > "$WORK/settings" <<SETTINGS
LOGS="$WORK/does-not-exist.log"
VALID_CACHE="$WORK/valid"
MYCNF="$WORK/unused.cf"
SETTINGS
PATH="$WORK/bin:$PATH" JOURNAL_FIXTURE="$WORK/journal" \
    exempts "falls back to the journal when there is no mail.log" 84.241.200.15

# --- settings ---------------------------------------------------------------

new_case info@majorlabel.nl
echo 'MIN_SUCCESS=3' >> "$WORK/settings"
log success_line 84.241.200.16 info@majorlabel.nl
log success_line 84.241.200.16 info@majorlabel.nl
log failure_line 84.241.200.16 info@majorlabel.nl
bans "thresholds come from the settings file" 84.241.200.16

# --- result -----------------------------------------------------------------

printf '\n%d passed, %d failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]
