"""mailctl import and export: a whole mail server in one JSON file, to move it to this one."""

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer

from .. import ui
from ..core import accountfile, addresses, dkim, domains, forwards, mailbox, senders, sieve, spam, transfer
from ..core.errors import MailctlError
from ..session import Session, open_session
from .dns import DryRun
from .shared import Yes, activate_imported, attempt, into_webmail, would_be_in_webmail

ServerFile = Annotated[Optional[str], typer.Argument(
    metavar="[FILE]", help="The JSON file of the mail server. Asked for when left out.", show_default=False)]
KeepHashes = Annotated[bool, typer.Option(
    "--keep-hashes", help="Keep the password hashes from the file without asking for new passwords.")]
NewPasswords = Annotated[bool, typer.Option(
    "--new-passwords", help=f"Ask for a new password for every account whose hash isn't {addresses.SCHEME}, "
    "which mailctl does anyway unless --keep-hashes.")]
Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Name everything that is skipped.")]


def import_server(file: ServerFile = None, dry_run: DryRun = False, keep_hashes: KeepHashes = False,
                  new_passwords: NewPasswords = False, verbose: Verbose = False, yes: Yes = False) -> None:
    """Create the domains, accounts, forwards, spam settings and filters in a JSON file, for moving a server.

    Write the file on the other server with [bold]mailctl export[/], or with export-mailserver.sh where there's no
    mailctl. Accounts have their password as the other server stores it, or in plain text:

    [dim]{"domains": ["example.nl"], "addresses": [{"address": "info@example.nl", "password_hash": "{SHA512-CRYPT}$6$..."}],
    "forwards": [{"source": "sales@example.nl", "destination": "info@example.nl", "send_as": true}],
    "spam": [{"target": "example.nl", "setting": "required_score", "value": "4"}],
    "sieve": [{"address": "info@example.nl", "name": "roundcube", "active": true, "content": "..."}]}[/]

    The file and its filters are checked whole before anything is created. Domains and accounts that are already on
    this server are skipped, with their spam settings and filters; so is a forward that's already there. Running the
    import again carries on where it stopped. Delete the file afterwards: it holds the passwords.

    For every account whose password hash isn't SHA512-CRYPT, like this server's, mailctl asks for a new password,
    and Enter keeps the hash; --keep-hashes doesn't ask. An account whose hash Dovecot here can't read at all needs
    a new password, or is skipped. Weak hashes, like MD5-CRYPT, are pointed out.

    [dim]Example:[/] mailctl import mailserver.json

    [dim]Check the file only:[/] mailctl import mailserver.json --dry-run
    """
    path = ui.ask_file("The JSON file of the mail server", file, "FILE")
    contents = transfer.read(path)
    if contents.accounts and accountfile.readable_by_others(path):
        ui.warn(f"{path} can be read by others, and it holds passwords. Delete it once the import is done.")
    with open_session() as session:
        plan = _Plan(session, contents, new_passwords, keep_hashes)
        if plan.empty():
            ui.success(f"Everything in {path} is already on this server.")
            return
        plan.report(verbose)
        problems = sieve.check([sieve.Script(script.name, script.content, script.active) for script in plan.filters])
        if problems:
            raise MailctlError(f"{ui.plural(len(problems), 'filter')} in {path} can't be read, so nothing was "
                               "imported:\n" + "\n".join(f"  {problem}" for problem in problems),
                               hint="Fix them in the file, or leave them out, and try again.")
        ui.line(f"To create from {path}: {plan.summary()}.")
        if dry_run:
            for address, scripts in plan.filters_per_account().items():
                ui.line(f"Filters of {address}:")
                would_be_in_webmail(scripts)
            ui.note("The file is fine. Nothing was changed (--dry-run).")
            return
        plan.require_passwords()
        ui.confirm("Import it?", yes)
        plan.ask_passwords()
        warnings = plan.carry_out()
    ui.success(f"Imported {plan.summary()} from {path}.")
    for warning in warnings:
        ui.warn(warning)
    for domain in plan.domains:
        ui.note(f"Publish the DNS records of {domain}; see them with: mailctl dns show {domain}")
    if plan.domains:
        ui.note("Once the new domains point to this server, give them webmail with: mailctl webmail sync")
    if contents.accounts:
        ui.note(f"Delete {path} now: it holds the passwords.")


