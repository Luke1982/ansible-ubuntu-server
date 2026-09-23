#!/usr/bin/env python3
"""Puts the phpMyAdmin site in OpenLiteSpeed's server config (httpd_config.conf).

OpenLiteSpeed's listeners and other sites are set up by hand in WebAdmin, so this only adds its own virtual host and
the site's name on every listener for port 80 and 443, in the layout WebAdmin writes, and leaves everything else as
it is. It doesn't use 'include': WebAdmin makes a config with includes read-only.

The note in the virtual host says who manages it, and is the only record of which site is ours: a site with that note
under another name, left by a server that has been renamed, goes with its name on the listeners.

Says "Changed:" when it changed the config, so Ansible can see it.
"""

import argparse
import errno
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# A value spanning lines, like rewrite rules: "rules <<<END_rules", up to a line holding just "END_rules".
_HEREDOC = re.compile(r"<<<\s*(\S+)\s*$")
_VALUE_COLUMN = 26  # where WebAdmin lines up values

NOTE = "Managed by Ansible: phpMyAdmin (db role)"


class ConfigError(Exception):
    """A config this can't read, or one without the listeners the site needs."""


@dataclass(frozen=True)
class Site:
    name: str  # also the name visitors use, like pma.web01.example.nl
    root: str
    config_file: str  # where the site's own settings are, as OpenLiteSpeed spells the path
    note: str


@dataclass(frozen=True)
class Block:
    kind: str  # the first word, in lower case, like "listener" or "virtualhost"
    name: str
    start: int  # the line with the opening brace
    end: int  # the line with the closing brace
    depth: int  # 0 for the top level


@dataclass(frozen=True)
class _Setting:
    key: str
    value: str
    start: int
    end: int  # the last line: for a multi-line value, the line that ends it


def blocks(lines: list[str]) -> list[Block]:
    """Every block, in the order they start."""
    found: list[Block] = []
    opened: list[tuple[str, str, int]] = []
    heredoc = None
    for index, line in enumerate(lines):
        text = line.strip()
        if heredoc:
            heredoc = None if _ends(text, heredoc) else heredoc
            continue
        if not text or text.startswith("#"):
            continue
        multi_line = _HEREDOC.search(text)
        if multi_line:
            heredoc = multi_line.group(1)
        elif text.endswith("{"):
            kind, _, name = text[:-1].strip().partition(" ")
            opened.append((kind.lower(), name.strip(), index))
        elif text.startswith("}"):  # OpenLiteSpeed ignores anything after it
            if not opened:
                raise _unreadable(f"line {index + 1} closes a block that isn't open")
            kind, name, start = opened.pop()
            found.append(Block(kind, name, start, index, len(opened)))
    if opened or heredoc:
        raise _unreadable("a block or a multi-line value isn't closed")
    return sorted(found, key=lambda block: block.start)


def value(lines: list[str], block: Block, key: str) -> str | None:
    """A setting of the block itself, not of a block inside it."""
    return next((setting.value for setting in _settings(lines, block) if setting.key.lower() == key.lower()), None)


def listeners(lines: list[str]) -> tuple[list[str], list[str]]:
    """The names of the listeners for HTTP on port 80 and for HTTPS on port 443."""
    http, https = [], []
    for block in blocks(lines):
        if block.kind != "listener" or block.depth:
            continue
        port = (value(lines, block, "address") or "").rsplit(":", 1)[-1]
        secure = (value(lines, block, "secure") or "0") == "1"
        if port == "80" and not secure:
            http.append(block.name)
        elif port == "443" and secure:
            https.append(block.name)
    return http, https


