"""OpenLiteSpeed's server config (httpd_config.conf): its listeners, virtual hosts, and the members of templates.

The config is set up by hand in WebAdmin, so mailctl only adds and removes its own virtual hosts, template members and
the listeners' names for them, in the layout WebAdmin writes, and leaves everything else as it is. It doesn't use 'include': WebAdmin makes a config with
includes read-only.
"""

import errno
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import system
from .errors import MailctlError

# A value spanning lines, like rewrite rules: "rules <<<END_rules", up to a line holding just "END_rules".
_HEREDOC = re.compile(r"<<<\s*(\S+)\s*$")
_VALUE_COLUMN = 26  # where WebAdmin lines up values


@dataclass(frozen=True)
class Block:
    kind: str  # the first word, in lower case, like "listener" or "member"
    name: str
    start: int  # the line with the opening brace
    end: int  # the line with the closing brace
    depth: int  # 0 for the top level


@dataclass(frozen=True)
class Template:
    name: str
    file: str  # relative to OpenLiteSpeed's directory, like conf/templates/example.conf
    listeners: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class Member:
    name: str
    domain: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VirtualHost:
    """A virtual host of its own, for a site that needs settings a template can't give each member."""
    name: str
    root: str
    config_file: str
    note: str


def config_file(root: Path) -> Path:
    return root / "conf" / "httpd_config.conf"


def read(root: Path) -> list[str]:
    return _read(config_file(root))[0].splitlines()


def write(root: Path, lines: list[str]) -> None:
    """Replaces the config in one step, with the same owner and mode, and keeps the previous version next to it.

    The config directory belongs to WebAdmin's user, and mailctl runs as root, so nothing here follows a symbolic
    link someone put there: files are made new and renamed into place, and owner and mode are set on the open file.
    """
    path = config_file(root)
    previous, status = _read(path)
    _replace(path.with_name(f"{path.name}.mailctl.bak"), previous, status)
    _replace(path, "\n".join(lines) + "\n", status)


