"""Collabora Online's own daemon, coolwsd: the settings this server needs, and which Nextclouds may use it.

The package ships a long settings file with everything coolwsd can do in it, documented line by line, and a new
version of the package brings new settings in it. So this doesn't write that file: it reads it, changes the
handful of values that are this server's business, and writes it back with everything else where it was,
comments and all.

Two things are its business. First, that coolwsd speaks plain HTTP on the loopback only: OpenLiteSpeed stands in
front of it with the certificate, which is what "ssl.termination" means. Second, which Nextclouds may ask it to
open a document, because Collabora refuses every host that isn't on that list. officectl keeps the list: it
holds the Nextclouds on this server and nothing else, so a host added to it by hand is taken out again on the
next run.
"""

import re
import shutil
from pathlib import Path
from xml.dom import minidom
from xml.dom.minidom import Document, Element
from xml.parsers.expat import ExpatError

from serverctl import files, system
from serverctl.errors import CtlError

from ..config import Config

MODE = 0o640  # what the package gives the settings file: coolwsd's own user reads it, nobody else
INDENT = "\t"  # what coolwsd's own tools write; only used where the file gives nothing to copy


def scalars(config: Config) -> dict[tuple[str, ...], str]:
    """The settings officectl keeps, by the path of elements they are at."""
    return {
        # OpenLiteSpeed holds the certificate and talks plain HTTP to coolwsd over the loopback. Without
        # termination, coolwsd builds the addresses in its pages with http:// in them and the browser refuses
        # them on an HTTPS page.
        ("ssl", "enable"): "false",
        ("ssl", "termination"): "true",
        ("net", "listen"): config.coolwsd_listen,
        ("net", "proto"): config.coolwsd_protocol,
        # The name it calls itself in its admin pages, and the one it builds the addresses of documents with.
        ("server_name",): config.office_host,
    }


def attributes(config: Config) -> dict[tuple[str, ...], dict[str, str]]:
    """The attributes officectl keeps, by the path of elements they are on."""
    return {
        ("storage", "wopi"): {"allow": "true"},
        # "groups": only the hosts listed below may use it. In the other mode, "first", whichever host connects
        # first after a restart becomes the only one that may.
        ("storage", "wopi", "alias_groups"): {"mode": "groups"},
    }


def allowed(config: Config, hosts: list[str]) -> list[str]:
    """Every Nextcloud Collabora may open documents for: the ones officectl made on this server, and the ones
    the settings name as not its own (other_nextclouds), so an older Nextcloud set up by hand keeps working."""
    return sorted(set(hosts) | set(config.other_hosts()))


def host_pattern(host: str) -> str:
    r"""A Nextcloud's name as Collabora matches it: the host part is a regular expression, so the dots are
    escaped. A dot would otherwise stand for any character, and acmeXoffice.example.nl would be let in too."""
    return f"https://{re.escape(host)}:443"


def read(path: Path) -> Document:
    try:
        return minidom.parse(str(path))
    except FileNotFoundError:
        raise CtlError(f"There are no Collabora settings at {path}.",
                       hint="The office role of the Ansible playbook installs Collabora.") from None
    except OSError as error:
        raise CtlError(f"Can't read {path}: {error.strerror}.") from None
    except ExpatError as error:
        raise CtlError(f"{path} isn't valid XML: {error}.",
                       hint="Put Collabora's own file back (coolwsd.xml.dist, next to it) and run this again.") \
            from None


def render(document: Document) -> str:
    """The document as text, laid out as coolwsd's own file is: what stands before the settings themselves, each
    on a line of its own, and the settings with the whitespace they were read with.

    The document is not written with toxml() as a whole: that puts an <?xml?> line at the top the file doesn't
    have, and drops the line breaks between the comment at the top and the settings, because XML has no place
    for text outside them.
    """
    before = "".join(f"{node.toxml()}\n" for node in document.childNodes if node is not document.documentElement)
    return f"{before}{document.documentElement.toxml()}\n"


def write(path: Path, document: Document) -> bool:
    """Writes the settings back in one step, with the owner and mode they have. Returns whether they changed."""
    return files.replace_keeping_owner(path, render(document), MODE)


def apply(document: Document, config: Config, hosts: list[str]) -> bool:
    """Brings the settings in the document up to date. Returns whether anything changed."""
    root = document.documentElement
    changed = False
    for path, value in scalars(config).items():
        changed |= _set_text(document, _element(document, root, path), value)
    for path, wanted in attributes(config).items():
        element = _element(document, root, path)
        for name, value in wanted.items():
            if element.getAttribute(name) != value:
                element.setAttribute(name, value)
                changed = True
    groups = _element(document, root, ("storage", "wopi", "alias_groups"))
    return _set_groups(document, groups, hosts) or changed


def configure(config: Config, hosts: list[str]) -> bool:
    """Brings coolwsd's settings up to date, letting in these Nextclouds and the ones the settings name as not
    officectl's own. Returns whether the file changed; coolwsd reads it when it restarts."""
    document = read(config.coolwsd_config)
    if not apply(document, config, allowed(config, hosts)):
        return False
    return write(config.coolwsd_config, document)


