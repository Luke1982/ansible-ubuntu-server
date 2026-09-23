"""Configuration files that other programs read."""

import os
import tempfile
from pathlib import Path


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
