"""mailctl certificate: the certificate Postfix and Dovecot present to mail programs."""

from datetime import datetime

from .. import ui
from ..core import dns_check, domains, mailcert, openlitespeed, system
from ..session import open_session
from .dns import DryRun
from .shared import Yes, group

app = group("Keep this server's mail certificate in step with its domains.")


@app.command()
def sync(dry_run: DryRun = False, yes: Yes = False) -> None:
    """Put this server's hostname and every mail.DOMAIN that points here in one certificate.

    Postfix and Dovecot serve that one certificate to everyone, so every name mail programs connect to has to be in
    it. Let's Encrypt checks each name over HTTP, which an OpenLiteSpeed site of mailctl's answers. Asks certbot for
    a certificate only when a name is missing from the one there, or it's about to expire; afterwards Postfix and
    Dovecot read it again. Every playbook run runs this too.

    [dim]Example:[/] mailctl certificate sync
    """
    now = datetime.now().astimezone()
    with open_session() as session:
        # On a server still being set up there is no listener yet: nothing to do, rather than a playbook run
        # stopped over a certificate that can't be asked for.
        if not mailcert.can_be_proved(session.config):
            ui.warn("OpenLiteSpeed has no listener on port 80 yet, where Let's Encrypt checks each name. "
                    "The certificate is left as it is; add the listener in WebAdmin and run this again.")
            return
        mail_domains = [domain.name for domain in domains.list_domains(session.db)]
        plan = mailcert.plan(session.config, mail_domains, system.server_ips(), dns_check.SystemResolver(), now)
        for line in plan.left_out:
            ui.note(f"Left out of the certificate: {line}")
        before, after = mailcert.planned_config(session.config, plan.names)
        elsewhere = mailcert.served_elsewhere(session.config, plan.names)
        if elsewhere:
            ui.warn(f"OpenLiteSpeed has a site of its own for {', '.join(elsewhere)}, which answers Let's Encrypt "
                    f"instead of mailctl's; the certificate may be refused for {'it' if len(elsewhere) == 1 else 'them'}.")
        if not plan.reason and before == after:
            ui.success(f"The certificate has {ui.plural(len(plan.names), 'name')}: {', '.join(plan.names)}.")
            return

        ui.line(f"The certificate of {session.config.hostname} is for:")
        for name in plan.names:
            ui.line(name, indent=2)
        if plan.reason:
            ui.note(plan.reason)
        if dry_run:
            ui.note("Nothing was changed (--dry-run).")
            return
        ui.confirm("Get that certificate?", yes)
        # The config is read again under the lock: nobody waits for the question, and another tool may have
        # changed it while it stood there.
        with openlitespeed.locked():
            before, after = mailcert.planned_config(session.config, plan.names)
            if before != after:
                openlitespeed.write(session.config.ols_root, after)
                openlitespeed.restart(session.config.ols_root)
                ui.success("OpenLiteSpeed answers Let's Encrypt for these names.")
        if not plan.reason:
            return
        with ui.console.status("Asking certbot for the certificate…"):
            mailcert.request(session.config, plan.names)
            mailcert.reload_mail_services()
    ui.success(f"Postfix and Dovecot serve a certificate for {ui.plural(len(plan.names), 'name')}.")
