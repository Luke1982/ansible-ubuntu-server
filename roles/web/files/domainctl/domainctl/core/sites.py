"""The sites: members of one OpenLiteSpeed template in httpd_config.conf.

A member's name is the Linux user, because the template finds everything by it: the home directory it serves
from ($VH_NAME) and the Let's Encrypt certificate it presents. So a site's certificate is named after its user
too, not after its domain.

domainctl only adds and removes members of its own template and leaves every other virtual host, and the rest of
the config, exactly as it is.

Which block that template is, is decided by name, and OpenLiteSpeed ships one called Webhosting: a server set up
by hand has its sites in that. So a block whose name differs only in case is the same template, and domainctl
manages the one that is there instead of adding a twin beside it, which would leave those sites out of every
listing and out of everything it does, while looking like nothing was wrong.
"""

from dataclasses import dataclass

from serverctl import openlitespeed
from serverctl.errors import CtlError
from serverctl.openlitespeed import Member, Template

from ..config import TOOL, Config

NOTE = "Managed by domainctl"


@dataclass(frozen=True)
class Site:
    user: str  # the Linux user, the member name, and the name of the certificate
    domain: str
    aliases: tuple[str, ...] = ()  # www.DOMAIN, when it points to this server

    @property
    def names(self) -> tuple[str, ...]:
        """The names the certificate covers, the domain first."""
        return (self.domain, *self.aliases)


def read(config: Config) -> list[str]:
    return openlitespeed.read(config.ols_root)


def managed(config: Config, lines: list[str]) -> str:
    """The name of the template block domainctl manages: a block already there whose name differs from the one it
    is configured for only in case, or else that configured name."""
    found = _matching(config, lines)
    return found[0] if found else config.template


def twins(config: Config, lines: list[str]) -> list[str]:
    """The other blocks with that same name in another case. OpenLiteSpeed then has two templates for one set of
    sites, and only one of them is served what domainctl writes, so it is worth saying so."""
    return _matching(config, lines)[1:]


def _matching(config: Config, lines: list[str]) -> list[str]:
    """Every template block with that name in any case, the one to manage first: the one that has the sites, or
    else the one named exactly as configured. A server can have both, and the sites decide which is the real one."""
    found = [block.name for block in openlitespeed.blocks(lines)
             if block.kind == "vhtemplate" and not block.depth and block.name.lower() == config.template.lower()]
    return sorted(found, key=lambda name: (not openlitespeed.members(lines, name), name != config.template))


def template(config: Config, lines: list[str]) -> Template:
    """The template block domainctl keeps up to date, on every listener OpenLiteSpeed has for HTTP and HTTPS."""
    http, https = openlitespeed.listeners(lines)
    if not http:
        raise CtlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a site.",
                       hint="Add one in WebAdmin.")
    if not https:
        raise CtlError("OpenLiteSpeed has no HTTPS listener on port 443, so a site can't be served over HTTPS.",
                       hint="Add one in WebAdmin.")
    return Template(managed(config, lines), config.template_file, (*http, *https), NOTE)


def list_sites(config: Config, lines: list[str] | None = None) -> list[Site]:
    lines = read(config) if lines is None else lines
    members = openlitespeed.member_details(lines, managed(config, lines))
    return sorted((Site(member.name, member.domain, member.aliases) for member in members), key=lambda s: s.user)


def find(config: Config, user: str, lines: list[str] | None = None) -> Site | None:
    return next((site for site in list_sites(config, lines) if site.user == user), None)


def taken_by_another(config: Config, names: tuple[str, ...], user: str, lines: list[str]) -> str | None:
    """A name that another site or virtual host already serves, so domainctl doesn't quietly take it over."""
    for site in list_sites(config, lines):
        if site.user == user:
            continue
        clash = next((name for name in names if name in site.names), None)
        if clash:
            return f"{clash} is already served by the site {site.user}."
    hosts = openlitespeed.virtual_hosts(lines)
    clash = next((name for name in names if name in hosts), None)
    if clash:
        return f"OpenLiteSpeed has a virtual host of its own named {clash}, which domainctl leaves alone."
    return None


def save(config: Config, site: Site) -> bool:
    """Puts the site in the template, adding the template block itself when it isn't there yet. Returns whether
    OpenLiteSpeed's config changed."""
    with openlitespeed.locked():
        lines = read(config)
        updated = openlitespeed.with_member(_without(lines, config, site.user),
                                            Member(site.user, site.domain, site.aliases), template(config, lines))
        return _write(config, lines, updated)


def remove(config: Config, user: str) -> bool:
    """Takes the site out of the template. Returns whether OpenLiteSpeed's config changed."""
    with openlitespeed.locked():
        lines = read(config)
        return _write(config, lines, _without(lines, config, user))


def ensure_template(config: Config) -> bool:
    """Makes sure the template block is in the config, with its file and every HTTP and HTTPS listener, and its
    members untouched. Ansible runs this, so a server has the block before the first site is added."""
    with openlitespeed.locked():
        lines = read(config)
        return _write(config, lines, openlitespeed.with_template(lines, template(config, lines)))


def _without(lines: list[str], config: Config, user: str) -> list[str]:
    return openlitespeed.without_member(lines, user, managed(config, lines))


def _write(config: Config, lines: list[str], updated: list[str]) -> bool:
    if updated == lines:
        return False
    openlitespeed.write(config.ols_root, updated, TOOL)
    return True
