"""mailctl dns: the DNS records a domain needs, and publishing them at TransIP."""

import sys
from dataclasses import dataclass, replace
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import autodiscover, dkim, dns_check, domains, system, transip, zone
from ..core.dns_check import DnsRecord, IPAddress
from ..core.errors import MailctlError
from ..session import Session, open_session
from .checks import offer_the_certificate
from .shared import Domain, Yes, ask_domain, group, recommended_records

app = group("Show a domain's DNS records and publish them at TransIP.")

DryRun = Annotated[bool, typer.Option("--dry-run", help="Only show the changes; change nothing.")]
Login = Annotated[Optional[str], typer.Option(
    "--login", help="The TransIP account name. Asked for when left out.", show_default=False)]
KeyStdin = Annotated[bool, typer.Option(
    "--key-stdin", help="Read the private key from standard input, for scripts. In a terminal it's asked for.")]


@app.command()
def show(domain: Domain = None) -> None:
    """Show the DNS records a domain needs for its mail on this server.

    [dim]Example:[/] mailctl dns show example.nl
    """
    with open_session() as session:
        domain = ask_domain(domain)
        domains.require(session.db, domain)
        problems: list[str] = []
        records = recommended_records(session, domain, problems)
        has_key = dkim.has_key(session.config, domain)
    ui.records(records)
    for problem in problems:
        ui.warn(problem)
    if not has_key:
        ui.note(f"{domain} has no DKIM key yet. Create it with: mailctl dkim create {domain}")


@app.command()
def publish(domain: Domain = None, dry_run: DryRun = False, yes: Yes = False) -> None:
    """Publish a domain's mail records in its DNS at TransIP.

    Adds the MX, mail host, SPF, DKIM, DMARC and SRV records, and removes the records they replace, like the MX
    record of another mail provider, and its autoconfig and autodiscover records (unless the domain has its own
    site for those; see 'mailctl autodiscover'). The other records, like the website's, stay. An SPF record keeps the
    senders it allows, and a DMARC record that's there stays. A domain like shop.example.nl goes into the zone of
    example.nl when it has none of its own. Shows the changes and asks before making them.

    [dim]Example:[/] mailctl dns publish example.nl --dry-run
    """
    with open_session() as session:
        domain = ask_domain(domain)
        domains.require(session.db, domain)
        change = read_zone(session, domain, _records_to_publish(session, domain), read_only=dry_run)
        if not change.plan.changes:
            ui.success(f"The mail records of {domain} at TransIP are already right.")
            return
        show_zone_change(change)
        if dry_run:
            ui.note("Nothing was changed (--dry-run).")
            return
        ui.confirm("Make these changes at TransIP?", yes)
        change = _one_expire_per_record_set(change, yes)
        save_zone_change(change)
        ui.success(f"Published the mail records of {domain} at TransIP.")
        note_removed_records(change)
        add_to_certificate = offer_the_certificate(session, domain, yes)
    if add_to_certificate:
        from . import certificate as certificate_command  # here: certificate.py takes DryRun from this module
        certificate_command.sync(yes=True)
    ui.note(f"Check the domain's setup with: mailctl doctor {domain}")


def _one_expire_per_record_set(change: "ZoneChange", yes: bool) -> "ZoneChange":
    """TransIP refuses a record set whose records don't all have the same expire, which happens when a record
    mailctl publishes lands next to one somebody else made with another one. Offers to make them the same."""
    mixed = zone.mixed_expires(change.plan.result)
    if not mixed:
        return change
    listed = ", ".join(f"{kind} {name}" for name, kind in mixed)
    ui.warn(f"TransIP wants one expire per record set, and these hold more than one: {listed}.")
    if not ui.decide(f"Set them all to {zone.SHORT_EXPIRE // 60} minutes?", True if yes else None, "--yes",
                     default=True):
        ui.note("TransIP will refuse the change; set their expire yourself in the control panel.")
        return change
    return replace(change, plan=replace(change.plan, result=zone.with_one_expire(change.plan.result)))


@dataclass(frozen=True)
class ZoneChange:
    client: transip.Client
    zone: str  # the domain's own zone at TransIP, or a parent domain's
    entries: list[zone.Entry]  # as read
    plan: zone.Plan


def read_zone(session: Session, domain: str, records: list[DnsRecord], *, read_only: bool) -> ZoneChange:
    """What publishing the records at TransIP changes. Asks for the TransIP login in a terminal when there is none."""
    client = transip.Client(transip_credentials(session), hostname=session.config.hostname, read_only=read_only)
    with ui.console.status("Reading the DNS records at TransIP…"):
        zone_name, entries = find_zone(client, domain)
        nameservers = client.nameservers(zone_name)
    if not transip.uses_transip_nameservers(nameservers):
        ui.warn(f"{zone_name} uses the nameservers {', '.join(nameservers) or '(none)'}, "
                f"so the internet doesn't see its DNS records at TransIP.")
    return ZoneChange(client, zone_name, entries,
                      zone.plan(domain, entries, records, zone=zone_name, sender_ips=system.server_ips()))


