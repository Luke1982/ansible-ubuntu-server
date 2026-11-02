"""Checks, over a real IMAPS login, that mail a program puts in a folder of its own naming ends up in the
standard folder of that kind: what roles/mail/tasks/configure-imapsieve.yml sets up.

It sends mail into the account it is given, and makes the folders it needs, so give it an account of its own:

    mailctl address add imapsieve-test@example.nl
    python3 -I tests/check-imapsieve.py imapsieve-test@example.nl PASSWORDFILE mail.example.nl
    mailctl address delete imapsieve-test@example.nl --yes --delete-mail

It says PASS or FAIL per check and ends with the count; it stops with 1 if any of them failed.
"""
import imaplib
import re
import ssl
import sys
import time
import uuid

if len(sys.argv) != 4:
    sys.exit(__doc__.strip())
USER, PWFILE, HOST = sys.argv[1], sys.argv[2], sys.argv[3]
results = []
RUN = uuid.uuid4().hex[:6]


def connect():
    m = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context())
    m.login(USER, open(PWFILE).read().strip())
    return m


def q(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def msg(tag, extra=""):
    mid = f"<{tag}-{uuid.uuid4().hex[:8]}@imapsieve-test>"
    body = (
        f"From: Test <{USER}>\r\nTo: someone@example.org\r\nSubject: {tag}\r\n"
        f"Message-ID: {mid}\r\nDate: Mon, 02 Nov 2026 12:00:00 +0000\r\n{extra}\r\nTest body {tag}\r\n"
    )
    return mid, body.encode()


def find(m, folder, mid):
    """Number of messages with this Message-ID in folder, and their flags."""
    typ, _ = m.select(q(folder), readonly=True)
    if typ != "OK":
        return None, []
    typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
    uids = data[0].split()
    flags = []
    for u in uids:
        _, f = m.uid("FETCH", u, "(FLAGS)")
        flags.append(re.search(rb"FLAGS \(([^)]*)\)", f[0]).group(1).decode())
    return len(uids), flags


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def create(m, folder):
    m.create(q(folder))
    m.subscribe(q(folder))


m = connect()
caps = m.capability()[1][0]
check("server advertises SPECIAL-USE (after login)", b"SPECIAL-USE" in caps)
_, lst = m.list('""', "*")
print("folders:", [l.decode() for l in lst])

# 1. APPEND into a client-made sent folder, with \Seen: lands in Sent with \Seen.
for folder in ["Sent Items", "Verzonden items", "Sent Messages"]:
    create(m, folder)
    mid, raw = msg("append-" + folder.replace(" ", "_"))
    typ, resp = m.append(q(folder), r"(\Seen)", None, raw)
    n_src, _ = find(m, folder, mid)
    n_dst, fl = find(m, "Sent", mid)
    check(f"APPEND to '{folder}' -> Sent (once, \\Seen kept), source empty",
          typ == "OK" and n_src == 0 and n_dst == 1 and "\\Seen" in fl[0], f"{typ} src={n_src} Sent={n_dst} flags={fl}")

# 2. APPEND with \Flagged and a keyword keeps them.
mid, raw = msg("append-flags")
m.append(q("Sent Items"), r"(\Seen \Flagged $Label1)", None, raw)
n_dst, fl = find(m, "Sent", mid)
check("APPEND keeps \\Flagged and keywords", n_dst == 1 and "\\Flagged" in fl[0] and "$Label1" in fl[0], str(fl))

# 3. COPY INBOX -> "Verwijderde items": copy lands in Trash, original stays in INBOX.
mid, raw = msg("copy-to-trash")
m.append("INBOX", None, None, raw)
m.select("INBOX")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
create(m, "Verwijderde items")
m.select("INBOX")
typ, _ = m.uid("COPY", d[0], q("Verwijderde items"))
check("COPY INBOX -> 'Verwijderde items' -> Trash, INBOX keeps original",
      typ == "OK" and find(m, "Trash", mid)[0] == 1 and find(m, "Verwijderde items", mid)[0] == 0
      and find(m, "INBOX", mid)[0] == 1)

# 4. MOVE INBOX -> "Deleted Items" (how Outlook deletes): only in Trash afterwards.
mid, raw = msg("move-to-trash")
m.append("INBOX", None, None, raw)
create(m, "Deleted Items")
m.select("INBOX")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
typ, _ = m.uid("MOVE", d[0], q("Deleted Items"))
check("MOVE INBOX -> 'Deleted Items' -> Trash only",
      typ == "OK" and find(m, "Trash", mid)[0] == 1 and find(m, "Deleted Items", mid)[0] == 0
      and find(m, "INBOX", mid)[0] == 0)

# 5. MOVE INBOX -> "Spam" -> Junk.
mid, raw = msg("move-to-spam")
m.append("INBOX", None, None, raw)
create(m, "Spam")
m.select("INBOX")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
m.uid("MOVE", d[0], q("Spam"))
check("MOVE INBOX -> 'Spam' -> Junk only",
      find(m, "Junk", mid)[0] == 1 and find(m, "Spam", mid)[0] == 0 and find(m, "INBOX", mid)[0] == 0)

# 6. MOVE Sent -> "Sent Items": must not be lost, nor duplicated.
mid, raw = msg("move-sent-to-alias")
m.append("Sent", None, None, raw)
m.select("Sent")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
m.uid("MOVE", d[0], q("Sent Items"))
total = (find(m, "Sent", mid)[0] or 0) + (find(m, "Sent Items", mid)[0] or 0)
check("MOVE Sent -> 'Sent Items' keeps exactly 1 copy (not lost)", total == 1,
      f"Sent={find(m, 'Sent', mid)[0]} Sent Items={find(m, 'Sent Items', mid)[0]}")

# 7. COPY Sent -> "Sent Items": no duplicate in Sent.
mid, raw = msg("copy-sent-to-alias")
m.append("Sent", None, None, raw)
m.select("Sent")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
m.uid("COPY", d[0], q("Sent Items"))
check("COPY Sent -> 'Sent Items' doesn't duplicate in Sent", find(m, "Sent", mid)[0] == 1,
      f"Sent={find(m, 'Sent', mid)[0]} Sent Items={find(m, 'Sent Items', mid)[0]}")

# 8. Normal folders untouched: APPEND to Sent, Trash, Drafts, INBOX stays put, once.
for folder in ["Sent", "Trash", "Drafts", "INBOX"]:
    mid, raw = msg("plain-" + folder)
    m.append(q(folder), None, None, raw)
    check(f"APPEND to '{folder}' stays there once", find(m, folder, mid)[0] == 1)

# 9. Spam-flagged APPEND to INBOX isn't moved by the delivery script.
mid, raw = msg("spamflag-inbox", "X-Spam-Flag: YES\r\n")
m.append("INBOX", None, None, raw)
check("X-Spam-Flag APPEND to INBOX stays in INBOX (delivery sieve not run on IMAP)",
      find(m, "INBOX", mid)[0] == 1 and find(m, "Junk", mid)[0] == 0)

# 10. Drafts by another name is left alone (client must find its draft back).
create(m, "Concepten")
mid, raw = msg("draft-concepten")
m.append(q("Concepten"), r"(\Draft)", None, raw)
check("APPEND to 'Concepten' stays there", find(m, "Concepten", mid)[0] == 1)

# 11. Moving out of Trash back into INBOX (undelete) works and isn't affected.
mid, raw = msg("undelete")
m.append("Trash", None, None, raw)
m.select("Trash")
_, d = m.uid("SEARCH", None, "HEADER", "Message-ID", mid)
m.uid("MOVE", d[0], "INBOX")
check("MOVE Trash -> INBOX works", find(m, "INBOX", mid)[0] == 1 and find(m, "Trash", mid)[0] == 0)

# 12. Bulk: 50 APPENDs into "Sent Items" -> 50 in Sent, timing.
mids = []
t = time.time()
for i in range(50):
    mid, raw = msg(f"bulk{RUN}x{i}")
    mids.append(mid)
    m.append(q("Sent Items"), r"(\Seen)", None, raw)
dt = time.time() - t
m.select("Sent", readonly=True)
_, d = m.uid("SEARCH", None, "SUBJECT", f"bulk{RUN}")
m.select(q("Sent Items"), readonly=True)
_, d2 = m.uid("SEARCH", None, "SUBJECT", f"bulk{RUN}")
check(f"50 APPENDs to 'Sent Items' -> 50 in Sent, 'Sent Items' empty ({dt:.2f}s)",
      len(d[0].split()) == 50 and len(d2[0].split()) == 0, f"Sent={len(d[0].split())} SentItems={len(d2[0].split())}")

# 13. Second session sees the same thing (no stale view / re-upload loop).
m2 = connect()
m2.select(q("Sent Items"), readonly=True)
_, d3 = m2.uid("SEARCH", None, "ALL")
m2.select("Sent", readonly=True)
_, d4 = m2.uid("SEARCH", None, "ALL")
print("Sent Items:", len(d3[0].split()), "Sent:", len(d4[0].split()))
m2.logout()
m.logout()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
