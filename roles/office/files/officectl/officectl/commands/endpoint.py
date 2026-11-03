"""officectl endpoint: Collabora's own site, the one every Nextcloud on this server opens documents through."""

from datetime import UTC, datetime

from serverctl import openlitespeed, serving, ui
from serverctl.errors import CtlError

from ..core import collabora, endpoint as site, php, tenants
from ..session import open_session
from .shared import NoDns, Yes, addresses, point_here


def endpoint(no_dns: NoDns = False, yes: Yes = False) -> None:
    """Set Collabora's own site up, or repair it: its settings, its certificate and its DNS record.

    Collabora itself answers on this server only, without encryption; this site stands in front of it with the
    certificate and hands everything through, the websocket a document is edited over included. Nextcloud talks
    to it over the internet, so it needs a name that points here and a certificate no browser or Nextcloud has
    to be told to trust.

    Run it again whenever you like: it only publishes a record that is missing, and only asks for a certificate
    when there is none, it doesn't cover the name, or it is about to expire.

    [dim]Example:[/] officectl endpoint
    """
    with open_session() as session:
        config = session.config
        if not collabora.installed(config):
            raise CtlError("Collabora Online isn't installed on this server.",
                           hint="Run the Ansible playbook for this server: the office role installs it.")
        found = point_here(session, config.office_host, no_dns, yes)
        if not found.ready:
            raise CtlError(f"{found.detail} Let's Encrypt checks the same thing, so there is no certificate.",
                           hint=f"Point {config.office_host} at {addresses(session)} and run this again.")
        ui.success(f"{config.office_host} points to this server.")
        hosts = [tenant.host for tenant in tenants.list_tenants(config)]
        if collabora.configure(config, hosts):
            collabora.restart(config)
            ui.success(f"Collabora reads its settings again, and lets in {ui.plural(len(hosts), 'Nextcloud')}.")
        if site.publish(config, site.certified(config)):
            openlitespeed.restart(config.ols_root)
            ui.success(f"OpenLiteSpeed serves {config.office_host}.")
        reason = site.needs_certificate(config, datetime.now(UTC))
        if not reason:
            ui.success(f"{config.office_host} has its certificate already.")
            _say_where(config)
            return
        ui.note(reason)
        with ui.console.status("Checking that OpenLiteSpeed serves the site…"):
            unanswered = serving.wait_until_served(site.webroot(config), config.office_host,
                                                   set(found.addresses))
        for line in unanswered:
            ui.note(line)
        with ui.console.status("Asking Let's Encrypt for a certificate…"):
            site.obtain(config)
        if site.publish(config, True):
            openlitespeed.restart(config.ols_root)
        ui.success(f"https://{config.office_host} is live with a Let's Encrypt certificate.")
        _say_where(config)


def _say_where(config) -> None:
    if not php.installed(config):
        ui.warn(f"{config.lsphp()} isn't there, so there is no PHP to run a Nextcloud on. Run the Ansible "
                f"playbook for this server.")
        return
    ui.note(f"Add a Nextcloud that uses it with: officectl add NAME (it is served at NAME.{config.office_host})")
