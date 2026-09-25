"""The directories a site needs inside its home directory, and the permissions on them."""

import os
from pathlib import Path

from serverctl import system

from ..config import Config
from . import acl
from .serving import challenge_dir

# Shown until the site's own files are uploaded, so a new domain answers with something rather than an error.
# The marker says it is ours, so it can be taken away again once the site's own files are there: index.html comes
# before index.php, so leaving it would keep a WordPress site from ever showing.
MARKER = "<!-- put here by domainctl; it goes when the site's own files arrive -->"
# The sentence every placeholder has held, marker or not: the ones written before the marker existed are ours too.
PLACEHOLDER_LINE = "This site is set up and waiting for its files."
PLACEHOLDER = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{{domain}}</title></head>
<body><h1>{{domain}}</h1><p>This site is set up and waiting for its files.</p></body>
</html>
{MARKER}
"""
# What a site's own index is called. index.html is not among them: that is the placeholder's name.
OWN_INDEX = ("index.php", "index.htm", "index.xhtml", "default.php")


def create(config: Config, user: str, domain: str) -> None:
    """Creates the web root, its challenge folder and the log directory, with a placeholder page.

    The challenge folder is made straight away because the template serves it from a context of its own, and
    OpenLiteSpeed complains about a context whose directory isn't there. It inherits the web root's permissions,
    so it needs no entries of its own.
    """
    for path in (config.docroot_of(user), challenge_dir(config.docroot_of(user)), config.logs_of(user)):
        path.mkdir(parents=True, exist_ok=True)
        _own(path, user)
    if remove_placeholder(config, user):
        return
    index = config.docroot_of(user) / "index.html"
    if not index.exists():
        index.write_text(PLACEHOLDER.format(domain=domain))
        _own(index, user)


def remove_placeholder(config: Config, user: str) -> bool:
    """Takes the placeholder away once the site has an index of its own, and says whether the site has one.

    OpenLiteSpeed serves index.html before index.php, so a placeholder left in place would answer for a
    WordPress site that has just been uploaded.
    """
    docroot = config.docroot_of(user)
    if not any((docroot / name).exists() for name in OWN_INDEX):
        return False
    index = docroot / "index.html"
    try:
        written_here = index.is_file() and any(mark in index.read_text(errors="replace")
                                               for mark in (MARKER, PLACEHOLDER_LINE))
        if written_here:
            index.unlink()
    except OSError:
        pass  # the site's own index answers either way; permissions are another check's business
    return True


def permissions(config: Config, user: str) -> dict[Path, dict[str, str]]:
    """The ACL entries each of the site's paths should have, in the order they are applied.

    The two directories on the way in only get the right to be walked through; the web root is readable and the
    log directory writable.
    """
    home = config.home(user)
    return {
        home: acl.traversable_by(config.web_user),
        **{parent: acl.traversable_by(config.web_user) for parent in _between(home, config.docroot_of(user))},
        config.docroot_of(user): acl.readable_by(config.web_user),
        config.logs_of(user): acl.writable_by(config.web_user),
    }


def apply_permissions(config: Config, user: str) -> bool:
    """Sets the ACL entries that aren't right yet. Returns whether anything changed."""
    return any([acl.apply(path, entries) for path, entries in permissions(config, user).items()])


def missing_permissions(config: Config, user: str) -> dict[Path, dict[str, str]]:
    """The entries that aren't right, per path, for the check command."""
    found = {path: acl.missing(path, entries) for path, entries in permissions(config, user).items()}
    return {path: entries for path, entries in found.items() if entries}


def _between(home: Path, docroot: Path) -> list[Path]:
    """The directories under the home directory on the way to the web root, nearest first: public_html for the
    usual public_html/www. The web root itself gets its own entries, so it isn't one of them."""
    return list(reversed(docroot.parents))[len(home.parts):]


def _own(path: Path, user: str) -> None:
    passwd = system.find_user(user)
    os.chown(path, passwd.pw_uid, passwd.pw_gid)
