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
    path = certificate_path(config, domain)
    try:
        found = certificate.read(path)
    except MailctlError as problem:
        return problem.message
    if found.expires <= now:
        return f"The certificate at {path} expired on {found.expires:%Y-%m-%d}."
    missing = [name for name in names(domain) if not certificate.covers(found, name)]
    if missing:
        return f"The certificate at {path} doesn't include {', '.join(missing)}."
    return None


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


def _template_file(template: str) -> str:
    return f"conf/templates/{template}.conf"