def allowed_hosts(document: Document) -> list[str]:
    """The names Collabora lets in now, as they are written in the settings."""
    groups = _find(document.documentElement, ("storage", "wopi", "alias_groups"))
    if groups is None:
        return []
    return [_text_of(host) for group in _children(groups, "group") for host in _children(group, "host")]


def installed(config: Config) -> bool:
    return bool(shutil.which("coolwsd")) and config.coolwsd_config.is_file()


def running(config: Config) -> bool:
    try:
        return system.run("systemctl", "is-active", config.coolwsd_service).strip() == "active"
    except CtlError:
        return False  # systemctl answers "inactive" with a failing status, which is not an error here


def version() -> str:
    """What Collabora says its version is, or "" when it can't be asked."""
    try:
        return system.run("coolwsd", "--version").splitlines()[0].strip()
    except (CtlError, IndexError):
        return ""


def restart(config: Config) -> None:
    """Restarts coolwsd, so it reads its settings again. A document somebody has open is closed, and their last
    few seconds of typing are lost, so this only runs when something really changed."""
    system.run("systemctl", "restart", config.coolwsd_service)


# --- The XML itself ------------------------------------------------------------------------------------------

def _children(parent: Element, name: str) -> list[Element]:
    return [node for node in parent.childNodes
            if node.nodeType == node.ELEMENT_NODE and node.tagName == name]


def _find(parent: Element, path: tuple[str, ...]) -> Element | None:
    """The element at the path, counted from the parent, or None when a step of it is missing.

    The path is followed step by step rather than searched for: coolwsd's settings hold more than one element
    called "ssl", and getElementsByTagName would find the wrong one.
    """
    found = parent
    for name in path:
        children = _children(found, name)
        if not children:
            return None
        found = children[0]
    return found


def _element(document: Document, parent: Element, path: tuple[str, ...]) -> Element:
    """The element at the path, added where it is missing: a setting coolwsd has no line for yet still gets one."""
    found = parent
    for name in path:
        children = _children(found, name)
        found = children[0] if children else _add(document, found, name)
    return found


def _add(document: Document, parent: Element, name: str) -> Element:
    """A new element at the end of the parent, on a line of its own with the indentation its neighbours have."""
    element = document.createElement(name)
    _append_indented(document, parent, element)
    return element


def _append_indented(document: Document, parent: Element, element: Element) -> None:
    inside, closing = _indents(parent)
    while parent.lastChild is not None and _is_blank(parent.lastChild):
        parent.removeChild(parent.lastChild)
    parent.appendChild(document.createTextNode(inside))
    parent.appendChild(element)
    parent.appendChild(document.createTextNode(closing))


def _indents(parent: Element) -> tuple[str, str]:
    """The whitespace before a child of this element, and before its own closing tag."""
    closing = _indent_before(parent)
    inside = next((_indent_before(node) for node in parent.childNodes
                   if node.nodeType == node.ELEMENT_NODE), closing + INDENT)
    return inside, closing


def _indent_before(element: Element) -> str:
    """The whitespace on the line in front of the element: what its own indentation is."""
    before = element.previousSibling
    if before is None or before.nodeType != before.TEXT_NODE or not _is_blank(before):
        return ""
    return "\n" + before.data.rpartition("\n")[2]


def _is_blank(node) -> bool:
    return node.nodeType == node.TEXT_NODE and not node.data.strip()


def _text_of(element: Element) -> str:
    return "".join(node.data for node in element.childNodes if node.nodeType == node.TEXT_NODE)


def _set_text(document: Document, element: Element, value: str) -> bool:
    """Puts the value in the element, leaving its attributes alone. Returns whether it changed."""
    if _text_of(element) == value:
        return False
    for node in [node for node in element.childNodes if node.nodeType == node.TEXT_NODE]:
        element.removeChild(node)
    element.insertBefore(document.createTextNode(value), element.firstChild)
    return True


def _set_groups(document: Document, groups: Element, hosts: list[str]) -> bool:
    """Makes the list of hosts that may use Collabora exactly these. Returns whether it changed.

    A group is one Nextcloud: the host it is reached at, and the other names it may also be reached at, which
    these don't have. The comments in the file, Collabora's own examples among them, are left where they are.
    """
    wanted = [host_pattern(host) for host in hosts]
    if allowed_hosts(document) == wanted:
        return False
    for group in _children(groups, "group"):
        before = group.previousSibling
        groups.removeChild(group)
        if before is not None and _is_blank(before):
            groups.removeChild(before)
    inside, closing = _indents(groups)
    while groups.lastChild is not None and _is_blank(groups.lastChild):
        groups.removeChild(groups.lastChild)
    for pattern in wanted:
        host = document.createElement("host")
        host.setAttribute("allow", "true")
        host.appendChild(document.createTextNode(pattern))
        group = document.createElement("group")
        group.appendChild(document.createTextNode(inside + INDENT))
        group.appendChild(host)
        group.appendChild(document.createTextNode(inside))
        groups.appendChild(document.createTextNode(inside))
        groups.appendChild(group)
    groups.appendChild(document.createTextNode(closing))
    return True
