"""mailctl domain: add, list and delete mail domains."""

from .. import ui
from ..core import addresses, dkim, dns_check, domains, forwards, mailbox
from ..session import Session, open_session
from .shared import DeleteMail, Domain, Yes, ask_domain, attempt, decide_mail, group, warn_about_incoming_forwards

app = group("Add, list and delete mail domains.")

SHOWN_ADDRESSES = 10


@app.command()
def add(domain: Domain = None) -> None:
    """Add a domain, create its DKIM key and show the DNS records to publish.

    [dim]Example:[/] mailctl domain add example.nl
    """
    with open_session() as session:
        create(session, ask_domain(domain))


def create(session: Session, domain: str) -> None:
    """Adds the domain, signs its mail and shows the records to publish. Also used by 'address add'."""
    domains.add(session.db, domain)
    problems: list[str] = []
    attempt(problems, "Couldn't create the DKIM key", lambda: dkim.create_key(session.config, domain))
    attempt(problems, "Couldn't update OpenDKIM", lambda: dkim.update_opendkim(session.config))
    has_key = dkim.has_key(session.config, domain)
    dkim_value = (
        attempt(problems, "Couldn't read the DKIM key", lambda: dkim.record_value(session.config, domain))
        if has_key else None
    )
    ui.success(f"Added {domain}. Publish these DNS records for it:")
    ui.records(dns_check.recommended_records(domain, session.config.hostname, dkim_value))
    for problem in problems:
        ui.warn(problem)
    if not has_key:
        ui.note(f"Create the DKIM key later with: mailctl dkim create {domain}")


@app.command(name="list")
def list_domains() -> None:
    """List the domains with their number of addresses and forwards, disk usage and DKIM key.

    [dim]Example:[/] mailctl domain list
    """
    with open_session() as session:
        rows = domains.list_domains(session.db)
        keys = set(dkim.key_domains(session.config))
        usage = {row.name: mailbox.disk_usage(mailbox.domain_dir(session.config, row.name)) for row in rows}
    if not rows:
        ui.note("There are no domains yet. Add one with: mailctl domain add")
        return
    table = ui.table("Domain", ("Addresses", "right"), ("Forwards", "right"), ("Disk usage", "right"), "DKIM key")
    for row in rows:
        ui.add_row(table, row.name, str(row.addresses), str(row.forwards), ui.size(usage[row.name]), ui.yes_no(row.name in keys))
    ui.console.print(table)


@app.command()
def delete(domain: Domain = None, delete_mail: DeleteMail = None, yes: Yes = False) -> None:
    """Delete a domain with its addresses, forwards and DKIM key.

    Asks you to type the domain to confirm, and whether to delete its stored mail too.

    [dim]Example:[/] mailctl domain delete example.nl --yes --keep-mail
    """
    with open_session() as session:
        domain = ask_domain(domain)
        forward_count = domains.get(session.db, domain).forwards
        accounts = addresses.list_addresses(session.db, domain)
        _announce(domain, accounts, forward_count)
        ui.confirm_by_typing(domain, f"Delete {domain}?", yes)
        mail = mailbox.domain_dir(session.config, domain)
        delete_mail = decide_mail(mail, delete_mail)
        incoming = forwards.to_domain(session.db, domain)

        with session.db.transaction():
            for account in accounts:
                addresses.delete(session.db, account)
            domains.delete(session.db, domain)
        for account in accounts:
            mailbox.kick(account)
        problems: list[str] = []
        if delete_mail:
            attempt(problems, "Couldn't delete the mail", lambda: mailbox.delete_mail(session.config, mail))
        attempt(problems, "Couldn't delete the DKIM key", lambda: dkim.delete_key(session.config, domain))
        attempt(problems, "Couldn't update OpenDKIM", lambda: dkim.update_opendkim(session.config))
    ui.success(f"Deleted {domain}.")
    if mail.exists() and not delete_mail:
        ui.note(f"Its mail is kept in {mail}.")
    for problem in problems:
        ui.warn(problem)
    warn_about_incoming_forwards(incoming)


def _announce(domain: str, accounts: list[str], forward_count: int) -> None:
    """Shows what goes along with the domain."""
    parts = []
    if accounts:
        parts.append(ui.plural(len(accounts), "address", "addresses"))
    if forward_count:
        parts.append(ui.plural(forward_count, "forward"))
    if not parts:
        return
    ui.warn(f"This also deletes {' and '.join(parts)} of {domain}.")
    for account in accounts[:SHOWN_ADDRESSES]:
        ui.note(account, indent=4)
    if len(accounts) > SHOWN_ADDRESSES:
        ui.note(f"and {len(accounts) - SHOWN_ADDRESSES} more", indent=4)
