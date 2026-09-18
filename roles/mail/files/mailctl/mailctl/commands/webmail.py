"""mailctl webmail: the webmail sites at webmail.DOMAIN."""

from .. import ui
from ..core import dns_check, domains, system, webmail
from ..core.webmail import Outcome, State
from ..session import open_session
from .shared import group

app = group("Keep the webmail sites at webmail.DOMAIN in step with the mail domains.")


@app.command()
def sync() -> None:
    """Set up webmail for every domain whose webmail.DOMAIN points to this server, and remove it for the others.

    Each site gets a Let's Encrypt certificate of its own. This runs every day by itself; run it after publishing a
    domain's records to not wait for that.

    [dim]Example:[/] mailctl webmail sync
    """
    with open_session() as session:
        names = [domain.name for domain in domains.list_domains(session.db)]
        with ui.console.status("Setting up the webmail sites…"):
            result = webmail.sync(session.config, names, system.server_ips(), dns_check.SystemResolver())
    for outcome in result.outcomes:
        _show(outcome)
    # Ansible reads this line to tell whether anything changed.
    if result.changed:
        ui.success("Changed the webmail sites.")
    else:
        ui.note("Nothing changed.")


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
