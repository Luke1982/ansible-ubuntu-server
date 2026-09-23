"""mailctl autodiscover: let Thunderbird (autoconfig) and Outlook (autodiscover) find a domain's mail settings."""

import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from .. import ui
from ..core import autodiscover, dns_check, domains, openlitespeed, system, transip
from ..core.dns_check import DnsRecord, IPAddress, LookupFailed
from ..core.errors import MailctlError
from ..session import Session, open_session
from .dns import (
    DryRun, ZoneChange, note_removed_records, public_ips, read_zone, save_zone_change, show_zone_change,
)
from .shared import Domain, Yes, ask_domain, group

app = group("Let Thunderbird and Outlook find a domain's mail settings (autoconfig and autodiscover).")


# How long records published a moment ago get to be answered, and how often that is tried.
RESOLVE_TIMEOUT = 120.0
RESOLVE_POLL = 5.0

NoDns = Annotated[bool, typer.Option(
    "--no-dns", help="Don't publish the DNS records at TransIP: show them, to publish by hand.")]


@app.command()
def publish(domain: Domain = None, dry_run: DryRun = False, yes: Yes = False, no_dns: NoDns = False) -> None:
    """Set up autoconfig and autodiscover for a domain, so Thunderbird and Outlook find its settings by themselves.

    Adds a site at autodiscover.DOMAIN and autoconfig.DOMAIN to OpenLiteSpeed, from mailctl's template, and publishes
    both names at TransIP. The site goes on HTTP first, since that is where Let's Encrypt checks a name; once both
    names point here, the command asks certbot for the certificate itself and switches the site to HTTPS. When a name
    doesn't point here yet, or Let's Encrypt refuses, it says so and how to do that step by hand.

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
    ui.note(f"No HTTPS yet: {https_problem}")
    if dry_run:
        return
    _certify(session, domain)


def _certify(session: Session, domain: str) -> None:
    """Gets the certificate for both names and puts the site on HTTPS. Outlook needs HTTPS, so this is the point of
    the command; when it can't be done yet, the certbot command is there to run by hand later."""
    config, (site, alias) = session.config, autodiscover.names(domain)
    try:
        addresses = _pointing_here(session, (site, alias))
        with ui.console.status(f"Asking certbot for a certificate for {site} and {alias}…"):
            worked_around = autodiscover.request_certificate(config, domain, addresses)
        for line in worked_around:
            ui.warn(line)
    except MailctlError as problem:
        ui.warn(problem.message)
        if problem.hint:
            ui.note(problem.hint)
        _by_hand(session, domain)
        return
    ui.success(f"Let's Encrypt gave a certificate for {site} and {alias}.")
    before, after = autodiscover.planned_config(config, domain, https=True)
    if before != after:
        _update_site(config.ols_root, before, after)
    ui.success(f"Mail programs find the settings at https://{site} and https://{alias}.")


def _pointing_here(session: Session, names: tuple[str, ...]) -> dict[str, set[IPAddress]]:
    """Where each name points, once every address is this server's. Records published a moment ago need a while to
    be answered everywhere, so this waits for them instead of failing on the run that published them."""
    resolver = dns_check.SystemResolver()
    server_ips = system.server_ips()
    found: dict[str, set[IPAddress]] = {}
    deadline = time.monotonic() + RESOLVE_TIMEOUT
    with ui.console.status(f"Waiting until {' and '.join(names)} point to this server…"):
        for name in names:
            while True:
                try:
                    found[name] = dns_check.resolve_to_this_server(resolver, name, server_ips)
                    break
                except (dns_check.NotPointingHere, LookupFailed) as problem:
                    if time.monotonic() >= deadline:
                        raise MailctlError(f"No certificate yet: {problem}") from None
                    time.sleep(RESOLVE_POLL)
    return found


def _by_hand(session: Session, domain: str) -> None:
    site, alias = autodiscover.names(domain)
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
    # Under the lock, so another tool can't save between the check and the write, which is what the check is for.
    with openlitespeed.locked():
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
