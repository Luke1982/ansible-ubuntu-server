"""officectl sync: writes everything officectl owns again, and says whether that changed anything.

The playbook runs this. It never adds or removes a Nextcloud, never downloads anything and never asks anything:
it writes the files these commands write -- Collabora's settings, the virtual hosts, the PHP settings, the
background jobs -- and puts the file permissions right. So a setting that was changed by hand goes back to what
officectl keeps, and a server that was set up before a change to this repository gets it.

Nextcloud's own settings are left alone here. Changing them means running occ against a live Nextcloud, which
is "officectl add"'s business, not a playbook run's.
"""

from serverctl import openlitespeed, ui
from serverctl.errors import CtlError

from ..core import collabora, endpoint, layout, nextcloud, php, tenants
from ..session import open_session


def sync() -> None:
    """Bring Collabora's settings, the sites, the PHP settings, the background jobs and the file permissions up
    to date. Run by the playbook.

    [dim]Example:[/] officectl sync
    """
    with open_session() as session:
        config = session.config
        found = tenants.list_tenants(config)
        changed = _collabora(config, found)
        served, php_changed, written = _sites(config, found)
        changed = changed or served or php_changed or written
        if served:
            openlitespeed.restart(config.ols_root)
            ui.success("OpenLiteSpeed reads its configuration again.")
        if php_changed:
            stopped = php.stop_pools(config)
            ui.success(f"Stopped {ui.plural(stopped, 'PHP process', 'PHP processes')}: the next request reads "
                       f"the new settings.")
        # Ansible reads this line to tell whether anything changed.
        if changed:
            ui.success("Changed Collabora and the Nextclouds.")
        else:
            ui.note("Nothing changed.")


def _collabora(config, found) -> bool:
    """Collabora's own settings, with the Nextclouds that may use it. Returns whether anything changed."""
    if not collabora.installed(config):
        ui.warn("Collabora Online isn't installed on this server, so its settings are left alone.")
        return False
    try:
        if not collabora.configure(config, [tenant.host for tenant in found]):
            return False
    except CtlError as problem:
        _say(problem)
        return False
    collabora.restart(config)
    ui.success(f"Collabora reads its settings again, and lets in "
               f"{ui.plural(len(found), 'Nextcloud')} on this server.")
    return True


def _sites(config, found) -> tuple[bool, bool, bool]:
    """The virtual hosts, the PHP settings, the background jobs and the permissions of Collabora's own site and
    of every Nextcloud.

    Returns whether OpenLiteSpeed has to read its configuration again, whether PHP has to read its settings
    again, and whether anything else was written.
    """
    served = php_changed = written = False
    try:
        if endpoint.publish(config, endpoint.certified(config)):
            ui.success(f"Brought the site of {config.office_host} up to date.")
            served = True
    except CtlError as problem:
        # A server whose listeners aren't set up yet still gets everything else in order.
        _say(problem)
    for tenant in found:
        try:
            if php.write_settings(config, tenant.user, tenant.host):
                ui.success(f"Brought the PHP settings of {tenant.slug} up to date.")
                php_changed = True
            if tenants.save(config, tenant, tenants.certified(config, tenant)):
                ui.success(f"Brought the site of {tenant.host} up to date.")
                served = True
            if nextcloud.write_cron(config, tenant):
                ui.success(f"Brought the background jobs of {tenant.slug} up to date.")
                written = True
            if layout.apply_permissions(config, tenant.user):
                ui.success(f"Corrected the file permissions of {tenant.slug}.")
                written = True
        except CtlError as problem:
            _say(problem)
    return served, php_changed, written


def _say(problem: CtlError) -> None:
    ui.warn(problem.message)
    if problem.hint:
        ui.note(problem.hint)
