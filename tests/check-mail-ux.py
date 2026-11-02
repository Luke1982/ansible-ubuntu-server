"""Checks, on the server itself, the three things the mail role sets up around a mailbox: SpamAssassin
learning from what people file by hand, the hidden folder deleted mail waits in, and the limit on how much
a mailbox may hold with the warnings before it is reached.

Run it as root on the mail server, since it reads the spam queue and talks to Dovecot and the database:

    mailctl address add ux-test@example.nl
    python3 -I tests/check-mail-ux.py ux-test@example.nl PASSWORDFILE mail.example.nl
    mailctl address delete ux-test@example.nl --yes --delete-mail

It sends mail into that account, fills it up and gives it a 3 MB limit for a moment, so give it an account
of its own. It says PASS or FAIL per check and ends with the count; it stops with 1 if any of them failed.
"""
import imaplib
import os
import re
import ssl
import subprocess
import sys
import time
import uuid

if len(sys.argv) != 4:
    sys.exit(__doc__.strip())
USER, PWFILE, HOST = sys.argv[1], sys.argv[2], sys.argv[3]
SPOOL = "/var/spool/spam-learn"
RUN = uuid.uuid4().hex[:6]
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def sh(*cmd, user=None):
    if user:
        cmd = ("runuser", "-u", user, "--") + cmd
    return subprocess.run(cmd, capture_output=True, text=True)


def connect():
    m = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context())
    m.login(USER, open(PWFILE).read().strip())
    return m


def q(name):
    return '"' + name + '"'


def msg(tag, size=0, extra=""):
    mid = f"<{tag}-{RUN}-{uuid.uuid4().hex[:6]}@uxtest>"
    body = "Test body " + tag + "\r\n" + ("x" * 76 + "\r\n") * (size // 78)
    raw = (f"From: Test <{USER}>\r\nTo: {USER}\r\nSubject: {tag} {RUN}\r\nMessage-ID: {mid}\r\n"
           f"Date: Mon, 02 Nov 2026 12:00:00 +0000\r\n{extra}\r\n{body}")
    return mid, raw.encode()


def count(m, folder, mid):
    if m.select(q(folder), readonly=True)[0] != "OK":
        return None
    return len(m.uid("SEARCH", None, "HEADER", "Message-ID", mid)[1][0].split())


def uid_of(m, folder, mid):
    m.select(q(folder))
    return m.uid("SEARCH", None, "HEADER", "Message-ID", mid)[1][0]


def spool(kind):
    d = f"{SPOOL}/{kind}"
    return sorted(f for f in os.listdir(d) if not f.startswith(".tmp"))


def spool_has(kind, mid):
    for f in spool(kind):
        if mid.encode() in open(f"{SPOOL}/{kind}/{f}", "rb").read():
            return True
    return False


def doveadm(*args):
    return sh("doveadm", *args)


def expunged_count(mid):
    r = doveadm("search", "-u", USER, "mailbox", "EXPUNGED", "HEADER", "Message-ID", mid)
    return len(r.stdout.split("\n")) - 1 if r.returncode == 0 else None


# A run that stopped halfway may have left the small test quota behind.
sh("mysql", "mailserver", "-e", f"UPDATE virtual_users SET quota=NULL WHERE email='{USER}'")
sh("doveadm", "auth", "cache", "flush")
m = connect()

# ---------------- spam learning ----------------
for kind in ("spam", "ham"):
    for f in spool(kind):
        pass  # other users' queued mail stays; tests look for their own Message-IDs

mid, raw = msg("learn-move-to-junk")
m.append("INBOX", None, None, raw)
m.uid("MOVE", uid_of(m, "INBOX", mid), "Junk")
check("MOVE INBOX -> Junk queues spam", spool_has("spam", mid) and count(m, "Junk", mid) == 1)

mid_ham = None
mid, raw = msg("learn-move-out-of-junk")
m.append("Junk", None, None, raw)
m.uid("MOVE", uid_of(m, "Junk", mid), "INBOX")
check("MOVE Junk -> INBOX queues ham", spool_has("ham", mid) and count(m, "INBOX", mid) == 1)

mid, raw = msg("learn-junk-to-trash")
m.append("Junk", None, None, raw)
m.uid("MOVE", uid_of(m, "Junk", mid), "Trash")
check("MOVE Junk -> Trash queues nothing", not spool_has("ham", mid) and not spool_has("spam", mid))

m.create(q("Deleted Items"))
mid, raw = msg("learn-junk-to-deleted-items")
m.append("Junk", None, None, raw)
m.uid("MOVE", uid_of(m, "Junk", mid), q("Deleted Items"))
check("MOVE Junk -> 'Deleted Items' queues nothing, lands in Trash",
      not spool_has("ham", mid) and count(m, "Trash", mid) == 1)

m.create(q("Spam"))
mid, raw = msg("learn-move-to-spam-folder")
m.append("INBOX", None, None, raw)
m.uid("MOVE", uid_of(m, "INBOX", mid), q("Spam"))
check("MOVE INBOX -> 'Spam' queues spam and lands in Junk",
      spool_has("spam", mid) and count(m, "Junk", mid) == 1 and count(m, "Spam", mid) == 0)

mid, raw = msg("learn-append-to-junk")
m.append("Junk", None, None, raw)
check("APPEND to Junk (not a COPY) queues nothing", not spool_has("spam", mid))

# bulk move speed
mids = []
for i in range(40):
    mid, raw = msg(f"bulkspam{i}")
    mids.append(mid)
    m.append("INBOX", None, None, raw)
m.select("INBOX")
uids = m.uid("SEARCH", None, "SUBJECT", f"bulkspam")[1][0].split()
t = time.time()
typ, _ = m.uid("MOVE", b",".join(uids), "Junk")
dt = time.time() - t
check(f"MOVE 40 messages to Junk at once is quick ({dt:.2f}s)", typ == "OK" and dt < 5)

before = len(spool("spam")) + len(spool("ham"))
r = sh("/usr/local/sbin/spam-learn-queue", user="vmail")
after = len(spool("spam")) + len(spool("ham"))
check("spam-learn-queue empties the queue via sa-learn", r.returncode == 0 and after == 0,
      f"{before}->{after} {r.stdout.strip()} {r.stderr.strip()[:200]}")

# ---------------- lazy_expunge ----------------
_, lst = m.list('""', "*")
check("EXPUNGED isn't listed to clients", not any(b"EXPUNGED" in l for l in lst), str([l.decode() for l in lst]))

mid, raw = msg("lazy-delete-for-good")
m.append("Trash", None, None, raw)
m.select("Trash")
u = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)[1][0]
m.uid("STORE", u, "+FLAGS", r"(\Deleted)")
m.expunge()
check("Expunged from Trash: gone for the client, kept in EXPUNGED",
      count(m, "Trash", mid) == 0 and expunged_count(mid) == 1)