def configure(lines: list[str], site: Site) -> list[str]:
    """The config with the site as a virtual host of its own, and its name on every listener for port 80 and 443."""
    http, https = listeners(lines)
    if not http:
        raise ConfigError("OpenLiteSpeed has no listener for port 80. Add one in WebAdmin.")
    if not https:
        raise ConfigError("OpenLiteSpeed has no listener for port 443. Add one in WebAdmin.")
    updated = lines
    for name in _ours(updated, site.note):
        if name == site.name:
            continue
        updated = _without_virtual_host(updated, name)
        for listener in http + https:
            updated = _without_map(updated, listener, name)
    updated = _with_virtual_host(updated, site)
    for listener in http + https:
        updated = _with_map(updated, listener, site.name)
    return updated


def apply(path: Path, site: Site) -> bool:
    """Puts the site in the config at path, and says whether that changed anything."""
    previous, status = _read(Path(path))
    wanted = "\n".join(configure(previous.splitlines(), site)) + "\n"
    if wanted == previous:
        return False
    _replace(Path(path).with_name(Path(path).name + ".pmasite.bak"), previous, status)
    _replace(Path(path), wanted, status)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puts the phpMyAdmin site in OpenLiteSpeed's config.")
    parser.add_argument("--config", required=True, help="OpenLiteSpeed's conf/httpd_config.conf")
    parser.add_argument("--name", required=True, help="the site's name, like pma.web01.example.nl")
    parser.add_argument("--root", required=True, help="the site's directory")
    parser.add_argument("--vhost-config", required=True, help="where the site's own settings are")
    parser.add_argument("--note", default=NOTE, help="what the virtual host says about who manages it")
    arguments = parser.parse_args(argv)
    site = Site(arguments.name, arguments.root, arguments.vhost_config, arguments.note)
    try:
        changed = apply(Path(arguments.config), site)
    except ConfigError as problem:
        print(problem, file=sys.stderr)
        return 1
    print(f"Changed: {site.name} is in OpenLiteSpeed's config, on its listeners for port 80 and 443." if changed
          else f"No change: OpenLiteSpeed already serves {site.name}.")
    return 0


def _settings(lines: list[str], block: Block) -> list[_Setting]:
    """The block's own settings, not those of blocks inside it."""
    settings: list[_Setting] = []
    inner = 0  # how deep inside blocks of this block we are
    heredoc = None
    index = block.start + 1
    while index < block.end:
        text = lines[index].strip()
        if heredoc:
            heredoc = None if _ends(text, heredoc) else heredoc
        elif not text or text.startswith("#"):
            pass
        elif _HEREDOC.search(text):
            marker = _HEREDOC.search(text).group(1)
            end = next((line for line in range(index + 1, block.end) if _ends(lines[line].strip(), marker)), block.end)
            if inner:
                heredoc = marker
            else:
                settings.append(_Setting(*_split(text), index, end))
                index = end
        elif text.endswith("{"):
            inner += 1
        elif text.startswith("}"):
            inner -= 1
        elif not inner:
            settings.append(_Setting(*_split(text), index, index))
        index += 1
    return settings


def _ours(lines: list[str], note: str) -> list[str]:
    return [block.name for block in blocks(lines)
            if block.kind == "virtualhost" and not block.depth and value(lines, block, "note") == note]


def _with_virtual_host(lines: list[str], site: Site) -> list[str]:
    """The config with the site's virtual host, added at the end or with its settings brought up to date.

    It runs scripts, phpMyAdmin being PHP, and stays inside its own directory: no symbolic links out of it, and no
    user of its own, so OpenLiteSpeed's own user serves it.
    """
    settings = {"vhRoot": site.root, "configFile": site.config_file, "allowSymbolLink": "0", "enableScript": "1",
                "restrained": "1", "setUIDMode": "0", "note": site.note}
    found = _top_block(lines, "virtualhost", site.name)
    if not found:
        added = [f"virtualhost {site.name} {{", *(_line(2, key, setting) for key, setting in settings.items()), "}"]
        return [*lines, *([""] if lines and lines[-1].strip() else []), *added]
    for key, wanted in settings.items():
        existing = next((setting for setting in _settings(lines, found) if setting.key.lower() == key.lower()), None)
        if existing and existing.value == wanted:
            continue
        start, end = (existing.start, existing.end) if existing else (found.start + 1, found.start)
        lines = [*lines[:start], _line(2, key, wanted), *lines[end + 1:]]
        found = _top_block(lines, "virtualhost", site.name)
    return lines


