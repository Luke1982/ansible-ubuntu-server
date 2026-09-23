"""Autoconfig (Thunderbird) and autodiscover (Outlook) for a domain.

A site at autodiscover.DOMAIN, with autoconfig.DOMAIN as alias, hands mail programs the settings for mail.DOMAIN.
It's a member of one of two OpenLiteSpeed templates that Ansible installs: one on the HTTPS listeners too, for a
domain with a certificate, and one on HTTP only, so certbot can get that certificate and no mail program sees
another site's certificate in the meantime.
"""

import shlex
from datetime import datetime
from pathlib import Path

from . import certificate, openlitespeed
from .config import Config
from .dns_check import Check, IPAddress, LookupFailed, Resolver, Status
from .errors import MailctlError
from .openlitespeed import Member, Template

TEMPLATE = "mailautodiscover"
WAITING_TEMPLATE = "mailautodiscover-http"
_NOTE = "Managed by mailctl: autoconfig and autodiscover for mail programs"


def names(domain: str) -> tuple[str, str]:
    """The site's name, which is also its certificate's name, and its alias."""
    return f"autodiscover.{domain}", f"autoconfig.{domain}"


def certificate_path(config: Config, domain: str) -> Path:
    return config.letsencrypt_dir / "live" / names(domain)[0] / "fullchain.pem"


def https_problem(config: Config, domain: str, now: datetime) -> str | None:
    """Why the site can't use HTTPS yet, or None when its certificate is valid for both names."""
    return certificate.problem(certificate_path(config, domain), names(domain), now)


SERVE_TIMEOUT = 10  # seconds OpenLiteSpeed gets to serve the site after a restart


def request_certificate(config: Config, domain: str, addresses: dict[str, set[IPAddress]]) -> list[str]:
    """Asks certbot for the certificate for both names, once OpenLiteSpeed really answers for them on port 80.
    A validation Let's Encrypt refuses counts against its limits, so what can be checked here is checked first.
    Returns a line about each address Let's Encrypt has to work around."""
    site, alias = names(domain)
    warnings = []
    for name in (site, alias):
        warnings += certificate.wait_until_served(
            config.autodiscover_root, name, addresses[name], timeout=SERVE_TIMEOUT,
            hint=f"Run: mailctl autodiscover publish {domain}, which adds the site to OpenLiteSpeed.")
    try:
        certificate.run_certbot(config, "certonly", "--webroot", "--webroot-path", str(config.autodiscover_root),
                                "--cert-name", site, "-d", site, "-d", alias)
    except MailctlError as problem:
        raise MailctlError(f"No certificate for {site} and {alias}: {certificate.reason(problem.message)}") from None
    return warnings


def certbot_command(config: Config, domain: str) -> str:
    site, alias = names(domain)
    return shlex.join(["certbot", "certonly", "--webroot", "-w", str(config.autodiscover_root),
                       "--cert-name", site, "-d", site, "-d", alias])


def state(config: Config, domain: str) -> str | None:
    """"https" or "http" for the template the domain's site is in; None when it has no site."""
    lines = openlitespeed.read(config.ols_root)
    site = names(domain)[0]
    if site in openlitespeed.members(lines, TEMPLATE):
        return "https"
    if site in openlitespeed.members(lines, WAITING_TEMPLATE):
        return "http"
    return None


def has_site(config: Config, domain: str) -> bool:
    """Whether the domain has a site. False without OpenLiteSpeed."""
    return openlitespeed.config_file(config.ols_root).exists() and state(config, domain) is not None


def planned_config(config: Config, domain: str, https: bool) -> tuple[list[str], list[str]]:
    """The OpenLiteSpeed config now, and with the domain's site in the template for HTTPS or for HTTP only."""
    for template in (TEMPLATE, WAITING_TEMPLATE):
        path = config.ols_root / _template_file(template)
        if not path.exists():
            raise MailctlError(f"The OpenLiteSpeed template {path} is missing.",
                               hint="Run the Ansible playbook for this server; the mail role installs it.")
    lines = openlitespeed.read(config.ols_root)
    http, secure = openlitespeed.listeners(lines)
    if not http:
        raise MailctlError("OpenLiteSpeed has no HTTP listener on port 80, which certbot and mail programs need.",
                           hint="Add one in WebAdmin.")
    if https and not secure:
        raise MailctlError("OpenLiteSpeed has no HTTPS listener on port 443.", hint="Add one in WebAdmin.")
    templates = {
        TEMPLATE: Template(TEMPLATE, _template_file(TEMPLATE), (*http, *secure), _NOTE),
        WAITING_TEMPLATE: Template(WAITING_TEMPLATE, _template_file(WAITING_TEMPLATE), tuple(http),
                                   f"{_NOTE}, on HTTP until they have a certificate"),
    }
    target = TEMPLATE if https else WAITING_TEMPLATE
    others = tuple(template for name, template in templates.items() if name != target)
    site, alias = names(domain)
    return lines, openlitespeed.with_member(lines, Member(site, site, (alias,)), templates[target], others)


def remove(config: Config, domain: str) -> bool:
    """Takes the domain's site out of OpenLiteSpeed. Returns whether it had one. Its certificate stays: certbot
    deletes that."""
    if not has_site(config, domain):
        return False
    site = names(domain)[0]
    with openlitespeed.locked():
        lines = openlitespeed.read(config.ols_root)
        updated = lines
        for template in (TEMPLATE, WAITING_TEMPLATE):
            updated = openlitespeed.without_member(updated, site, template)
        if updated != lines:
            openlitespeed.write(config.ols_root, updated)
            openlitespeed.restart(config.ols_root)
    return True


def check(config: Config, domain: str, server_ips: set[IPAddress], resolver: Resolver, now: datetime) -> Check | None:
    """How the domain's site is doing, or None when it has none."""
    site, alias = names(domain)
    if not has_site(config, domain):
        return None
    try:
        elsewhere = [name for name in (site, alias) if not resolver.addresses(name) & server_ips]
    except LookupFailed as failure:
        return Check("Autodiscover", Status.WARN, str(failure))
    if elsewhere:
        detail = (f"{', '.join(elsewhere)} doesn't point to this server, so mail programs don't find the settings "
                  f"here. Publish it with: mailctl autodiscover publish {domain}")
        return Check("Autodiscover", Status.WARN, detail)
    problem = https_problem(config, domain, now)
    if problem:
        return Check("Autodiscover", Status.WARN, f"{site} has no HTTPS yet, which Outlook needs: {problem} "
                                                  f"Get it with: {certbot_command(config, domain)}")
    return Check("Autodiscover", Status.OK, f"Mail programs find the settings at https://{site}.")


def _template_file(template: str) -> str:
    return f"conf/templates/{template}.conf"
