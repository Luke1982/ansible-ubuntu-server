"""The domainctl command."""

import typer
from typer.core import TyperGroup

from serverctl import ui
from serverctl.errors import CtlError

from .commands import dns, site, wordpress


class _DomainctlGroup(TyperGroup):
    """Shows expected failures as a message and a hint instead of a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CtlError as problem:
            ui.error(problem)
            ctx.exit(1)


app = typer.Typer(
    cls=_DomainctlGroup,
    name="domainctl",
    help="Manage the web sites of this server: their Linux user, files, virtual host and certificate.",
    epilog="Run [bold]domainctl COMMAND --help[/] to see what a command does, with examples.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(dns.app, name="dns")
app.add_typer(wordpress.app, name="wp")
app.command()(site.add)
app.command()(site.repair)
app.command(name="list")(site.list_sites)
app.command()(site.delete)
app.command()(site.doctor)
# The name it had before both tools used the same words for the same thing.
app.command(name="check", hidden=True)(site.doctor)
app.command()(site.sync)
