"""The officectl command."""

import typer
from typer.core import TyperGroup

from serverctl import ui
from serverctl.errors import CtlError

from .commands import endpoint, sync, tenant


class _OfficectlGroup(TyperGroup):
    """Shows expected failures as a message and a hint instead of a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CtlError as problem:
            ui.error(problem)
            ctx.exit(1)


app = typer.Typer(
    cls=_OfficectlGroup,
    name="officectl",
    help="Manage Collabora Online on this server, and the Nextclouds that use it.",
    epilog="Run [bold]officectl COMMAND --help[/] to see what a command does, with examples.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.command()(endpoint.endpoint)
app.command()(tenant.add)
app.command(name="list")(tenant.list_tenants)
app.command()(tenant.remove)
app.command()(tenant.doctor)
app.command()(tenant.update)
# Everything after the name is occ's, not officectl's: the options are handed on as they are.
app.command(name="occ", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(
    tenant.run_occ)
app.command()(sync.sync)
