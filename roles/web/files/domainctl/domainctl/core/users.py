"""The Linux user a site runs under.

Each site gets a user of its own, so one site's files can never be read or changed by another. The account has
no password: nobody logs in as it. Root reaches it with "su - USER", and sshd's AllowUsers decides who may
connect over SSH, so a site user cannot.

The home directory is the virtual host's root, which the OpenLiteSpeed template finds as /home/$VH_NAME.
"""

import grp
import pwd
from pathlib import Path

from serverctl import system
from serverctl.errors import CtlError

HOME_MODE = 0o750  # the user's own; OpenLiteSpeed gets in through an ACL entry, nothing else does


def exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


def home_of(name: str) -> Path:
    return Path(system.find_user(name).pw_dir)


def create(name: str, home: Path, shell: str) -> None:
    """Creates the user with a group of its own, a home directory and no password.

    Fails when the name is taken, so an existing user's home directory is never handed to a new site.
    """
    if exists(name):
        raise CtlError(f"There is already a user {name} on this server.",
                       hint="Choose another name with --user, or use the site that is already there.")
    if group_exists(name):
        raise CtlError(f"There is already a group {name} on this server.", hint="Choose another name with --user.")
    if home.exists():
        raise CtlError(f"{home} already exists.",
                       hint="Move it aside, or choose another name with --user, so no old files end up on the site.")
    system.run("useradd", "--create-home", "--home-dir", str(home), "--user-group", "--shell", shell, name)
    # useradd leaves the account without a usable password; locking it says so plainly in /etc/shadow.
    system.run("usermod", "--lock", name)
    home.chmod(HOME_MODE)


def delete(name: str, remove_home: bool) -> None:
    """Removes the user, and its home directory with everything in it when asked."""
    if not exists(name):
        return
    system.run("userdel", *(["--remove"] if remove_home else []), name)