def _without_virtual_host(lines: list[str], name: str) -> list[str]:
    found = _top_block(lines, "virtualhost", name)
    if not found:
        return lines
    start = found.start - 1 if found.start and not lines[found.start - 1].strip() else found.start
    return [*lines[:start], *lines[found.end + 1:]]


def _with_map(lines: list[str], listener: str, name: str) -> list[str]:
    """The config with the listener giving the site that name, and no other."""
    if _maps(lines, listener).get(name) == [name]:
        return lines
    lines = _without_map(lines, listener, name)
    return _insert(lines, _top_block(lines, "listener", listener).end, _line(2, "map", f"{name} {name}"))


def _without_map(lines: list[str], listener: str, host: str) -> list[str]:
    dropped = {index for setting in _map_settings(lines, listener) if setting.value.split(None, 1)[0] == host
               for index in range(setting.start, setting.end + 1)}
    return [line for index, line in enumerate(lines) if index not in dropped]


def _maps(lines: list[str], listener: str) -> dict[str, list[str]]:
    """The names the listener gives each virtual host ("map VHOST NAME, NAME")."""
    mapped: dict[str, list[str]] = {}
    for setting in _map_settings(lines, listener):
        host, _, names = setting.value.partition(" ")
        mapped.setdefault(host, []).extend(name for name in re.split(r"[,\s]+", names) if name)
    return mapped


def _map_settings(lines: list[str], listener: str) -> list[_Setting]:
    found = _top_block(lines, "listener", listener)
    return [setting for setting in (_settings(lines, found) if found else []) if setting.key.lower() == "map"]


def _top_block(lines: list[str], kind: str, name: str) -> Block | None:
    return next((block for block in blocks(lines)
                 if block.kind == kind.lower() and not block.depth and block.name == name), None)


def _insert(lines: list[str], before: int, line: str) -> list[str]:
    return [*lines[:before], line, *lines[before:]]


def _line(indent: int, key: str, setting: str) -> str:
    return f"{' ' * indent}{key.ljust(_VALUE_COLUMN - indent - 1)} {setting}"


def _split(text: str) -> tuple[str, str]:
    key, setting = (text.split(None, 1) + [""])[:2]
    return key, setting.strip()


def _ends(text: str, marker: str) -> bool:
    """Whether the line ends a multi-line value: OpenLiteSpeed compares the first word, in any case."""
    return bool(text) and text.split(None, 1)[0].lower() == marker.lower()


def _unreadable(reason: str) -> ConfigError:
    return ConfigError(f"Can't read OpenLiteSpeed's config: {reason}. It reads the layout WebAdmin writes: a block's "
                       "{ at the end of its first line, and its } on a line of its own.")


def _read(path: Path) -> tuple[str, os.stat_result]:
    """The config, and what to give the new one. Nothing here follows a symbolic link: the config belongs to
    WebAdmin's user, and this runs as root."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConfigError(f"There is no OpenLiteSpeed config at {path}. The web role installs OpenLiteSpeed.") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ConfigError(f"{path} is a symbolic link, which this doesn't follow.") from None
        raise ConfigError(f"Can't read {path}: {error.strerror}.") from None
    with os.fdopen(descriptor) as file:
        return file.read(), os.fstat(file.fileno())


def _replace(path: Path, content: str, status: os.stat_result) -> None:
    """Writes the file in one step, with the owner and mode of the config it came from."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(content)
            os.fchown(file.fileno(), status.st_uid, status.st_gid)
            os.fchmod(file.fileno(), stat.S_IMODE(status.st_mode))
        os.replace(name, path)  # replaces a symbolic link at path instead of following it
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
