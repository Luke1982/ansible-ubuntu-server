"""The access control lists that let OpenLiteSpeed's user into a site's files.

A site's home directory is the user's own and nothing else may read it, so the web server needs an entry of its
own. Only the two directories on the way in get one, and it is the least that works: "--x" is enough to walk
through a directory without being able to list it.

The web root gets "r-x" and the same as a default entry, so files put there later are readable too. The log
directory gets "rwx" and a default of "rw-", since OpenLiteSpeed writes the site's logs itself.

Entries are read back from getfacl and compared, so running a command twice changes nothing the second time.
"""

import re
from pathlib import Path

from serverctl import system
from serverctl.errors import CtlError

# A getfacl line: an optional "default:", the kind, the name it applies to, the permissions, and what the mask
# leaves of them ("r-x\t#effective:r--"), which is not part of the entry itself.
_LINE = re.compile(r"^(?P<key>(?:default:)?(?:user|group|other|mask):[^:]*):(?P<perms>[rwx-]{3})(?:\s*#.*)?$")


def read(path: Path) -> dict[str, str]:
    """The path's entries, keyed the way the entry helpers below write them. A path that isn't there has none."""
    if not Path(path).exists():
        return {}
    found = {}
    for line in system.run("getfacl", "--absolute-names", "--omit-header", str(path)).splitlines():
        match = _LINE.match(line.strip())
        if match:
            found[match.group("key")] = match.group("perms")
    return found


def missing(path: Path, entries: dict[str, str]) -> dict[str, str]:
    """The wanted entries the path doesn't have, or has with other permissions."""
    current = read(path)
    return {key: perms for key, perms in entries.items() if current.get(key) != perms}


def apply(path: Path, entries: dict[str, str]) -> bool:
    """Gives the path the entries, leaving its other entries alone. Returns whether anything changed."""
    if not Path(path).exists():
        raise CtlError(f"There is no {path} to set permissions on.",
                       hint="Add the site again to make its directories: domainctl add DOMAIN")
    difference = missing(path, entries)
    if not difference:
        return False
    system.run("setfacl", "--modify", ",".join(f"{key}:{perms}" for key, perms in sorted(difference.items())),
               str(path))
    return True


def traversable_by(web_user: str) -> dict[str, str]:
    """Walk through, but not list: for the home directory and the one above the web root."""
    return {f"user:{web_user}": "--x"}


def readable_by(web_user: str) -> dict[str, str]:
    """The web root: readable now and for whatever is put there later.

    The owner keeps full control and the mask is set with the rest, so adding a named entry doesn't quietly take
    the group's permissions away.
    """
    return {
        "user:": "rwx", f"user:{web_user}": "r-x", "group:": "rwx", "mask:": "rwx", "other:": "r-x",
        "default:user:": "rwx", f"default:user:{web_user}": "r-x", "default:group:": "rwx",
        "default:mask:": "rwx", "default:other:": "r-x",
    }


def writable_by(web_user: str) -> dict[str, str]:
    """The log directory: OpenLiteSpeed creates and writes the log files, so it needs to write in the directory.

    Files inherit "rw-" rather than "rwx": a log file is never run.
    """
    return {
        "user:": "rwx", f"user:{web_user}": "rwx", "group:": "rwx", "mask:": "rwx", "other:": "---",
        "default:user:": "rwx", f"default:user:{web_user}": "rw-", "default:group:": "rwx",
        "default:mask:": "rwx", "default:other:": "---",
    }
