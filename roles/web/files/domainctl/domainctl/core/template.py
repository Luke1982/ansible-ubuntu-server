"""The OpenLiteSpeed template the sites share: conf/templates/webhosting.conf.

Ansible writes the file; domainctl only switches the redirect to HTTPS off and on again, between the markers
Ansible leaves in it. The redirect is shared by every site, so it is off for as short a time as possible: while
certbot proves one new domain over plain HTTP, and never when a command ends, however it ends.

The file belongs to OpenLiteSpeed's own user, like the rest of its config directory, so it is written as that
user: a symbolic link planted there must not make domainctl, which runs as root, write where that user couldn't.
"""

from serverctl import files, openlitespeed, system
from serverctl.errors import CtlError

from ..config import Config

BEGIN = "# BEGIN https-redirect"
END = "# END https-redirect"


def read(config: Config) -> str:
    text = files.read(config.template_path())
    if text is None:
        raise CtlError(f"There is no OpenLiteSpeed template at {config.template_path()}.",
                       hint="Run the Ansible playbook for this server: the web role writes it.")
    return text


def redirect_is_on(text: str) -> bool:
    """Whether any rule between the markers is active. A file without markers counts as on, so a hand-written
    template is never taken for one with the redirect switched off."""
    marked = _between(text)
    if marked is None:
        return True
    return any(line.strip() and not line.lstrip().startswith("#") for line in marked)


def with_redirect(text: str, on: bool) -> str:
    """The template with the rules between the markers commented out, or uncommented again."""
    start, end = _markers(text)
    lines = text.splitlines(keepends=True)
    changed = [_uncomment(line) if on else _comment(line) for line in lines[start + 1:end]]
    return "".join([*lines[:start + 1], *changed, *lines[end:]])


def set_redirect(config: Config, on: bool) -> bool:
    """Writes the template with the redirect switched on or off. Returns whether the file changed.

    Under the same lock as OpenLiteSpeed's own config: the redirect is shared by every site, so one command must
    not switch it on while another has it off to prove a new domain.
    """
    path = config.template_path()
    with openlitespeed.locked():
        wanted = with_redirect(read(config), on)
        with system.as_user(config.ols_user):
            return files.replace(path, wanted)


def _markers(text: str) -> tuple[int, int]:
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip().startswith(BEGIN)]
    ends = [index for index, line in enumerate(lines) if line.strip().startswith(END)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise CtlError(f"The OpenLiteSpeed template doesn't hold exactly one '{BEGIN}' ... '{END}' pair.",
                       hint="Run the Ansible playbook for this server to write the template again.")
    return starts[0], ends[0]


def _between(text: str) -> list[str] | None:
    try:
        start, end = _markers(text)
    except CtlError:
        return None
    return text.splitlines()[start + 1:end]


def _comment(line: str) -> str:
    if not line.strip() or line.lstrip().startswith("#"):
        return line
    indent = line[:len(line) - len(line.lstrip())]
    return f"{indent}#{line.lstrip()}"


def _uncomment(line: str) -> str:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return line
    indent = line[:len(line) - len(stripped)]
    return f"{indent}{stripped[1:]}"
