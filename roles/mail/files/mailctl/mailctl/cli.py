"""The mailctl command."""

import typer
from typer.core import TyperGroup

from . import ui
from .commands import (
    address, autodiscover, certificate, dkim, dns, doctor, domain, filters, forward, repair, spam, status,
    transfer, webmail,
)
from .core.errors import MailctlError


class _MailctlGroup(TyperGroup):
    """Shows expected failures as a message and a hint instead of a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except MailctlError as problem:
            ui.error(problem)
            ctx.exit(1)


app = typer.Typer(
    cls=_MailctlGroup,
    name="mailctl",
    help="Manage the mail domains, addresses, forwards, DKIM keys, DNS records and spam settings of this server.",
    epilog="Run [bold]mailctl COMMAND --help[/] to see what a command does, with examples.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(domain.app, name="domain")
app.add_typer(address.app, name="address")
app.add_typer(forward.app, name="forward")
app.add_typer(dkim.app, name="dkim")
app.add_typer(dns.app, name="dns")
app.add_typer(autodiscover.app, name="autodiscover")
app.add_typer(certificate.app, name="certificate")
app.add_typer(spam.app, name="spam")
app.add_typer(filters.app, name="filters")
app.add_typer(webmail.app, name="webmail")
app.command()(status.status)
app.command()(doctor.doctor)
app.command()(repair.repair)
app.command(name="import")(transfer.import_server)
app.command()(transfer.export)
