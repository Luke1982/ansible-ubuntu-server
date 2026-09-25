"""Stored mail: Maildirs on disk, and what Dovecot reports about them."""

import errno
import os
import shutil
import stat
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

from . import names, sieve, system
from .config import Config
from .errors import MailctlError

# Folder names that mail clients look for, linked to the folder they mean.
FOLDER_ALIASES = {
    "Sent": ("Verzonden items", "Verzonden Items", "Sent Messages", "Sent Items"),
    "Trash": ("Verwijderde items", "Deleted Messages"),
    "Junk": ("Ongewenste e-mail",),
}
_STATUS_FIELDS = ("messages", "vsize")


@dataclass(frozen=True)
class Folder:
    name: str
    messages: int
    size: int
    alias_of: str | None = None


@dataclass(frozen=True)
class SieveScript:
    name: str
    active: bool
    content: str


def domain_dir(config: Config, domain: str) -> Path:
    return config.vmail_root / domain


def home_dir(config: Config, address: str) -> Path:
    """Where the account's mail is. Dovecot builds the path from the login in lowercase, so it's the same for
    accounts that were written with capitals before mailctl."""
    local_part, domain = names.split(address)
    return domain_dir(config, domain) / local_part


def create_maildir(config: Config, address: str) -> None:
    """Creates the Maildir with the standard folders and their aliases, as the vmail user, so they're vmail's.
    Existing mail and folders stay."""
    local_part, domain = names.split(address)
    with system.as_user(config.vmail_user), ExitStack() as opened:
        folder = _MailFolder.root(config, opened)
        for name in (domain, local_part, "Maildir"):
            folder = folder.enter(name, create=True)
        _complete(folder)  # INBOX
        for standard, aliases in FOLDER_ALIASES.items():
            _complete(folder.enter(f".{standard}", create=True))
            for alias in aliases:
                folder.link(f".{alias}", f".{standard}")


def _complete(folder: "_MailFolder") -> None:
    """Gives a Maildir folder its cur, new and tmp directories, so it's a complete folder from the start instead of
    one Dovecot creates when it's first opened."""
    for name in ("cur", "new", "tmp"):
        folder.enter(name, create=True)


def delete_mail(config: Config, directory: Path) -> None:
    """Deletes a folder of stored mail under the vmail root, like /var/vmail/example.nl/info, as the vmail user."""
    try:
        *parents, target = directory.relative_to(config.vmail_root).parts
    except ValueError:
        raise MailctlError(f"{directory} isn't in {config.vmail_root}.") from None
    with system.as_user(config.vmail_user), ExitStack() as opened:
        folder = _MailFolder.root(config, opened)
        try:
            for name in parents:
                folder = folder.enter(name)
            folder.remove(target)
        except FileNotFoundError:
            pass  # There is no mail to delete.


def folders(config: Config, address: str) -> list[Folder]:
    output = system.run("doveadm", "-f", "tab", "mailbox", "status", "-u", address, " ".join(_STATUS_FIELDS), "*")
    return parse_folders(output, home_dir(config, address) / "Maildir")


def parse_folders(output: str, maildir: Path) -> list[Folder]:
    """Reads doveadm's tab-separated folder status. Folders that are symlinks name the folder they point to."""
    result = []
    for line in filter(None, output.splitlines()):
        name, messages, size = line.split("\t")
        if (name, messages, size) == ("mailbox", *_STATUS_FIELDS):
            continue
        path = maildir if name == "INBOX" else maildir / f".{name.replace('/', '.')}"
        alias_of = os.readlink(path).removeprefix(".") if path.is_symlink() else None
        result.append(Folder(name, int(messages), int(size), alias_of))
    return result


def sieve_scripts(address: str) -> list[SieveScript]:
    return [
        SieveScript(name, active, sieve.get_script(address, name))
        for name, active in parse_sieve_list(system.run("doveadm", "sieve", "list", "-u", address))
    ]


def put_sieve(address: str, name: str, content: str) -> None:
    """Writes a filter for the account, replacing one of the same name."""
    system.run("doveadm", "sieve", "put", "-u", address, name, stdin=content)


def activate_sieve(address: str, name: str) -> None:
    system.run("doveadm", "sieve", "activate", "-u", address, name)


def parse_sieve_list(output: str) -> list[tuple[str, bool]]:
    """Reads 'doveadm sieve list', which marks the active script with ' ACTIVE'."""
    scripts = []
    for line in output.splitlines():
        name = line.rstrip()
        if name:
            scripts.append((name.removesuffix(" ACTIVE"), name.endswith(" ACTIVE")))
    return scripts


def server_scripts(config: Config) -> list[SieveScript]:
    """The scripts that run for every account, after its own."""
    return [SieveScript(path.stem, True, path.read_text()) for path in sorted(config.sieve_after.glob("*.sieve"))]


def kick(address: str) -> None:
    """Logs out the account's sessions."""
    try:
        system.run("doveadm", "kick", address)
    except MailctlError:
        pass  # Not being able to log out sessions mustn't stop a deletion; they end when the mail client logs out.


def disk_usage(path: Path) -> int:
    """Bytes in the files under path. Symlinked folders aren't followed, so aliases don't count twice."""
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                pass  # Dovecot moved the message while it was being counted.
    return total


class _MailFolder:
    """A folder under the vmail root, opened one level at a time without following symbolic links.

    Use it as the vmail user (system.as_user): the folders are vmail's to change, and a folder swapped for a link
    halfway then can't lead anywhere vmail couldn't go.
    """

    def __init__(self, fd: int, path: Path, user: str, opened: ExitStack) -> None:
        self.fd = fd
        self.path = path
        self.user = user
        self._opened = opened

    @classmethod
    def root(cls, config: Config, opened: ExitStack) -> "_MailFolder":
        fd = os.open(config.vmail_root, os.O_RDONLY | os.O_DIRECTORY)
        opened.callback(os.close, fd)
        return cls(fd, config.vmail_root, config.vmail_user, opened)

    def enter(self, name: str, create: bool = False) -> "_MailFolder":
        """Opens a folder in this one, creating it first if asked to and needed."""
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise self._failed("create", name, error) from None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise self._refused(name) from None
            raise self._failed("open", name, error) from None
        self._opened.callback(os.close, fd)
        return _MailFolder(fd, self.path / name, self.user, self._opened)

    def link(self, name: str, target: str) -> None:
        """Makes a symbolic link in this folder, unless something by that name is there already."""
        with suppress(FileExistsError):  # A mail client may have made a real folder with this name.
            os.symlink(target, name, dir_fd=self.fd)

    def remove(self, name: str) -> None:
        """Deletes a folder in this one, with everything in it. Messages that Dovecot expunges meanwhile are skipped."""
        if stat.S_ISLNK(os.lstat(name, dir_fd=self.fd).st_mode):
            raise self._refused(name)

        def stop(function: Callable[..., object], path: str, error: OSError) -> None:
            if not isinstance(error, FileNotFoundError):
                raise self._failed("delete", path, error) from None

        shutil.rmtree(name, dir_fd=self.fd, onexc=stop)

    def _refused(self, name: str) -> MailctlError:
        return MailctlError(f"{self.path / name} is a symbolic link or not a folder, so mailctl won't touch mail there.")

    def _failed(self, action: str, name: str, error: OSError) -> MailctlError:
        """The error for a file or folder below this one; name may be a relative path."""
        hint = None
        if error.errno in (errno.EACCES, errno.EPERM):
            hint = f"mailctl changes mail folders as {self.user}, so they must belong to {self.user}."
        return MailctlError(f"Can't {action} {self.path / name}: {error.strerror or error}.", hint=hint)