def _read(path: Path) -> tuple[str, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise MailctlError(f"There is no OpenLiteSpeed config at {path}.",
                           hint="The web role of the Ansible playbook installs OpenLiteSpeed.") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise MailctlError(f"{path} is a symbolic link, which mailctl doesn't follow.") from None
        raise MailctlError(f"Can't read {path}: {error.strerror}.") from None
    with os.fdopen(descriptor) as file:
        return file.read(), os.fstat(file.fileno())


def _replace(path: Path, content: str, status: os.stat_result) -> None:
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


def restart(root: Path) -> None:
    """A graceful restart: requests being handled finish first. When OpenLiteSpeed isn't running, this starts it."""
    output = system.run_starter(str(root / "bin" / "lswsctrl"), "restart")
    if "[ERROR]" in output:  # lswsctrl reports some failures only in its output
        raise MailctlError(f"lswsctrl restart failed: {output}")


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


@dataclass(frozen=True)
class _Setting:
    key: str
    value: str
    start: int
    end: int  # the last line: for a multi-line value, the line that ends it


def _settings(lines: list[str], block: Block) -> list[_Setting]:
    """The block's own settings, not those of blocks inside it."""
    inner = {other.start: other for other in blocks(lines) if block.start < other.start and other.end < block.end}
    settings = []
    index = block.start + 1
    while index < block.end:
        if index in inner:
            index = inner[index].end + 1
            continue
        text = lines[index].strip()
        if text and not text.startswith("#"):
            key, setting = (text.split(None, 1) + [""])[:2]
            end = index
            multi_line = _HEREDOC.search(text)
            if multi_line:
                end = next(line for line in range(index + 1, block.end) if _ends(lines[line].strip(), multi_line.group(1)))
            settings.append(_Setting(key, setting.strip(), index, end))
            index = end
        index += 1
    return settings


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


def members(lines: list[str], template: str) -> list[str]:
    found = _template(lines, template)
    if not found:
        return []
    return [block.name for block in blocks(lines)
            if block.kind == "member" and found.start < block.start < found.end]


def with_member(lines: list[str], member: Member, template: Template, others: tuple[Template, ...] = ()) -> list[str]:
    """The config with the member in the template, and in none of the others. The template is added when it's
    missing, and its file and listeners are brought up to date."""
    for other in others:
        lines = without_member(lines, member.name, other.name)
    lines = _with_template(lines, template)
    if member.name in members(lines, template.name):
        return lines
    end = _template(lines, template.name).end
    added = ["", f"  member {member.name} {{", _setting(4, "vhDomain", member.domain)]
    if member.aliases:
        added.append(_setting(4, "vhAliases", ", ".join(member.aliases)))
    return [*lines[:end], *added, "  }", *lines[end:]]


def without_member(lines: list[str], name: str, template: str) -> list[str]:
    found = _template(lines, template)
    for block in blocks(lines) if found else []:
        if block.kind == "member" and block.name == name and found.start < block.start < found.end:
            start = block.start - 1 if not lines[block.start - 1].strip() else block.start
            return [*lines[:start], *lines[block.end + 1:]]
    return lines


def virtual_hosts(lines: list[str]) -> dict[str, str | None]:
    """The virtual hosts, with their notes."""
    return {block.name: value(lines, block, "note") for block in blocks(lines)
            if block.kind == "virtualhost" and not block.depth}


def with_virtual_host(lines: list[str], host: VirtualHost) -> list[str]:
    """The config with the virtual host, added or brought up to date. It runs no scripts and follows no links."""
    settings = {"vhRoot": host.root, "configFile": host.config_file, "allowSymbolLink": "0", "enableScript": "0",
                "restrained": "1", "note": host.note}
    return _with_block(lines, "virtualhost", host.name, settings)


def without_virtual_host(lines: list[str], name: str) -> list[str]:
    found = _top_block(lines, "virtualhost", name)
    if not found:
        return lines
    start = found.start - 1 if found.start and not lines[found.start - 1].strip() else found.start
    return [*lines[:start], *lines[found.end + 1:]]


def maps(lines: list[str], listener: str) -> dict[str, list[str]]:
    """The names the listener gives each virtual host ("map VHOST NAME, NAME")."""
    mapped: dict[str, list[str]] = {}
    for setting in _maps(lines, listener):
        host, names = (setting.value.split(None, 1) + [""])[:2]
        mapped.setdefault(host, []).extend(name for name in re.split(r"[,\s]+", names) if name)
    return mapped


def with_map(lines: list[str], listener: str, host: str, name: str) -> list[str]:
    """The config with the listener mapping the name, and nothing else, to the virtual host."""
    if maps(lines, listener).get(host) == [name]:
        return lines
    lines = without_map(lines, listener, host)
    end = _top_block(lines, "listener", listener).end
    return [*lines[:end], _setting(2, "map", f"{host} {name}"), *lines[end:]]


def without_map(lines: list[str], listener: str, host: str) -> list[str]:
    dropped = {index for setting in _maps(lines, listener) if setting.value.split(None, 1)[0] == host
               for index in range(setting.start, setting.end + 1)}
    return [line for index, line in enumerate(lines) if index not in dropped]


def _maps(lines: list[str], listener: str) -> list[_Setting]:
    found = _top_block(lines, "listener", listener)
    return [setting for setting in (_settings(lines, found) if found else []) if setting.key.lower() == "map"]


def _with_template(lines: list[str], template: Template) -> list[str]:
    settings = {"templateFile": template.file, "listeners": ", ".join(template.listeners), "note": template.note}
    return _with_block(lines, "vhTemplate", template.name, settings)


def _with_block(lines: list[str], kind: str, name: str, settings: dict[str, str]) -> list[str]:
    """The config with the top-level block, added at the end or with its settings brought up to date. A setting is
    replaced whole, a multi-line one included."""
    found = _top_block(lines, kind, name)
    if not found:
        added = [f"{kind} {name} {{", *(_setting(2, key, setting) for key, setting in settings.items()), "}"]
        return [*lines, *([""] if lines and lines[-1].strip() else []), *added]
    for key, wanted in settings.items():
        existing = next((setting for setting in _settings(lines, found) if setting.key.lower() == key.lower()), None)
        if existing and existing.value == wanted:
            continue
        start, end = (existing.start, existing.end) if existing else (found.start + 1, found.start)
        lines = [*lines[:start], _setting(2, key, wanted), *lines[end + 1:]]
        found = _top_block(lines, kind, name)
    return lines


def _template(lines: list[str], name: str) -> Block | None:
    return _top_block(lines, "vhTemplate", name)


def _top_block(lines: list[str], kind: str, name: str) -> Block | None:
    return next((block for block in blocks(lines)
                 if block.kind == kind.lower() and not block.depth and block.name == name), None)


def _setting(indent: int, key: str, setting: str) -> str:
    return f"{' ' * indent}{key.ljust(_VALUE_COLUMN - indent - 1)} {setting}"


def _ends(text: str, marker: str) -> bool:
    """Whether the line ends a multi-line value: OpenLiteSpeed compares the first word, in any case."""
    return bool(text) and text.split(None, 1)[0].lower() == marker.lower()


def _unreadable(reason: str) -> MailctlError:
    return MailctlError(f"mailctl can't read OpenLiteSpeed's config: {reason}.",
                        hint="mailctl reads the layout WebAdmin writes: a block's { at the end of its first line, "
                             "and its } on a line of its own.")
