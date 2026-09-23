"""domainctl dns: the TransIP login domainctl uses to publish a missing record."""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from serverctl import system, transip, ui
from serverctl.errors import CtlError

from ..session import Session, open_session
from .shared import group

app = group("The TransIP login used to publish a site's DNS records.")

Login = Annotated[Optional[str], typer.Option(
    "--login", help="The TransIP account name. Asked for when left out.", show_default=False)]
KeyStdin = Annotated[bool, typer.Option(
    "--key-stdin", help="Read the private key from standard input, for scripts. In a terminal it's asked for.")]


@app.command()
def credentials(login: Login = None, key_stdin: KeyStdin = False) -> None:
    """Enter the TransIP login and key, or replace them.

    Make a key pair in TransIP's control panel, under the API settings of your account, and put this server's
    addresses on its whitelist. The key is checked with TransIP before it is saved, readable only by root.
    mailctl on this server uses the same two files, so this is entered once per server.

    [dim]Example:[/] domainctl dns credentials
    [dim]In a script:[/] domainctl dns credentials --login myaccount --key-stdin < transip.key
    """
    with open_session() as session:
        enter(session, login, key_stdin)


# Where mailctl kept the login before both tools shared one place. Until mailctl reads the shared files too,
# "mailctl dns credentials" writes here and domainctl doesn't see it, so a replaced key would look like a
# refused login with no reason. This check goes away when mailctl moves over.
MAILCTL_TRANSIP = Path("/etc/mailctl")


def saved_or_asked(session: Session) -> transip.Credentials:
    """The saved TransIP login and key; in a terminal they're asked for when there are none yet."""
    saved = transip.saved_credentials(session.config.transip_access())
    if saved:
        _warn_if_mailctl_has_another(session)
        return saved
    if not ui.interactive():
        raise CtlError("There is no TransIP login and key on this server yet.",
                       hint="Enter them with: domainctl dns credentials --login LOGIN --key-stdin < transip.key")
    ui.warn("There is no TransIP login and key on this server yet.")
    return enter(session, None, False)


def client(session: Session, read_only: bool) -> transip.Client:
    return transip.Client(saved_or_asked(session), access=session.config.transip_access(), read_only=read_only)


def enter(session: Session, login: str | None, key_stdin: bool) -> transip.Credentials:
    access = session.config.transip_access()
    if ui.interactive():
        _explain_key_pair()
    login = ui.ask("TransIP login", login, "--login").strip()
    if ui.interactive():
        key = ui.ask_secret_lines("Paste the private key (it isn't shown):", "-----END")
    elif key_stdin:
        key = sys.stdin.read()
    else:
        raise CtlError("The private key is missing.",
                       hint="Pipe it in with --key-stdin, or run domainctl in a terminal to be asked.")
    with ui.console.status("Logging in to TransIP…"):
        saved = transip.save_credentials(access, login, key)
    ui.success(f"TransIP accepts the key. domainctl logs in as {login} from now on.")
    if saved.global_key:
        ui.note("The key isn't limited to the addresses on the whitelist, so tokens that work anywhere are used.")
    return saved


def _warn_if_mailctl_has_another(session: Session) -> None:
    """Says so when mailctl holds a different key, which would otherwise show up as an unexplained refusal."""
    access = session.config.transip_access()
    for ours, theirs in ((access.key, MAILCTL_TRANSIP / "transip.key"),
                         (access.settings, MAILCTL_TRANSIP / "transip.json")):
        try:
            if theirs.exists() and theirs.read_bytes() != ours.read_bytes():
                ui.warn(f"mailctl has a different TransIP login in {theirs}. If this one is refused, copy "
                        f"mailctl's over {ours} or enter it again with: domainctl dns credentials")
                return
        except OSError:
            return  # unreadable is not something to warn about; a refused login says enough


def _explain_key_pair() -> None:
    try:
        ips = sorted(system.server_ips(), key=lambda ip: (ip.version, ip))
    except CtlError:
        ips = []
    ui.note("Make a key pair in TransIP's control panel, under the API settings of your account.")
    if ips:
        ui.note(f"Put this server's addresses on its whitelist: {', '.join(map(str, ips))}.")
