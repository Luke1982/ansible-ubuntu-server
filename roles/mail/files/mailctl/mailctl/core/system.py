"""External commands and facts about this server."""

import json
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from .errors import MailctlError


def run(*args: str, stdin: str | None = None) -> str:
    return _run(args, stdin, text=True, errors="replace")


def run_starter(*args: str) -> str:
    """Like run(), for a command that may start a daemon, like lswsctrl. The daemon keeps the command's output open
    as long as it runs, so that output goes to a file: reading a pipe would never end."""
    with tempfile.TemporaryFile("w+", errors="replace") as output:
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, check=False)
        except FileNotFoundError:
            raise MailctlError(f"{args[0]} isn't installed.") from None
        output.seek(0)
        text = output.read().strip()
    if result.returncode != 0:
        raise MailctlError(f"{shlex.join(args)} failed: {text or f'exit status {result.returncode}'}")
    return text


def run_binary(*args: str, stdin: bytes) -> bytes:
    """Like run(), for programs whose input or output isn't text, like signatures."""
    return _run(args, stdin)


def _run(args: tuple[str, ...], stdin, **text_options):
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, check=False, **text_options)
    except FileNotFoundError:
        raise MailctlError(f"{args[0]} isn't installed.") from None
    if result.returncode != 0:
        stderr, stdout = (_as_text(output).strip() for output in (result.stderr, result.stdout))
        detail = stderr or stdout or f"exit status {result.returncode}"
        raise MailctlError(f"{shlex.join(args)} failed: {detail}")
    return result.stdout


def _as_text(output: str | bytes) -> str:
    return output if isinstance(output, str) else output.decode(errors="replace")


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


# Addresses on the internet, to ask which route the kernel takes; nothing is sent to them.
_ROUTE_PROBES = ("1.1.1.1", "2606:4700:4700::1111")


def server_ips() -> set[IPv4Address | IPv6Address]:
    """The addresses this server sends mail from: the source addresses the kernel picks for its default IPv4 and
    IPv6 routes, as it does for Postfix. Other addresses, like failover addresses or extra IPv6 addresses, aren't."""
    ips: set[IPv4Address | IPv6Address] = set()
    problem = None
    for probe in _ROUTE_PROBES:
        try:
            ips |= parse_route_sources(run("ip", "-json", "route", "get", probe))
        except MailctlError as error:  # usually no route for that kind of address
            problem = error
    if not ips:
        reason = problem.message if problem else "its default routes have no source address."
        raise MailctlError(f"Can't tell which address this server sends mail from: {reason}")
    return ips


def parse_route_sources(output: str) -> set[IPv4Address | IPv6Address]:
    return {ip_address(route["prefsrc"]) for route in json.loads(output) if "prefsrc" in route}
