"""The certificate Postfix and Dovecot present: the hostname and every mail.DOMAIN of this server.

They serve one certificate to everyone, so it has to hold every name mail programs connect to. Let's Encrypt proves
each name over HTTP, so each one needs a site answering its challenges: mailctl keeps them as members of one
OpenLiteSpeed template, whose web root holds nothing but those challenges.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import certificate, openlitespeed, system
from .config import Config
from .dns_check import IPAddress, LookupFailed, Resolver, mail_host
from .errors import MailctlError
from .openlitespeed import Member, Template

TEMPLATE = "mailnames"
NOTE = "Managed by mailctl: Let's Encrypt challenges for the mail names"
# Certbot renews 30 days before the end; mailctl asks for a new certificate when a name is missing, not by age.
RENEW_BEFORE = timedelta(days=30)


@dataclass(frozen=True)
class Plan:
    names: list[str]  # the names the certificate should hold, the hostname first
    left_out: list[str]  # a line about each mail host that isn't one of them
    reason: str  # why a new certificate is needed, or "" when the one there will do


def plan(config: Config, domains: list[str], server_ips: set[IPAddress], resolver: Resolver, now: datetime) -> Plan:
    """The names for the certificate: the hostname, and the mail hosts that point to this server."""
    names, left_out = [config.hostname], []
    for domain in sorted(domains):
        host = mail_host(domain)
        try:
            addresses = resolver.addresses(host)
        except LookupFailed as failure:
            left_out.append(f"{host}: {failure}")
            continue
        if not addresses:
            left_out.append(f"{host} has no A or AAAA record yet.")
        elif not addresses & server_ips:
            left_out.append(f"{host} points to {', '.join(map(str, sorted(addresses, key=str)))}, not to this server.")
        else:
            names.append(host)
    return Plan(names, left_out, _reason(config, names, now))


def _reason(config: Config, names: list[str], now: datetime) -> str:
    path = config.certificate()
    try:
        found = certificate.read(path)
    except MailctlError as problem:
        return problem.message
    missing = [name for name in names if not certificate.covers(found, name)]
    if missing:
        return f"The certificate doesn't include {', '.join(missing)}."
    if found.expires - now <= RENEW_BEFORE:
        return f"The certificate expires on {found.expires:%Y-%m-%d}, which certbot's own renewal should have done."
    return ""


def planned_config(config: Config, names: list[str]) -> tuple[list[str], list[str]]:
    """The OpenLiteSpeed config now, and with a member for each name, so Let's Encrypt can reach their challenges."""
    path = config.ols_root / f"conf/templates/{TEMPLATE}.conf"
    if not path.exists():
        raise MailctlError(f"The OpenLiteSpeed template {path} is missing.",
                           hint="Run the Ansible playbook for this server; the mail role installs it.")
    lines = openlitespeed.read(config.ols_root)
    http, _ = openlitespeed.listeners(lines)
    if not http:
        raise MailctlError("OpenLiteSpeed has no HTTP listener on port 80, where Let's Encrypt checks a name.",
                           hint="Add one in WebAdmin.")
    template = Template(TEMPLATE, f"conf/templates/{TEMPLATE}.conf", tuple(http), NOTE)
    updated = lines
    for name in openlitespeed.members(lines, TEMPLATE):
        if name not in names:
            updated = openlitespeed.without_member(updated, name, TEMPLATE)
    for name in names:
        updated = openlitespeed.with_member(updated, Member(name, name), template)
    return lines, updated


def served_elsewhere(config: Config, names: list[str]) -> list[str]:
    """The names another site in OpenLiteSpeed already answers for, which mailctl leaves to it."""
    lines = openlitespeed.read(config.ols_root)
    ours = set(openlitespeed.members(lines, TEMPLATE))
    hosts = set(openlitespeed.virtual_hosts(lines))
    mapped = {name: host for listener in sum(openlitespeed.listeners(lines), [])
              for host, host_names in openlitespeed.maps(lines, listener).items() for name in host_names}
    return [name for name in names
            if name not in ours and (name in hosts or (name in mapped and mapped[name] not in ours))]


def request(config: Config, names: list[str]) -> None:
    """Asks certbot for a certificate with exactly these names, under the hostname's name."""
    domains = [argument for name in names for argument in ("-d", name)]
    try:
        certificate.run_certbot(config, "certonly", "--webroot", "--webroot-path", str(config.mailcert_root),
                                "--cert-name", config.hostname, "--expand", *domains)
    except MailctlError as problem:
        raise MailctlError(f"No certificate for the mail names: {certificate.reason(problem.message)}") from None


def reload_mail_services() -> None:
    """Postfix and Dovecot read the certificate again. Certbot's deploy hook does this too, for its own renewals."""
    system.run("doveadm", "reload")
    system.run("postfix", "reload")

