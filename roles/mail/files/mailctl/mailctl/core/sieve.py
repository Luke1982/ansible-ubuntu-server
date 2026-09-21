"""Sieve filters in a tar archive, as another server's mail folder holds them: a sieve directory with the scripts,
and .dovecot.sieve naming the active one. The archive is read, never unpacked, so nothing it holds can land anywhere.
"""

import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .errors import MailctlError

ACTIVE_LINK = ".dovecot.sieve"  # points at the active script in the sieve directory
MAX_SCRIPT = 1 << 20  # bytes; a filter is text, and a bigger file isn't one
MAX_SCRIPTS = 1000
# Sieve script names, as ManageSieve allows them, kept to what a file name can hold.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")


@dataclass(frozen=True)
class Script:
    name: str
    content: str
    active: bool


def read_archive(path: Path) -> tuple[list[Script], list[str]]:
    """The filters in the archive, and a line about each file left out."""
    scripts: dict[str, str] = {}
    skipped: list[str] = []
    active_name, active_content = None, None
    with _open(path) as archive:
        for member in archive:
            name = Path(member.name).name
            if name == ACTIVE_LINK:
                if member.issym() or member.islnk():
                    active_name = Path(member.linkname).stem
                elif member.isfile():
                    active_content = _content(archive, member, skipped)
                continue
            if not member.isfile() or not name.endswith(".sieve"):
                continue
            if len(scripts) >= MAX_SCRIPTS:
                raise MailctlError(f"{path} holds more than {MAX_SCRIPTS} filters.")
            if not _NAME.fullmatch(name.removesuffix(".sieve")):
                skipped.append(f"{member.name}: not a filter name")
                continue
            content = _content(archive, member, skipped)
            if content is not None:
                scripts[name.removesuffix(".sieve")] = content
    if active_name not in scripts:
        active_name = _matching(scripts, active_content) or (list(scripts)[0] if len(scripts) == 1 else None)
    return [Script(name, content, name == active_name) for name, content in scripts.items()], skipped


def _open(path: Path) -> tarfile.TarFile:
    try:
        return tarfile.open(path, "r:*")
    except FileNotFoundError:
        raise MailctlError(f"There is no file {path}.") from None
    except (tarfile.TarError, OSError) as error:
        raise MailctlError(f"Can't read {path}: {error}.", hint="It should be a tar archive of a sieve directory.") \
            from None


def _content(archive: tarfile.TarFile, member: tarfile.TarInfo, skipped: list[str]) -> str | None:
    if member.size > MAX_SCRIPT:
        skipped.append(f"{member.name}: bigger than {MAX_SCRIPT // 1024} KB")
        return None
    stream = archive.extractfile(member)
    if stream is None:
        return None
    return stream.read().decode(errors="replace")


def _matching(scripts: dict[str, str], content: str | None) -> str | None:
    """The script the active one is a copy of, for an archive that holds .dovecot.sieve as a file."""
    if content is None:
        return None
    return next((name for name, script in scripts.items() if script == content), None)
