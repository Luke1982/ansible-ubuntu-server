"""Checks of the server's and a domain's setup, and how they're shown. Used by 'status' and 'doctor'."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .. import ui
from ..core import autodiscover, certificate, dkim, dns_check, reach, spamscan, system, webmail
from ..core.certificate import Certificate
from ..core.dns_check import Check, DnsRecord, IPAddress, Status
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
    resolver = dns_check.SystemResolver()
    knows = getattr(resolver, "knows_this_server", None)
    if knows:
        knows(ips)
    return Server(session.config.hostname, ips, found, problem, resolver)


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
    # Nothing about a server that stopped scanning is visible in its DNS or its certificate, so it is asked.
    checks.append(spamscan.check())
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
    reachable = attempt_check(lambda: reach.check(_ports_of(session, domain), facts.resolver,
                                                  advice=_how_to_fix(domain)))
    missing = _not_set_up(session, domain, checks, site, webmail_site)
    return checks + [found for found in (site, webmail_site, *missing, reachable) if found]


def _not_set_up(session: Session, domain: str, checks: list[Check], site: Check | None,
                webmail_site: Check | None) -> list[Check]:
    """Webmail and autodiscover a domain doesn't have yet. A domain whose mail isn't delivered here is left out:
    its mail is somewhere else, and so is the webmail of the people reading it."""
    here = next((check.status is Status.OK for check in checks if check.name == "MX"), False)
    if not here:
        return []
    found = []
    if not webmail_site:
        found.append(Check("Webmail", Status.WARN, f"{domain} has no webmail site. Give it one with: "
                                                   f"mailctl webmail sync"))
    if not site:
        found.append(Check("Autodiscover", Status.WARN,
                           f"{domain} has no autoconfig and autodiscover site, so mail programs don't find its "
                           f"settings by themselves. Set it up with: mailctl autodiscover publish {domain}"))
    return found


def _how_to_fix(domain: str) -> dict[str, str]:
    """Which command takes a record away, per name: mailctl publishes the mail names, and the domain itself is
    the website's, which is domainctl's on a server that has it."""
    mail_names = f"Take it away with: mailctl dns publish {domain}"
    return {name: mail_names for name in (dns_check.mail_host(domain), dns_check.webmail_host(domain),
                                          *autodiscover.names(domain))} | {
        domain: f"It is the website's record: take it away at TransIP, or with: domainctl repair {domain}"}


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


MAX_VALUE = 60  # characters of a record's value that are shown before it is cut off; a DKIM key is far longer


def show(checks: list[Check], everything: bool = True) -> bool:
    """Each check on a line, with the records that would solve its problem below it. Without everything, only the
    checks that need attention. Returns whether anything was shown."""
    shown = [check for check in checks if everything or check.status is not Status.OK]
    if not shown:
        return False
    width = max(len(check.name) for check in shown)
    for check in shown:
        ui.line(ui.mark(check.status), " ", ui.text(check.name.ljust(width), "bold"), "  ", check.detail, indent=2)
        ui.records([_short(record) for record in check.fixes] if not everything else check.fixes, indent=4)
    return True


def _short(record: DnsRecord) -> DnsRecord:
    """A record with a long value cut off: a DKIM key fills the screen, and dns show has it in full."""
    if len(record.value) <= MAX_VALUE:
        return record
    return DnsRecord(record.type, record.name, f"{record.value[:MAX_VALUE]}… (mailctl dns show has it in full)")


def offer_the_certificate(session: Session, domain: str, yes: bool = False) -> bool:
    """Whether to add the mail host to the certificate now. Asks when it points here already, and otherwise says
    what to run once it does. Nothing when the certificate can't be read; 'doctor' reports that."""
    found, _ = read_certificate(session)
    host = dns_check.mail_host(domain)
    if not found or certificate.covers(found, host):
        return False
    ui.warn(f"The certificate doesn't include {host} yet, so mail programs get a certificate warning.")
    points_here = attempt_check(lambda: Check("", Status.OK, "")
                                if dns_check.SystemResolver().addresses(host) & system.server_ips() else None)
    if points_here is None:
        ui.note(f"Add it once {host} points to this server with: mailctl certificate sync")
        return False
    return ui.decide(f"Put {host} in this server's certificate now?", True if yes else None, "--yes", default=True)