def export(file: ServerFile = None, yes: Yes = False) -> None:
    """Write this server's domains, accounts, forwards, spam settings and filters to a JSON file.

    Import it on another server with [bold]mailctl import[/]. Accounts are written with their password hashes, so
    only root can read the file; delete it once it's imported.

    [dim]Example:[/] mailctl export mailserver.json
    """
    with open_session() as session:
        path = ui.ask_file("The file to write", file, "FILE")
        if path.exists():
            ui.confirm(f"{path} is already there. Overwrite it?", yes)
        data = transfer.collect(session.db, mailbox.sieve_scripts)
    _write_private(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    counts = [ui.plural(len(data["domains"]), "domain"), ui.plural(len(data["addresses"]), "account"),
              ui.plural(len(data["forwards"]), "forward"), ui.plural(len(data["spam"]), "spam setting"),
              ui.plural(len(data["sieve"]), "filter")]
    ui.success(f"Wrote {', '.join(counts)} to {path}.")
    ui.note(f"It holds the password hashes. Import it on the other server with: mailctl import {path.name}")


def _write_private(path: Path, content: str) -> None:
    """Writes the file so only its owner can read it, also when it was there already."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as problem:
        raise MailctlError(f"Can't write {path}: {problem.strerror or problem}.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)


class _Plan:
    """What the import creates: the file's contents less what's on this server already."""

    def __init__(self, session: Session, contents: transfer.Contents, new_passwords: bool, keep_hashes: bool) -> None:
        self.session = session
        db = session.db
        on_server = {domain.name for domain in domains.list_domains(db)}
        missing = sorted(contents.needed_domains() - on_server - set(contents.domains))
        if missing:
            raise MailctlError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} neither on this server "
                               "nor in the file.", hint='Add them to "domains" in the file, or first with: mailctl domain add')
        # What's here already, by what it is, to say in one line.
        self.here: dict[str, list[str]] = {"domain": [], "account": [], "forward": [], "server spam setting": []}
        self.domains = []
        for domain in contents.domains:
            (self.here["domain"] if domain in on_server else self.domains).append(domain)
        self.accounts = []
        for account in contents.accounts:
            if addresses.exists(db, account.address):
                self.here["account"].append(account.address)
            else:
                self.accounts.append(account)
        new_accounts = {account.address for account in self.accounts}
        self.forwards = []
        for forward in contents.forwards:
            if forwards.exists(db, forward.source, forward.destination):
                self.here["forward"].append(f"{forward.source} → {forward.destination}")
            else:
                self.forwards.append(forward)
        # Settings of what's skipped stay as they are here; the server's own are only added to.
        self.spam = []
        for value in contents.spam:
            if value.target in new_accounts or _domain_of(value.target) in self.domains:
                self.spam.append(value)
            elif value.target == spam.SERVER:
                if _new_for_server(db, value):
                    self.spam.append(value)
                else:
                    self.here["server spam setting"].append(f"{value.setting} {value.value}")
        self.filters = [script for script in contents.filters if script.address in new_accounts]
        self._sort_hashes(new_passwords, keep_hashes)

    def _sort_hashes(self, new_passwords: bool, keep_hashes: bool) -> None:
        """Finds the accounts to ask a new password for: every one whose hash isn't this server's scheme, unless
        --keep-hashes, and whatever happens the ones whose hash Dovecot here can't read at all."""
        schemes = {account.address: addresses.scheme_of(account.password_hash) for account in self.accounts
                   if account.password_hash and addresses.scheme_of(account.password_hash) != addresses.SCHEME}
        supported = addresses.supported_schemes() if schemes else set()
        self.unreadable = {address: scheme for address, scheme in schemes.items() if scheme not in supported}
        self.weak = {address: scheme for address, scheme in schemes.items()
                     if address not in self.unreadable and scheme not in addresses.STRONG_SCHEMES}
        self.to_ask = dict(self.unreadable)
        if not keep_hashes:
            # Every hash of another scheme: Dovecot reads it, but this server hashes passwords its own way, and
            # the way to get a hash that fits is the password itself. Enter keeps the hash that came with it.
            self.to_ask.update(schemes)
        self.new_passwords = new_passwords

    def report(self, verbose: bool) -> None:
        """Says in a line or two what is left alone, and which password hashes need attention."""
        here = [ui.plural(len(names), kind) for kind, names in self.here.items() if names]
        if here:
            ui.success(f"Already on this server, left as they are: {_and(here)}.")
            _details([name for names in self.here.values() for name in names], verbose)
        if self.unreadable:
            ui.warn(f"{_accounts(self.unreadable)} a password hash Dovecot here can't read "
                    f"({_schemes(self.unreadable)}): mailctl asks for new passwords, and Enter skips an account.")
            _details([f"{address} ({scheme})" for address, scheme in self.unreadable.items()], verbose)
        others = {address: scheme for address, scheme in self.to_ask.items() if address not in self.unreadable}
        if others:
            weak = " (one of them weak)" if self.weak else ""
            if ui.interactive():
                what = f"mailctl asks for new passwords{weak}, and Enter keeps a hash"
            else:
                what = f"they're kept{weak}; set new ones with: mailctl address password ADDRESS"
            ui.note(f"{_accounts(others)} a password hash of another scheme ({_schemes(others)}): {what}.")
            _details([f"{address} ({scheme})" for address, scheme in others.items()], verbose)

    def require_passwords(self) -> None:
        """Stops before anything is created when passwords are needed and there's no terminal to type them in."""
        if ui.interactive():
            return
        if self.unreadable:
            raise MailctlError(f"New passwords are needed for {ui.plural(len(self.unreadable), 'account')}, and "
                               "mailctl can't ask for them without a terminal.",
                               hint='Run mailctl import in a terminal, or give them a "password" in the file.')
        if self.new_passwords:
            raise MailctlError("--new-passwords asks for passwords, which needs a terminal.")

    def ask_passwords(self) -> None:
        """Asks for the new passwords. Enter keeps the hash, or skips an account whose hash Dovecot here can't
        read."""
        if not ui.interactive():
            return  # weak hashes are kept, as report() said
        for address, scheme in self.to_ask.items():
            unreadable = address in self.unreadable
            password = _ask_password(f"New password for {address} ({scheme} now; Enter "
                                     f"{'skips the account' if unreadable else 'keeps the hash'})")
            if password is not None:
                self.accounts = [transfer.Account(address, password=password) if account.address == address
                                 else account for account in self.accounts]
            elif unreadable:
                self._leave_out(address)
                ui.note(f"{address} is skipped, with its spam settings and filters.")

    def _leave_out(self, address: str) -> None:
        self.accounts = [account for account in self.accounts if account.address != address]
        self.spam = [value for value in self.spam if value.target != address]
        self.filters = [script for script in self.filters if script.address != address]

    def empty(self) -> bool:
        return not (self.domains or self.accounts or self.forwards or self.spam or self.filters)

    def summary(self) -> str:
        counts = [(self.domains, "domain"), (self.accounts, "account"), (self.forwards, "forward"),
                  (self.spam, "spam setting"), (self.filters, "filter")]
        return ", ".join(ui.plural(len(items), name) for items, name in counts if items)

    def carry_out(self) -> list[str]:
        """Creates everything, saying so as it goes. Returns what went wrong after a change was saved."""
        db, config = self.session.db, self.session.config
        problems: list[str] = []
        for domain in self.domains:
            domains.add(db, domain)
            attempt(problems, f"Couldn't create the DKIM key of {domain}", lambda: dkim.create_key(config, domain))
            ui.success(f"Added {domain}.")
        if self.domains:
            attempt(problems, "Couldn't update OpenDKIM", lambda: dkim.update_opendkim(config))
        for account in self.accounts:
            home = mailbox.home_dir(config, account.address)
            had_mail = home.exists()
            with db.transaction():
                if account.password is not None:
                    addresses.add(db, account.address, account.password)
                else:
                    addresses.add_hashed(db, account.address, account.password_hash)
                mailbox.create_maildir(config, account.address)
            ui.success(f"Created {account.address}." +
                       (f" The mail that was kept in {home} is in the account again." if had_mail else ""))
        for forward in self.forwards:
            may_send_as = forward.send_as and addresses.exists(db, forward.destination)
            with db.transaction():
                forwards.add(db, forward.source, forward.destination)
                if may_send_as:
                    senders.allow(db, forward.destination, forward.source)
            ui.success(f"{forward.source} now forwards to {forward.destination}"
                       f"{', which may send as it' if may_send_as else ''}.")
        for value in self.spam:
            self._set_spam(value)
        for address, scripts in self.filters_per_account().items():
            attempt(problems, f"Couldn't import the filters of {address}", lambda: self._put_filters(address, scripts))
        return problems

    def filters_per_account(self) -> dict[str, list[transfer.Filter]]:
        per_account: dict[str, list[transfer.Filter]] = {}
        for script in self.filters:
            per_account.setdefault(script.address, []).append(script)
        return per_account

    def _set_spam(self, value: transfer.SpamValue) -> None:
        scope = spam.scope_chain(value.target)[-1]
        with self.session.db.transaction():
            spam.set_value(self.session.db, scope, value.setting, value.value)
        ui.success(f"Set {value.setting} {value.value} for {scope.label}.")

    def _put_filters(self, address: str, scripts: list[transfer.Filter]) -> None:
        """The rules go into webmail's filters, where filters are edited. When none of them fits, the script that
        was active on the other server runs instead. The accounts are new, so no filter of theirs stops running."""
        for script in scripts:
            mailbox.put_sieve(address, script.name, script.content)
        ui.success(f"Imported {ui.plural(len(scripts), 'filter')} of {address}.")
        if not into_webmail(self.session, address, scripts):
            activate_imported(address, scripts, None)


def _ask_password(prompt: str) -> str | None:
    """A new password typed twice, or None when Enter is pressed."""
    while True:
        password = ui.ask_secret(prompt)
        if not password:
            return None
        try:
            addresses.check_password(password)
        except MailctlError as problem:
            ui.warn(problem.message)
            continue
        if ui.ask_secret("Password again") == password:
            return password
        ui.warn("The passwords don't match. Try again.")


def _new_for_server(db, value: transfer.SpamValue) -> bool:
    """Whether a setting for the whole server adds to this server's: a value it hasn't got, and for a setting with a
    single value, only when it isn't set here."""
    scope = spam.scope_chain(spam.SERVER)[0]
    if spam.is_set(db, scope, value.setting, value.value):
        return False
    single = value.setting in spam.SETTINGS and not spam.SETTINGS[value.setting].many
    return not (single and spam.count_values(db, scope, value.setting))


def _details(lines: list[str], verbose: bool) -> None:
    if verbose:
        for detail in lines:
            ui.note(detail, indent=2)


def _accounts(found: dict[str, str]) -> str:
    return f"{ui.plural(len(found), 'account')} {'has' if len(found) == 1 else 'have'}"


def _schemes(found: dict[str, str]) -> str:
    return ", ".join(sorted(set(found.values())))


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _domain_of(target: str) -> str:
    return target.rpartition("@")[2]
