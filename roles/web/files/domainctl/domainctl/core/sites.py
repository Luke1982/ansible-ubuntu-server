"""The sites: members of one OpenLiteSpeed template in httpd_config.conf.

A member's name is the Linux user, because the template finds everything by it: the home directory it serves
from ($VH_NAME) and the Let's Encrypt certificate it presents. So a site's certificate is named after its user
too, not after its domain.

A site whose pages are not in the place the template serves -- the public folder of a Laravel or Symfony
application, say -- cannot be a member, because a member has no document root of its own. It gets a virtual
host to itself instead (core/ownhost.py), with the same settings and its own document root. Everything else
about it is the same site: the Linux user, its directories, and the certificate named after the user.

domainctl only adds and removes members of its own template and the virtual hosts of its own sites, and leaves
every other virtual host, and the rest of the config, exactly as it is.

Which block that template is, is decided by name, and OpenLiteSpeed ships one called Webhosting: a server set up
by hand has its sites in that. So a block whose name differs only in case is the same template, and domainctl
manages the one that is there instead of adding a twin beside it, which would leave those sites out of every
listing and out of everything it does, while looking like nothing was wrong.
"""

from dataclasses import dataclass
from pathlib import Path

from serverctl import openlitespeed
from serverctl.errors import CtlError
from serverctl.openlitespeed import Member, Template

from . import layout, ownhost

from ..config import TOOL, Config

NOTE = "Managed by domainctl"
# The note on the virtual host of a site that has a web root of its own, with that web root at the end of it:
# WebAdmin shows it, and domainctl reads the web root back from it.
OWN_NOTE = "Managed by domainctl: served from "


@dataclass(frozen=True)
class Site:
    user: str  # the Linux user, the member name, and the name of the certificate
    domain: str
    aliases: tuple[str, ...] = ()  # www.DOMAIN, when it points to this server
    # Where its pages are, inside the home directory, when that isn't the place the template serves. A site
    # that has one is served by a virtual host of its own.
    webroot: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        """The names the certificate covers, the domain first."""
        return (self.domain, *self.aliases)

    @property
    def own_host(self) -> bool:
        """Whether the site has a virtual host of its own instead of being a member of the template."""
        return bool(self.webroot)


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
    """Every site: the members of the template, and the ones with a virtual host of their own."""
    lines = read(config) if lines is None else lines
    members = openlitespeed.member_details(lines, managed(config, lines))
    found = [Site(member.name, member.domain, member.aliases) for member in members]
    return sorted([*found, *_own_sites(lines)], key=lambda site: site.user)


def _own_sites(lines: list[str]) -> list[Site]:
    """The sites served by a virtual host of their own: their note says domainctl made it and where it serves
    from, and the listeners say which names they answer for."""
    found = []
    for name, note in openlitespeed.virtual_hosts(lines).items():
        if not note or not note.startswith(OWN_NOTE):
            continue
        served = _served_names(lines, name)
        if served:
            found.append(Site(name, served[0], tuple(served[1:]), note[len(OWN_NOTE):].strip()))
    return found


def _served_names(lines: list[str], host: str) -> list[str]:
    """The names a listener gives the virtual host, the domain first, as domainctl wrote them."""
    http, https = openlitespeed.listeners(lines)
    for listener in (*http, *https):
        names = openlitespeed.maps(lines, listener).get(host)
        if names:
            return names
    return []


def docroot(config: Config, user: str, lines: list[str] | None = None) -> Path:
    """Where a site's pages are: its own web root when it has one, and the template's place when it hasn't.

    For the commands that are given a user name and no site, like the WordPress ones.
    """
    site = find(config, user, lines)
    return config.docroot_of(user, site.webroot if site else "")


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
    # A site with a web root of its own is served by a virtual host named after its Linux user, so one of that
    # name that somebody else made must not be written over.
    if user in hosts and not (hosts[user] or "").startswith(OWN_NOTE):
        return f"OpenLiteSpeed already has a virtual host named {user}, which domainctl didn't make."
    return None


def save(config: Config, site: Site) -> bool:
    """Puts the site in OpenLiteSpeed's config: in the template, or in a virtual host of its own when it has a
    web root of its own. Returns whether anything changed; OpenLiteSpeed reads it when it restarts.

    A site that is given a web root, or has one taken away again, moves from the one to the other, so it is
    never served twice.
    """
    changed = False
    if site.own_host:
        changed = ownhost.write_settings(config, site.user, site.domain, site.webroot)
    with openlitespeed.locked():
        lines = read(config)
        if site.own_host:
            updated = ownhost.publish(config, _without(lines, config, site.user), site.user, site.names,
                                      f"{OWN_NOTE}{site.webroot}")
        else:
            updated = openlitespeed.with_member(ownhost.unpublish(_without(lines, config, site.user), site.user),
                                                Member(site.user, site.domain, site.aliases),
                                                template(config, lines))
        changed |= _write(config, lines, updated)
    if not site.own_host:
        changed |= ownhost.remove_settings(config, site.user)
    return changed


def remove(config: Config, user: str) -> bool:
    """Takes the site out of the template, or away as a virtual host of its own. Returns whether anything
    changed."""
    with openlitespeed.locked():
        lines = read(config)
        own = (openlitespeed.virtual_hosts(lines).get(user) or "").startswith(OWN_NOTE)
        updated = _without(lines, config, user)
        changed = _write(config, lines, ownhost.unpublish(updated, user) if own else updated)
    if own:
        changed |= ownhost.remove_settings(config, user)
    return changed


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

def waiting_for_dns(config: Config, lines: list[str] | None = None) -> list[str]:
    """The sites domainctl made whose virtual host is still waiting for the domain to point here: their Linux
    user and directories are there, but they are in no template, so list_sites() does not see them. A site
    whose files were uploaded before the DNS moved sits here until "domainctl add" is run again."""
    served = {site.user for site in list_sites(config, lines)}
    try:
        homes = sorted(path.name for path in config.home_root.iterdir() if path.is_dir())
    except OSError:
        return []
    return [name for name in homes if name not in served and layout.made_by_domainctl(config, name)]
