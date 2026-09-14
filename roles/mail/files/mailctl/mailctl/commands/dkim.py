"""mailctl dkim: DKIM keys and their DNS records."""

from .. import ui
from ..core import dkim, dns_check
from ..session import open_session
from .shared import Domain, ask_domain, group

app = group("Create DKIM keys and show their DNS records.")


@app.command()
def show(domain: Domain = None) -> None:
    """Show the DKIM record to publish in the domain's DNS.

    [dim]Example:[/] mailctl dkim show example.nl
    """
    with open_session() as session:
        domain = ask_domain(domain)
        record = dns_check.dkim_record(domain, dkim.record_value(session.config, domain))
    ui.records([record])


@app.command()
def create(domain: Domain = None) -> None:
    """Create a DKIM key for a domain that has none, and sign the domain's mail with it.

    An existing key is never replaced. OpenDKIM's tables are brought up to date either way, and Ansible runs
    this for the server's own domain.

    [dim]Example:[/] mailctl dkim create example.nl
    """
    with open_session() as session:
        domain = ask_domain(domain)
        created = dkim.create_key(session.config, domain)
        if created:
            ui.success(f"Created a DKIM key for {domain}. Publish this DNS record for it:")
            ui.records([dns_check.dkim_record(domain, dkim.record_value(session.config, domain))])
        else:
            ui.note(f"{domain} already has a DKIM key.")
        # A failed reload ends the command with an error, so Ansible stops; the next run tries again.
        if dkim.update_opendkim(session.config) and not created:
            ui.success("Updated the OpenDKIM tables.")
