"""domainctl's commands for the sites themselves: add, list, delete, check and sync."""

from datetime import UTC, datetime

from serverctl import certbot, names, openlitespeed, ui
from serverctl.dns import Check, Status
from serverctl.errors import CtlError
from serverctl.transip import NotInAccount

from ..core import certificates, dnsnames, layout, serving, sites, template, users
from ..core.dnsnames import NameCheck, State
from ..core.sites import Site
from ..session import Session, open_session
from . import dns
from .shared import (
    Domain, ExistingUser, NoDns, Purge, User, UserFilter, UserOption, Yes, ask_domain, ask_user, find_site, www,
)


def add(domain: Domain = None, user: UserOption = None, no_dns: NoDns = False,
        existing_user: ExistingUser = False, yes: Yes = False) -> None:
    """Set up a site for a domain: a Linux user, its directories, a virtual host and a certificate.

    The domain must already point to this server; www is included when it points here too, and left out when it
    doesn't, because one name that doesn't resolve here would fail the whole certificate request. A name with no
    record at all is offered to TransIP, if the domain is in that account.

    Run it again for the same domain to pick up where it stopped, for instance after fixing a DNS record.

    [dim]Example:[/] domainctl add example.nl
    [dim]Another user name:[/] domainctl add example.nl --user examplesite
    """
    with open_session() as session:
        config = session.config
        domain = ask_domain(domain)
        name = names.user_name(user) if user else _free_name(config, domain, existing_user)
        existing = sites.find(config, name)
        if existing and existing.domain != domain:
            raise CtlError(f"The site {name} already serves {existing.domain}.",
                           hint=f"Choose another user name with --user, or remove it with: domainctl delete {name}")
        checked = _check_names(session, domain, no_dns, yes)
        site = Site(name, domain, tuple(found.name for found in checked[1:] if found.ready))
        _reserve(config, site, existing is not None, existing_user)
        _make_home(config, site, existing_user)
        if sites.save(config, site):
            openlitespeed.restart(config.ols_root)
            ui.success(f"{domain} is served from {config.docroot_of(name)}.")
        _certify(config, site, checked[0])


def list_sites(user: UserFilter = None) -> None:
    """List the sites on this server with their domain, user and certificate.

    [dim]Example:[/] domainctl list
    """
    with open_session() as session:
        config = session.config
        found = [site for site in sites.list_sites(config) if user is None or site.user == user]
        _warn_about_twins(config)
        if not found:
            ui.note("There are no sites on this server yet." if user is None else f"There is no site {user}.")
            return
        now = datetime.now(UTC)
        table = ui.table("Site", "Domain", "Also", "Certificate")
        for site in found:
            ui.add_row(table, site.user, site.domain, ", ".join(site.aliases) or "–", _certificate(config, site, now))
        ui.console.print(table)


def delete(user: User = None, purge: Purge = False, yes: Yes = False) -> None:
    """Remove a site: its virtual host and its certificate.

    The Linux user and its home directory, with the site's files, are kept unless you add --purge.

    [dim]Example:[/] domainctl delete example
    [dim]Files and all:[/] domainctl delete example --purge
    """
    with open_session() as session:
        config = session.config
        site = find_site(config, ask_user(user))
        what = f"the site {site.user} ({site.domain}) and its certificate"
        if purge:
            ui.warn(f"--purge also deletes {config.home(site.user)} with everything in it. This cannot be undone.")
            ui.confirm_by_typing(site.user, f"Delete {what}, the Linux user and all its files?", yes)
        else:
            ui.confirm(f"Delete {what}?", yes)
        if sites.remove(config, site.user):
            openlitespeed.restart(config.ols_root)
        ui.success(f"Removed the site {site.user}.")
        if certificates.delete(config, site.user):
            ui.success(f"Removed the certificate for {', '.join(site.names)}.")
        if purge:
            users.delete(site.user, remove_home=True)
            ui.success(f"Removed the user {site.user} and {config.home(site.user)}.")
        else:
            ui.note(f"{config.home(site.user)} and the user {site.user} are still there. "
                    f"Remove them with: domainctl delete {site.user} --purge")


def check(user: UserFilter = None) -> None:
    """Say how each site is doing: its names, its file permissions and its certificate.

    [dim]Example:[/] domainctl check
    [dim]One site:[/] domainctl check example
    """
    with open_session() as session:
        config = session.config
        found = [site for site in sites.list_sites(config) if user is None or site.user == user]
        _warn_about_twins(config)
        if not found:
            ui.note("There are no sites on this server yet." if user is None else f"There is no site {user}.")
            return
        now = datetime.now(UTC)
        for site in found:
            ui.heading(f"{site.user} — {site.domain}")
            for result in _checks(session, site, now):
                ui.line(ui.mark(result.status), " ", result.detail, indent=1)


