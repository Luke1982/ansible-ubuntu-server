"""mailctl webmail: the webmail sites at webmail.DOMAIN."""

import time
from typing import Annotated

import typer

from .. import ui
from ..core import dns_check, domains, names, system, transip, webmail, zone
from ..core.dns_check import IPAddress, Resolver
from ..core.errors import MailctlError
from ..core.webmail import Outcome, State
from ..session import Session, open_session
from .dns import ZoneChange, find_zone, save_zone_change, transip_credentials
from .shared import group

app = group("Keep the webmail sites at webmail.DOMAIN in step with the mail domains.")

# How long sync waits for TransIP's nameservers to serve a record it published, and how often it asks them.
PUBLISH_WAIT = 300  # seconds
PUBLISH_POLL = 10  # seconds

NoDns = Annotated[bool, typer.Option("--no-dns", help="Don't publish missing webmail records at TransIP.")]


@app.command()
def sync(no_dns: NoDns = False) -> None:
    """Set up webmail for every domain whose webmail.DOMAIN points to this server, and remove it for the others.

    A domain whose webmail.DOMAIN has no record at TransIP gets one, pointing here (records that are there are left
    alone). Once TransIP's nameservers serve it, the site gets a Let's Encrypt certificate of its own. Every playbook
    run runs this too; run it after changing a domain's webmail record.

    [dim]Example:[/] mailctl webmail sync
    """
    with open_session() as session:
        mail_domains = [domain.name for domain in domains.list_domains(session.db)]
        server_ips = system.server_ips()
        resolver: Resolver = dns_check.SystemResolver()
        if not no_dns:
            resolver = _WithPublished(resolver, _publish_missing_records(session, mail_domains, server_ips))
        with ui.console.status("Setting up the webmail sites…"):
            result = webmail.sync(session.config, mail_domains, server_ips, resolver)
    for outcome in result.outcomes:
        _show(outcome)
    # Ansible reads this line to tell whether anything changed.
    if result.changed:
        ui.success("Changed the webmail sites.")
    else:
        ui.note("Nothing changed.")


def _publish_missing_records(session: Session, mail_domains: list[str], server_ips: set[IPAddress]) -> dict:
    """Publishes webmail.DOMAIN at TransIP for the domains that have no record for it there, and waits until TransIP's
    nameservers serve them. Returns the names they serve, with their addresses."""
    ips = {ip for ip in server_ips if ip.is_global}
    if not ips:
        ui.warn("This server has no public address, so no webmail records are published.")
        return {}
    if not transip.saved_credentials(session.config) and not ui.interactive():
        ui.note("mailctl has no TransIP login, so it doesn't publish webmail records. "
                "Enter it with: mailctl dns credentials")
        return {}
    client = transip.Client(transip_credentials(session), hostname=session.config.hostname, read_only=False)
    published: dict[str, str] = {}  # name: its zone at TransIP
    for domain in sorted({domain.lower() for domain in mail_domains}):
        name = webmail.host(domain)
        if not names.valid_domain(name):
            continue
        try:
            zone_name, entries = find_zone(client, domain)
            relative = "@" if name == zone_name else name.removesuffix(f".{zone_name}")
            if any(entry.name.lower() == relative and entry.type in ("A", "AAAA", "CNAME") for entry in entries):
                continue
            added = tuple(zone.Entry(relative, zone.EXPIRE, "A" if ip.version == 4 else "AAAA", str(ip))
                          for ip in sorted(ips, key=lambda ip: (ip.version, ip)))
            save_zone_change(ZoneChange(client, zone_name, entries, zone.Plan((), added, 0, (*entries, *added))))
        except transip.NotInAccount:
            continue
        except transip.LoginRefused as problem:
            ui.warn(f"{problem.message} No webmail records are published.")
            return {}
        except MailctlError as problem:
            ui.warn(f"Couldn't publish {name} at TransIP: {problem.message}")
            continue
        ui.success(f"Published {name} at TransIP.")
        published[name] = zone_name
    return _served(published, ips)


def _served(published: dict[str, str], ips: set[IPAddress]) -> dict:
    """The published names once all their zone's nameservers serve them; Let's Encrypt may ask any of them."""
    served: dict = {}
    deadline = time.monotonic() + PUBLISH_WAIT
    waiting = dict(published)
    with ui.console.status("Waiting for TransIP's nameservers to serve the new records…"):
        while waiting:
            for name, zone_name in list(waiting.items()):
                try:
                    answers = transip.nameserver_addresses(zone_name, name)
                except MailctlError:
                    continue
                if answers and all(answer == ips for answer in answers):
                    served[name] = ips
                    del waiting[name]
            if not waiting or time.monotonic() >= deadline:
                break
            time.sleep(PUBLISH_POLL)
    for name in waiting:
        ui.warn(f"TransIP's nameservers don't serve {name} yet. Run mailctl webmail sync again in a few minutes.")
    return served


class _WithPublished:
    """The resolver, but knowing the records just published: a cache may still have their absence."""

    def __init__(self, resolver: Resolver, published: dict) -> None:
        self._resolver = resolver
        self._published = published

    def addresses(self, name: str) -> set[IPAddress]:
        return self._published.get(name) or self._resolver.addresses(name)

    def __getattr__(self, attribute: str):
        return getattr(self._resolver, attribute)


def _show(outcome: Outcome) -> None:
    url = f"https://{outcome.host}"
    match outcome.state:
        case State.LIVE:
            ui.success(url)
        case State.NEW:
            ui.success(f"{url} is live, with a new certificate.")
        case State.WAITING:
            ui.warn(f"No webmail for {outcome.domain} yet: {outcome.detail}")
        case State.FAILED:
            ui.warn(outcome.detail)
        case State.REMOVED:
            ui.success(f"Removed {url}: {outcome.detail}")
        case State.UNCHECKED:
            ui.warn(f"{outcome.detail} Left {url} as it was.")
