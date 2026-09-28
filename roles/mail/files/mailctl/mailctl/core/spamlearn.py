"""Teaching SpamAssassin from the mail people file themselves.

Bayes, the part of SpamAssassin that learns, only helps when it is fed. Nobody has the time to feed it by hand, and
they don't have to: every account's Junk folder holds what somebody called spam, and their inbox holds what they
kept. sa-learn reads both and writes what it learns to the shared bayes database -- bayes.cf overrides the username,
so every account teaches the same one.

Only messages that arrived or were moved since the last run are handed over, which keeps a nightly run short on a
server with years of mail. sa-learn also remembers the messages it has already seen, so a message that comes by
twice is not learned twice.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import mailbox, system
from .config import Config
from .errors import MailctlError

JUNK = ".Junk"
# Folders that teach nothing: mail somebody threw away is often spam they never filed, and a draft is half a
# message. The Dutch names are here too, because a server set up before this one has them as folders of their own
# rather than as the aliases Dovecot links to the standard ones.
NOT_HAM = {"junk", "spam", "trash", "drafts", "ongewenste e-mail", "verwijderde items", "prullenbak", "concepten"}
# Files per sa-learn call: a command line can't hold everything, and each call says what it learned.
BATCH = 200
# Where the time of the last run is kept, so the next one only looks at what came after it.
STATE = Path("/var/lib/mailctl/spam-learn.json")
_LEARNED = re.compile(r"Learned tokens from (\d+) message")


@dataclass(frozen=True)
class Folder:
    """A maildir to learn from, and what it teaches."""
    address: str
    path: Path
    spam: bool
    inbox: bool = False

    @property
    def parts(self) -> tuple[str, ...]:
        """The inbox teaches only from mail that was read: what nobody has looked at yet may well be spam that
        hasn't been filed. Everywhere else, unread mail was put there by somebody or by their own filters, so it
        counts as it is."""
        return ("cur",) if self.inbox else ("cur", "new")


@dataclass(frozen=True)
class Learned:
    spam: int = 0
    ham: int = 0
    handed_over: int = 0
    problems: tuple[str, ...] = ()


def folders(config: Config, addresses: list[str]) -> list[Folder]:
    """What every account teaches: its Junk folder is spam, and everything it keeps elsewhere is not.

    Not only the inbox: a message that was marked as spam wrongly is dragged back out of Junk, and people drag it
    wherever it belongs, not back into the inbox. Every folder they file in therefore teaches, which is how the
    filter is told it was wrong.
    """
    found = []
    for address in addresses:
        maildir = mailbox.home_dir(config, address) / "Maildir"
        if not maildir.is_dir():
            continue
        if (maildir / JUNK).is_dir():
            found.append(Folder(address, maildir / JUNK, spam=True))
        found.append(Folder(address, maildir, spam=False, inbox=True))
        found += _own_folders(address, maildir)
    return found


def _own_folders(address: str, maildir: Path) -> list[Folder]:
    """The account's other folders, in the layout Dovecot keeps them: ".INBOX.Invoices" next to the inbox itself.

    An alias, like ".Ongewenste e-mail" beside ".Junk", is a link to one of these and is left out, so nothing is
    read twice under two names.
    """
    found = []
    for entry in sorted(maildir.iterdir()):
        if not entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        if any(part.strip().lower() in NOT_HAM for part in entry.name.lstrip(".").split(".")):
            continue
        found.append(Folder(address, entry, spam=False))
    return found


def messages(folder: Folder, since: float = 0.0) -> list[Path]:
    """The folder's messages that arrived or were moved after that moment. A message moved into Junk keeps the time
    it was written, so the time it was last changed counts too."""
    found = []
    for part in folder.parts:
        directory = folder.path / part
        if not directory.is_dir():
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError as problem:
            raise MailctlError(f"Can't read {directory}: {problem.strerror or problem}.") from None
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            if max(stat.st_mtime, stat.st_ctime) > since:
                found.append(Path(entry.path))
    return found


def learn(paths: list[Path], spam: bool) -> tuple[int, list[str]]:
    """Hands the messages to sa-learn, in batches. Returns how many it learned from, and what went wrong.

    Nothing is synced here: the caller does that once, at the end, which is what --no-sync is for.
    """
    learned, problems = 0, []
    what = "--spam" if spam else "--ham"
    for start in range(0, len(paths), BATCH):
        batch = paths[start:start + BATCH]
        try:
            output = system.run("sa-learn", "--no-sync", what, *(str(path) for path in batch))
        except MailctlError as problem:
            problems.append(problem.message)
            continue
        found = _LEARNED.search(output)
        learned += int(found.group(1)) if found else 0
    return learned, problems


def sync() -> None:
    """Writes what the runs above learned to the bayes database."""
    system.run("sa-learn", "--sync")


def last_run(state: Path = STATE) -> float:
    """When mailctl last learned, as a timestamp. 0 when it never did, so everything counts."""
    try:
        return float(json.loads(state.read_text())["last_run"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def remember(when: float, state: Path = STATE) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"last_run": when, "written": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
