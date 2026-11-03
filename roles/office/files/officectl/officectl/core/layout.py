"""The directories one Nextcloud has, who owns them and who may read them.

Nextcloud's own code is root's: the files it is made of are only read, never written, and a program that may
rewrite its own code can be made to run anything. The three places it does write -- its settings, the apps it
installs itself, and the files people put in it -- belong to the user PHP runs as, and the last of them is
deliberately not under the web root: nothing the web server serves can reach what people store.

OpenLiteSpeed is let into the home directory by an access control list, the same way the web sites do it, since
the home directory itself belongs to the Nextcloud's own Linux user and nothing else may read it.
"""

import os
from pathlib import Path

from serverctl import acl, serving, system

from ..config import Config

HOME_MODE = 0o750  # the Nextcloud's own user; OpenLiteSpeed gets in through an access control list
CODE_MODE = 0o755  # the code, which is read by anyone and written by root alone
WRITABLE_MODE = 0o770  # what Nextcloud writes: its settings and the files people store
APPS_MODE = 0o775  # the apps it installs itself, which it also serves
PRIVATE_MODE = 0o700  # its temporary files, which are nobody else's business
# Inside the web root: where Nextcloud keeps its settings and the apps it installs itself.
CONFIG = "config"
APPS = "apps"


def directories(config: Config, user: str) -> dict[Path, tuple[str, int]]:
    """Every directory a Nextcloud has, with the user that owns it and its mode, outermost first.

    The owner is root where only root writes. The group is the owner's own, except on what the web server
    writes: that gets the group PHP runs as, so a file Nextcloud makes stays readable to it.
    """
    docroot = config.docroot_of(user)
    return {
        config.home(user): (user, HOME_MODE),
        **{parent: ("root", CODE_MODE) for parent in _between(config.home(user), docroot)},
        docroot: ("root", CODE_MODE),
        serving.challenge_dir(docroot): ("root", CODE_MODE),
        docroot / CONFIG: (config.web_user, WRITABLE_MODE),
        docroot / APPS: (config.web_user, APPS_MODE),
        config.data_of(user): (config.web_user, WRITABLE_MODE),
        config.tmp_of(user): (config.web_user, PRIVATE_MODE),
        config.logs_of(user): (user, CODE_MODE),
    }


def create(config: Config, user: str) -> None:
    """Makes the directories, each with its owner and mode. Anything already there is left where it is, with its
    owner and mode put right: a Nextcloud that is being repaired keeps its files."""
    for path, (owner, mode) in directories(config, user).items():
        path.mkdir(parents=True, exist_ok=True)
        own(path, owner, config.web_group if owner == config.web_user else owner)
        path.chmod(mode)


def made_here(config: Config, user: str) -> bool:
    """Whether this Linux user is one officectl made for a Nextcloud: its home holds the directories it makes.
    That is how a run that stopped at the DNS picks up where it left off."""
    return config.docroot_of(user).is_dir() and config.data_of(user).is_dir()


def permissions(config: Config, user: str) -> dict[Path, dict[str, str]]:
    """The access control entries each path should have, in the order they are applied.

    Only what OpenLiteSpeed reaches by itself needs one: the way into the home directory, the code it serves and
    the directory it writes the logs in. What PHP writes belongs to PHP's own user already.
    """
    home = config.home(user)
    return {
        home: acl.traversable_by(config.web_user),
        **{parent: acl.traversable_by(config.web_user) for parent in _between(home, config.docroot_of(user))},
        config.docroot_of(user): acl.readable_by(config.web_user),
        config.logs_of(user): acl.writable_by(config.web_user),
    }


def apply_permissions(config: Config, user: str) -> bool:
    """Sets the entries that aren't right yet. Returns whether anything changed."""
    return any([acl.apply(path, entries) for path, entries in permissions(config, user).items()])


def missing_permissions(config: Config, user: str) -> dict[Path, dict[str, str]]:
    """The entries that aren't right, per path, for the doctor command."""
    found = {path: acl.missing(path, entries) for path, entries in permissions(config, user).items()}
    return {path: entries for path, entries in found.items() if entries}


def own(path: Path, user: str, group: str) -> None:
    passwd = system.find_user(user)
    gid = passwd.pw_gid if group == user else system.find_group(group).gr_gid
    os.chown(path, passwd.pw_uid, gid)


def _between(home: Path, docroot: Path) -> list[Path]:
    """The directories under the home directory on the way to the web root, nearest first: public_html for the
    usual public_html/www. The web root itself is named on its own, so it isn't one of them."""
    return list(reversed(docroot.parents))[len(home.parts):]