def show_zone_change(change: ZoneChange) -> None:
    ui.line(f"Changes to the DNS records of {change.zone} at TransIP:")
    for entry in change.plan.remove:
        ui.record_change(False, _record(change.zone, entry))
    for entry in change.plan.add:
        ui.record_change(True, _record(change.zone, entry))
    if change.plan.unchanged:
        records = "1 record is" if change.plan.unchanged == 1 else f"{change.plan.unchanged} records are"
        ui.note(f"{records} already right.", indent=2)


def save_zone_change(change: ZoneChange) -> None:
    with ui.console.status("Saving the DNS records at TransIP…"):
        # The whole zone is replaced, so a change made meanwhile, like in the control panel, would be lost.
        if change.client.dns_entries(change.zone) != change.entries:
            raise MailctlError(f"The DNS records of {change.zone} at TransIP changed in the meantime. Nothing was changed.",
                               hint="Run the command again to see the changes it makes now.")
        change.client.replace_dns_entries(change.zone, change.plan.result)


def note_removed_records(change: ZoneChange) -> None:
    if change.plan.remove:
        longest = max(entry.expire for entry in change.plan.remove)
        ui.note(f"Other servers may still use the removed records for up to {ui.duration(longest)}.")


def public_ips(host: str) -> set[IPAddress]:
    """The server's addresses to publish for a name."""
    ips = {ip for ip in system.server_ips() if ip.is_global}
    if not ips:
        raise MailctlError(f"This server has no public IP address to publish for {host}.")
    return ips


@app.command()
def credentials(login: Login = None, key_stdin: KeyStdin = False) -> None:
    """Enter the login and key mailctl uses for TransIP, or replace them.

    Make a key pair in TransIP's control panel, under the API settings of your account, and put this server's
    addresses on its whitelist. mailctl checks that TransIP accepts them before it saves them, readable only by root.
    'dns publish' asks for them the first time too.

    [dim]Example:[/] mailctl dns credentials
    [dim]In a script:[/] mailctl dns credentials --login myaccount --key-stdin < transip.key
    """
    with open_session() as session:
        _enter_credentials(session, login, key_stdin)


def transip_credentials(session: Session) -> transip.Credentials:
    """The saved TransIP login and key; in a terminal they're asked for when there are none yet."""
    saved = transip.saved_credentials(session.config)
    if saved:
        return saved
    if not ui.interactive():
        raise MailctlError("mailctl has no TransIP login and key yet.",
                           hint="Enter them with: mailctl dns credentials --login LOGIN --key-stdin < transip.key")
    ui.warn("mailctl has no TransIP login and key yet.")
    return _enter_credentials(session, None, False)


def _enter_credentials(session: Session, login: str | None, key_stdin: bool) -> transip.Credentials:
    if ui.interactive():
        _explain_key_pair()
    login = ui.ask("TransIP login", login, "--login").strip()
    if ui.interactive():
        key = ui.ask_secret_lines("Paste the private key (it isn't shown):", "-----END")
    elif key_stdin:
        key = sys.stdin.read()
    else:
        raise MailctlError("The private key is missing.",
                           hint="Pipe it in with --key-stdin, or run mailctl in a terminal to be asked.")
    with ui.console.status("Logging in to TransIP…"):
        saved = transip.save_credentials(session.config, login, key)
    ui.success(f"TransIP accepts the key. mailctl logs in as {login} from now on.")
    if saved.global_key:
        ui.note("The key isn't limited to the addresses on the whitelist, so mailctl asks for tokens that work anywhere.")
    return saved


def _explain_key_pair() -> None:
    try:
        ips = sorted(system.server_ips(), key=lambda ip: (ip.version, ip))
    except MailctlError:
        ips = []
    addresses = f": {', '.join(map(str, ips))}" if ips else ""
    ui.note("Make a key pair in TransIP's control panel, under the API settings of your account, and put this "
            f"server's addresses on its whitelist{addresses}.")


def find_zone(client: transip.Client, domain: str) -> tuple[str, list[zone.Entry]]:
    """The zone at TransIP that holds the domain's records: the domain's own, or else the nearest parent domain's."""
    labels = domain.split(".")
    for start in range(len(labels) - 1):
        name = ".".join(labels[start:])
        try:
            return name, client.dns_entries(name)
        except transip.NotInAccount:
            continue
    raise transip.NotInAccount(f"{domain} isn't in the TransIP account {client.login}.",
                               hint=f"See the records to publish by hand with: mailctl dns show {domain}")


def _records_to_publish(session: Session, domain: str) -> list[DnsRecord]:
    ips = public_ips(dns_check.mail_host(domain))
    if dkim.has_key(session.config, domain):
        dkim_value = dkim.record_value(session.config, domain)
    else:
        dkim_value = None
        ui.warn(f"{domain} has no DKIM key, so there's no DKIM record to publish. "
                f"Create the key with: mailctl dkim create {domain}")
    records = dns_check.recommended_records(domain, ips, dkim_value)
    try:
        has_site = autodiscover.has_site(session.config, domain)
    except MailctlError as problem:
        # Without knowing, publishing could remove the records of the domain's own autodiscover site.
        raise MailctlError(f"Can't tell whether {domain} has an autodiscover site: {problem.message}",
                           hint=problem.hint) from None
    if has_site:
        records += dns_check.autodetect_records(domain, ips)
    return records


def _record(zone_name: str, entry: zone.Entry) -> DnsRecord:
    return DnsRecord(entry.type, zone.absolute(zone_name, entry.name), entry.content)