def sync() -> None:
    """Bring OpenLiteSpeed and the file permissions of every site up to date. Run by the playbook.

    It makes sure the template block is in OpenLiteSpeed's config with every listener, that the redirect to
    HTTPS is on, and that each site's directories still let OpenLiteSpeed in. It never adds or removes a site.

    [dim]Example:[/] domainctl sync
    """
    with open_session() as session:
        config = session.config
        changed = template.set_redirect(config, True)
        if changed:
            ui.warn("The redirect to HTTPS was switched off; it is on again.")
        try:
            changed |= sites.ensure_template(config)
        except CtlError as problem:
            # A server whose listeners aren't set up yet still gets its template and permissions in order, so
            # provisioning one doesn't stop here.
            ui.warn(problem.message)
            if problem.hint:
                ui.note(problem.hint)
        _warn_about_twins(config)
        for site in sites.list_sites(config):
            if layout.apply_permissions(config, site.user):
                ui.success(f"Corrected the file permissions of {site.user}.")
                changed = True
        if changed:
            openlitespeed.restart(config.ols_root)
        # Ansible reads this line to tell whether anything changed.
        if changed:
            ui.success("Changed the web sites.")
        else:
            ui.note("Nothing changed.")


def _warn_about_twins(config) -> None:
    """A template with the same name in another case holds sites domainctl says nothing about: OpenLiteSpeed keeps
    the two apart, so they are missing from every listing here, and from everything domainctl does."""
    lines = sites.read(config)
    others = sites.twins(config, lines)
    if not others:
        return
    ours = sites.managed(config, lines)
    ui.warn(f"OpenLiteSpeed has another template named {', '.join(others)}, which is {ours} in another case. "
            f"domainctl manages {ours}; the sites in the other one are left alone and are not listed here.")
    ui.note(f"Move its members into {ours}, or delete the empty template, in WebAdmin.")


def _free_name(config, domain: str, existing_user: bool) -> str:
    """The user name a domain gets by default, or an error naming what is in the way."""
    name = names.user_name_for(domain)
    site = sites.find(config, name)
    if site and site.domain == domain:
        return name  # the same site again: adding it once more picks up where it stopped
    if site:
        raise CtlError(f"The name {name}, taken from {domain}, is the site for {site.domain}.",
                       hint=f"Choose another one with --user, or remove that site with: domainctl delete {name}")
    if users.exists(name) and not existing_user:
        # A server being moved to domainctl has the user and its files already; that is what --existing-user is for.
        raise CtlError(f"There is already a Linux user {name}, taken from {domain}, but no site for it.",
                       hint=f"Give it this site with: domainctl add {domain} --existing-user\n"
                            f"Or make a site under another name with --user.")
    return names.user_name(name)


def _check_names(session: Session, domain: str, no_dns: bool, yes: bool) -> list[NameCheck]:
    """Where the domain and its www name point. The domain has to point here; www may be left out."""
    found = [_resolve(session, name, no_dns, yes) for name in (domain, www(domain))]
    if found[0].state is State.UNKNOWN:
        raise CtlError(f"Can't tell where {domain} points: {found[0].detail}",
                       hint="Try again when DNS answers.")
    if not found[0].ready:
        raise CtlError(f"{found[0].detail} Let's Encrypt checks the same thing, so no certificate is possible yet.",
                       hint=f"Point {domain} at {_addresses(session)} and run the command again.")
    ui.success(f"{domain} points to this server.")
    if found[1].ready:
        ui.success(f"{www(domain)} points here too, so it is included.")
    else:
        ui.warn(f"{found[1].detail} It is left out; run the command again once it points here.")
    return found


def _resolve(session: Session, name: str, no_dns: bool, yes: bool) -> NameCheck:
    """Looks the name up, and offers to publish it at TransIP when it has no record at all."""
    found = dnsnames.check(session.resolver, name, session.server_ips)
    if found.state is not State.MISSING or no_dns:
        return found
    ips = dnsnames.publishable_ips(session.server_ips)
    if not ips:
        return found
    if not ui.interactive() and not yes:
        ui.note(f"{name} has no record. Add --yes to publish one at TransIP without being asked.")
        return found
    if not ui.decide(f"{name} has no DNS record. Publish one at TransIP, pointing here?", True if yes else None,
                     "--yes", default=True):
        return found
    try:
        with ui.console.status(f"Publishing {name} at TransIP…"):
            zone, added = dnsnames.publish(dns.client(session, read_only=False), name, ips)
    except (CtlError, NotInAccount) as problem:
        ui.warn(f"Couldn't publish {name}: {problem.message}")
        return found
    ui.success(f"Published {len(added)} record(s) for {name} in the zone {zone}.")
    with ui.console.status(f"Waiting until {name} resolves…"):
        return dnsnames.wait_until_resolving(session.resolver, name, session.server_ips)


