"""mailctl autodiscover: let Thunderbird (autoconfig) and Outlook (autodiscover) find a domain's mail settings."""

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from .. import ui
from ..core import autodiscover, dns_check, domains, openlitespeed, transip
from ..core.dns_check import DnsRecord
from ..core.errors import MailctlError
from ..session import Session, open_session
from .dns import (
    DryRun, ZoneChange, note_removed_records, public_ips, read_zone, save_zone_change, show_zone_change,
)
from .shared import Domain, Yes, ask_domain, group

app = group("Let Thunderbird and Outlook find a domain's mail settings (autoconfig and autodiscover).")


NoDns = Annotated[bool, typer.Option(
    "--no-dns", help="Don't publish the DNS records at TransIP: show them, to publish by hand.")]


@app.command()
def publish(domain: Domain = None, dry_run: DryRun = False, yes: Yes = False, no_dns: NoDns = False) -> None:
    """Set up autoconfig and autodiscover for a domain, so Thunderbird and Outlook find its settings by themselves.

    Adds a site at autodiscover.DOMAIN and autoconfig.DOMAIN to OpenLiteSpeed, from mailctl's template, and publishes
    both names at TransIP. Checks for HTTPS first: with a certificate for both names the site answers on HTTP and
    HTTPS; without one it answers on HTTP only, and the command shows how to get the certificate with certbot. Run it
    again once certbot has the certificate, to switch HTTPS on.

    [dim]Example:[/] mailctl autodiscover publish example.nl
    """
    now = datetime.now().astimezone()
    with open_session() as session:
        domain = ask_domain(domain)
        domains.require(session.db, domain)
        site, alias = autodiscover.names(domain)
        https_problem = autodiscover.https_problem(session.config, domain, now)
        before, after = autodiscover.planned_config(session.config, domain, https=https_problem is None)
        dns = _dns_change(session, domain, dns_check.autodetect_records(domain, public_ips(site)), dry_run, no_dns)
        site_changes = before != after
        dns_changes = dns is not None and dns.plan.changes

        if site_changes:
            where = "HTTP and HTTPS" if https_problem is None else "HTTP only, until it has a certificate"
            ui.line(f"The OpenLiteSpeed site {site} (also {alias}) goes on {where}.")
        if dns_changes:
            show_zone_change(dns)
        if not site_changes and not dns_changes:
            ui.success(f"Autoconfig and autodiscover for {domain} are already set up.")
        elif dry_run:
            ui.note("Nothing was changed (--dry-run).")
            return
        else:
            ui.confirm("Make these changes?", yes)
            if site_changes:
                _update_site(session.config.ols_root, before, after)
                ui.success(f"OpenLiteSpeed serves {site} and {alias}.")
            if dns_changes:
                save_zone_change(dns)
                ui.success(f"Published {site} and {alias} at TransIP.")
                note_removed_records(dns)

    if https_problem is None:
        ui.success(f"Mail programs find the settings at https://{site} and https://{alias}.")
        return
    ui.warn(f"No HTTPS yet: {https_problem}")
    ui.line(f"Certbot's web root for {site} and {alias} is {session.config.autodiscover_root}.")
    ui.note("Once both names point to this server, get the certificate with:")
    ui.line(autodiscover.certbot_command(session.config, domain), indent=2)
    ui.note(f"Then run this again to switch HTTPS on: mailctl autodiscover publish {domain}")


def _dns_change(
    session: Session, domain: str, records: list[DnsRecord], dry_run: bool, no_dns: bool
) -> ZoneChange | None:
    """The changes at TransIP, or None after showing the records to publish by hand."""
    if not no_dns:
        try:
            return read_zone(session, domain, records, read_only=dry_run)
        except transip.NotInAccount as problem:
            ui.warn(problem.message)
    ui.note("Publish these records by hand:")
    ui.records(records)
    return None


def _update_site(root: Path, before: list[str], after: list[str]) -> None:
    if openlitespeed.read(root) != before:
        raise MailctlError("OpenLiteSpeed's config changed in the meantime. Nothing was changed.",
                           hint="Run the command again to see the changes it makes now.")
    openlitespeed.write(root, after)
    try:
        openlitespeed.restart(root)
    except MailctlError:
        # The old config goes back, so the next run sees the change again and tries again.
        openlitespeed.write(root, before)
        raise