r = doveadm("move", "-u", USER, "INBOX", "mailbox", "EXPUNGED", "HEADER", "Message-ID", mid)
check("doveadm move restores it from EXPUNGED to INBOX",
      r.returncode == 0 and count(m, "INBOX", mid) == 1 and expunged_count(mid) == 0, r.stderr.strip()[:200])

m.create(q("Archief"))
mid, raw = msg("lazy-move-no-copy")
m.append("INBOX", None, None, raw)
m.uid("MOVE", uid_of(m, "INBOX", mid), q("Archief"))
check("MOVE between folders doesn't leave a copy in EXPUNGED", expunged_count(mid) == 0 and count(m, "Archief", mid) == 1,
      f"EXPUNGED={expunged_count(mid)}")

# ---------------- quota ----------------
typ, data = m.getquotaroot("INBOX")
check("GETQUOTAROOT INBOX shows the 25G default", typ == "OK" and b"26214400" in b" ".join(
    x if isinstance(x, bytes) else b" ".join(x) for x in data[1]), str(data))

# Big message in EXPUNGED must not count.
sh("doveadm", "quota", "recalc", "-u", USER)
before = int(re.search(r"STORAGE\s+(\d+)", doveadm("quota", "get", "-u", USER).stdout).group(1))
mid, raw = msg("lazy-quota", size=600_000)
m.append("Trash", None, None, raw)
mid_size_check = mid
m.select("Trash")
u = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)[1][0]
m.uid("STORE", u, "+FLAGS", r"(\Deleted)")
m.expunge()
after = int(re.search(r"STORAGE\s+(\d+)", doveadm("quota", "get", "-u", USER).stdout).group(1))
check("Mail in EXPUNGED doesn't count toward quota", after <= before + 5 and expunged_count(mid) == 1,
      f"{before}KiB -> {after}KiB")

# Small quota for this account: warnings at 85/95%, and refusal when full.
sh("mysql", "mailserver", "-e", f"UPDATE virtual_users SET quota='3M' WHERE email='{USER}'")
doveadm("auth", "cache", "flush")
m.logout()
m = connect()
sh("doveadm", "quota", "recalc", "-u", USER)
used = int(re.search(r"STORAGE\s+(\d+)", doveadm("quota", "get", "-u", USER).stdout).group(1))
print("usage before filling:", used, "KiB of 3072", flush=True)
# fill to ~90%
need = int((3072 * 0.90 - used) * 1024)
if need > 0:
    mid, raw = msg("fill", size=need)
    m.append("Archief", None, None, raw)
time.sleep(4)
m.select("INBOX", readonly=True)
warn = m.uid("SEARCH", None, "SUBJECT", '"85% vol"')[1][0].split()
check("Passing 85% delivers a warning mail to the account", len(warn) == 1, f"found {len(warn)}")

mid, raw = msg("overfill", size=1_000_000)
typ, data = m.append("Archief", None, None, raw)
check("APPEND past the quota is refused with OVERQUOTA", typ == "NO" and b"OVERQUOTA" in data[0], str(data))

import smtplib
l = smtplib.LMTP("/var/spool/postfix/private/dovecot-lmtp")
mid_d, raw_d = msg("lmtp-overquota", size=1_000_000)
try:
    l.sendmail(USER, [USER], raw_d)
    lmtp = "accepted"
except smtplib.SMTPRecipientsRefused as e:
    lmtp = str(e.recipients)
except smtplib.SMTPDataError as e:
    lmtp = f"{e.smtp_code} {e.smtp_error!r}"
l.quit()
check("Delivery past the quota is refused", lmtp != "accepted", lmtp)

# Deleting frees room again (EXPUNGED doesn't count).
m.select(q("Archief"))
u = m.uid("SEARCH", None, "SUBJECT", "fill")[1][0]
m.uid("STORE", u, "+FLAGS", r"(\Deleted)")
m.expunge()
mid, raw = msg("after-delete", size=500_000)
typ, data = m.append("Archief", None, None, raw)
check("After deleting, APPEND works again", typ == "OK", str(data))

sh("mysql", "mailserver", "-e", f"UPDATE virtual_users SET quota=NULL WHERE email='{USER}'")
doveadm("auth", "cache", "flush")
m.logout()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