def _reserve(config, site: Site, is_retry: bool, existing_user: bool) -> None:
    """Checks that nothing else on this server already answers to the site's names."""
    lines = sites.read(config)
    clash = sites.taken_by_another(config, site.names, site.user, lines)
    if clash:
        raise CtlError(clash, hint="Remove it first, or choose another domain.")
    if is_retry or existing_user or not users.exists(site.user):
        return
    raise CtlError(f"There is already a Linux user {site.user} on this server, but no site for it.",
                   hint="Add --existing-user to use it and its home directory, or another name with --user.")


def _make_home(config, site: Site, existing_user: bool) -> None:
    if not users.exists(site.user):
        users.create(site.user, config.home(site.user), config.user_shell)
        ui.success(f"Made the user {site.user} with {config.home(site.user)}, without a password.")
    layout.create(config, site.user, site.domain)
    if layout.apply_permissions(config, site.user):
        ui.success(f"{config.web_user} may read {config.docroot_of(site.user)} and write in "
                   f"{config.logs_of(site.user)}.")


def _certify(config, site: Site, domain_check: NameCheck) -> None:
    """Gets the site's certificate, once OpenLiteSpeed really answers for it on port 80."""
    if certificates.exists(config, site.user):
        missing = [name for name in site.names
                   if not certbot.covers(certbot.read(certificates.path(config, site.user)), name)]
        if not missing:
            ui.success(f"The certificate already covers {', '.join(site.names)}.")
            return
        ui.note(f"The certificate doesn't cover {', '.join(missing)} yet, so it is extended.")
    with ui.console.status("Checking that OpenLiteSpeed serves the site…"):
        serving.wait_until_served(config.docroot_of(site.user), site.domain, set(domain_check.addresses))
    try:
        with ui.console.status("Asking Let's Encrypt for a certificate…"):
            certificates.obtain(config, site)
    except CtlError as problem:
        ui.warn(problem.message)
        ui.note(f"The site is up over HTTP. Fix the reason above and run: domainctl add {site.domain}")
        return
    openlitespeed.restart(config.ols_root)
    ui.success(f"https://{site.domain} is live with a Let's Encrypt certificate.")


def _certificate(config, site: Site, now: datetime) -> str:
    path = certificates.path(config, site.user)
    if not certificates.exists(config, site.user):
        return "none"
    if certbot.problem(path, site.names, now):
        return "problem"
    return f"until {certbot.read(path).expires:%Y-%m-%d}"


def _checks(session: Session, site: Site, now: datetime):
    config = session.config
    for name in site.names:
        found = dnsnames.check(session.resolver, name, session.server_ips)
        status = Status.OK if found.ready else (Status.WARN if found.state is State.UNKNOWN else Status.FAIL)
        yield Check(name, status, f"{name} points to this server." if found.ready else found.detail)
    wrong = layout.missing_permissions(config, site.user)
    if wrong:
        paths = ", ".join(str(path) for path in wrong)
        yield Check("Permissions", Status.FAIL,
                    f"{config.web_user} can't reach the site's files the way it should: {paths}. "
                    f"Correct it with: domainctl sync")
    else:
        yield Check("Permissions", Status.OK, f"{config.web_user} may read the site's files.")
    if not certificates.exists(config, site.user):
        yield Check("Certificate", Status.FAIL,
                    f"There is no certificate, so {site.domain} is only served over HTTP. "
                    f"Get one with: domainctl add {site.domain}")
        return
    invalid = certbot.problem(certificates.path(config, site.user), site.names, now)
    if invalid:
        yield Check("Certificate", Status.FAIL, f"{invalid} Renew it with: certbot renew")
    else:
        expires = certbot.read(certificates.path(config, site.user)).expires
        yield Check("Certificate", Status.OK, f"The certificate is valid until {expires:%Y-%m-%d}.")


def _addresses(session: Session) -> str:
    return ", ".join(str(ip) for ip in sorted(session.server_ips, key=lambda ip: (ip.version, ip)))
