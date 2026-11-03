"""Configuration files that other programs read."""

import os
import stat
import tempfile
from pathlib import Path

from .errors import CtlError


def read(path: Path) -> str | None:
    """The file's text, or None when there is no file."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def replace(path: Path, content: str) -> bool:
    """Replaces the file in one step, so a program never reads half of it. Returns whether the content changed.

    The mode is set on the open file, not by name, so nothing swapped in for the temporary file gets it.
    """
    if read(path) == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
        os.fchmod(file.fileno(), 0o644)
    os.replace(temporary, path)
    return True


def restore(path: Path, content: str | None) -> None:
    """Puts back what read() returned: that content, or no file."""
    if content is None:
        path.unlink(missing_ok=True)
    else:
        replace(path, content)


def remove(path: Path) -> bool:
    """Deletes the file. Returns whether there was one."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def replace_keeping_owner(path: Path, content: str, mode: int = 0o644) -> bool:
    """Replaces a file that belongs to another program, in one step, with the owner and mode it has now. Returns
    whether the content changed. A file that isn't there yet is made root's, with the mode given.

    For a file like coolwsd's settings, which its own user reads and this runs as root: nothing here follows a
    symbolic link someone put in its place, and the mode is set on the open file rather than by name.
    """
    status = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise CtlError(f"Can't read {path}: {error.strerror}.") from None
    else:
        with os.fdopen(descriptor) as file:
            if file.read() == content:
                return False
            status = os.fstat(file.fileno())
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(content)
            if status:
                os.fchown(file.fileno(), status.st_uid, status.st_gid)
            os.fchmod(file.fileno(), stat.S_IMODE(status.st_mode) if status else mode)
        os.replace(temporary, path)  # replaces a symbolic link at path instead of following it
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True
