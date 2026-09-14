"""External commands and facts about this server."""

import json
import os
import pwd
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from .errors import MailctlError


def run(*args: str, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, errors="replace", check=False)
    except FileNotFoundError:
        raise MailctlError(f"{args[0]} isn't installed.") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise MailctlError(f"{shlex.join(args)} failed: {detail}")
    return result.stdout


def require_root() -> None:
    if os.geteuid() != 0:
        raise MailctlError("mailctl must run as root.", hint="Try again with sudo.")


def find_user(name: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise MailctlError(f"There's no user {name} on this server.") from None


@contextmanager
def as_user(name: str) -> Iterator[None]:
    """Runs the block as another user, like 'sudo -u': what it creates is that user's, and a link that user swapped
    in can't lead anywhere they couldn't go themselves. Nothing changes when the process already is that user."""
    user = find_user(name)
    if os.geteuid() == user.pw_uid:
        yield
        return
    uid, gid, groups = os.geteuid(), os.getegid(), os.getgroups()
    try:
        os.setgroups(os.getgrouplist(user.pw_name, user.pw_gid))
        os.setegid(user.pw_gid)
        os.seteuid(user.pw_uid)
        yield
    finally:
        os.seteuid(uid)
        os.setegid(gid)
        os.setgroups(groups)


def reload(service: str) -> None:
    run("systemctl", "reload-or-restart", service)


def remove_tree(path: Path) -> None:
    """Deletes a directory with everything in it. A directory that doesn't exist is fine."""
    # Checked here, because shutil's error for it has no message on Python 3.14.
    if path.is_symlink():
        raise MailctlError(f"Can't delete {path}: it's a symbolic link.")
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise MailctlError(f"Can't delete {path}: {error.strerror or error}.") from None


def server_ips() -> set[IPv4Address | IPv6Address]:
    return parse_ip_addresses(run("ip", "-json", "address", "show", "scope", "global"))


def parse_ip_addresses(output: str) -> set[IPv4Address | IPv6Address]:
    return {
        ip_address(info["local"])
        for link in json.loads(output)
        for info in link.get("addr_info", [])
        if "local" in info
    }
