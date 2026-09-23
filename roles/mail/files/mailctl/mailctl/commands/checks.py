"""Checks of the server's and a domain's setup, and how they're shown. Used by 'status' and 'doctor'."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .. import ui
from ..core import autodiscover, certificate, dkim, dns_check, reach, system, webmail
from ..core.certificate import Certificate
from ..core.dns_check import Check, IPAddress, Status
from ..core.errors import MailctlError
from ..session import Session


@dataclass(frozen=True)
class Server:
    """What the checks need to know about this server, looked up once."""
    hostname: str
    ips: set[IPAddress]
    certificate: Certificate | None
    certificate_problem: str  # why there's no certificate
    resolver: dns_check.Resolver


def server(session: Session) -> Server:
    """Fails when this server's addresses can't be read, since no check can do without them."""
    ips = system.server_ips()
    found, problem = read_certificate(session)
    return Server(session.config.hostname, ips, found, problem, dns_check.SystemResolver())


def read_certificate(session: Session) -> tuple[Certificate | None, str]:
    """The certificate, or else None and why."""
    try:
        return certificate.read(session.config.certificate()), ""
    except MailctlError as problem:
        return None, problem.message


def server_checks(facts: Server, now: datetime) -> list[Check]:
    checks = dns_check.check_server(facts.hostname, server_ips=facts.ips, resolver=facts.resolver)
    if facts.certificate:
        checks.append(certificate.check_server(facts.certificate, facts.hostname, now))
    else:
        checks.append(Check("Certificate", Status.FAIL, facts.certificate_problem))
    # Where other mail servers deliver: a hostname address that doesn't answer there delays every message.
    reachable = reach.check({facts.hostname: reach.SMTP_PORT}, facts.resolver)
    return checks + ([reachable] if reachable else [])


def domain_checks(session: Session, facts: Server, domain: str, now: datetime | None = None) -> list[Check]:
    dkim_value = None
    if dkim.has_key(session.config, domain):
        try:
            dkim_value = dkim.record_value(session.config, domain)
        except MailctlError as problem:
            ui.warn(f"Couldn't read the DKIM key: {problem.message}", indent=2)
    checks = dns_check.check_domain(domain, server_ips=facts.ips, dkim_value=dkim_value, resolver=facts.resolver)
    if facts.certificate:
        checks.append(certificate.check_domain(facts.certificate, domain))
    else:
        checks.append(Check("Certificate", Status.WARN, f"Can't check the certificate: {facts.certificate_problem}"))
    moment = now or datetime.now().astimezone()
    site = attempt_check(lambda: autodiscover.check(session.config, domain, facts.ips, facts.resolver, moment))
    webmail_site = attempt_check(lambda: webmail.check(session.config, domain, facts.ips, facts.resolver, moment))
    reachable = attempt_check(lambda: reach.check(_ports_of(session, domain), facts.resolver))
    return checks + [found for found in (site, webmail_site, reachable) if found]


def _has_webmail(session: Session, domain: str) -> bool:
    """Whether the domain has a webmail site. False when OpenLiteSpeed's config can't be read, which the webmail
    check itself reports: the mail host is worth looking at either way."""
    try:
        return webmail.host(domain) in webmail.sites(session.config)
    except MailctlError:
        return False


def _ports_of(session: Session, domain: str) -> dict[str, int]:
    """The names a domain's mail depends on, with the port each one is used for."""
    names = {dns_check.mail_host(domain): reach.IMAP_PORT}
    if _has_webmail(session, domain):
        names[webmail.host(domain)] = reach.HTTPS_PORT
    if autodiscover.has_site(session.config, domain):
        site, alias = autodiscover.names(domain)
        # Outlook asks the domain itself before it asks these, so an address there that doesn't answer stops it
        # before it ever reaches the site.
        names.update({site: reach.HTTPS_PORT, alias: reach.HTTPS_PORT, domain: reach.HTTPS_PORT})
    return names


def attempt_check(check: Callable[[], Check | None]) -> Check | None:
    """A check that can't be made, because OpenLiteSpeed's config can't be read, is left out."""
    try:
        return check()
    except MailctlError:
        return None


def show(checks: list[Check]) -> None:
    """Each check on a line, with the records that would solve its problem below it."""
    width = max(len(check.name) for check in checks)
    for check in checks:
        ui.line(ui.mark(check.status), " ", ui.text(check.name.ljust(width), "bold"), "  ", check.detail, indent=2)
        ui.records(check.fixes, indent=4)


def warn_if_not_in_certificate(session: Session, domain: str) -> None:
    """A reminder to add the mail host to the certificate, once it points here. Nothing when the certificate can't
    be read; 'doctor' reports that."""
    found, _ = read_certificate(session)
    host = dns_check.mail_host(domain)
    if found and not certificate.covers(found, host):
        ui.warn(f"The certificate doesn't include {host} yet. Add it once {host} points to this server, "
                f"or mail programs get a certificate warning.")
