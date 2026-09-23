"""Settings from /etc/domainctl/config.json, written by Ansible, plus system paths."""

import json
import os
from dataclasses import MISSING, dataclass, fields
from pathlib import Path

from serverctl.errors import CtlError
from serverctl.transip import Access

TOOL = "domainctl"
DEFAULT_PATH = Path("/etc/domainctl/config.json")


@dataclass(frozen=True)
class Config:
    # The server's own name, used to label the API tokens domainctl asks TransIP for.
    hostname: str
    # Where a site's Linux user gets its home directory, which is also the virtual host's root.
    home_root: Path = Path("/home")
    user_shell: str = "/bin/bash"  # so "su - USER" works; sshd's AllowUsers decides who may log in
    # The directories made inside a home directory, relative to it. The template serves the first one and
    # writes its logs in the second, through $VH_ROOT.
    docroot: str = "public_html/www"
    logs: str = "logs"
    # OpenLiteSpeed
    ols_root: Path = Path("/usr/local/lsws")
    ols_user: str = "lsadm"  # owns OpenLiteSpeed's config directory, and WebAdmin runs as it
    web_user: str = "nobody"  # the user OpenLiteSpeed serves and runs PHP as; the one the ACLs let in
    template: str = "webhosting"  # the vhTemplate in httpd_config.conf that the sites are members of
    template_file: str = "conf/templates/webhosting.conf"  # its settings, relative to ols_root
    # Let's Encrypt. A site's certificate is named after its user, because the template looks it up as $VH_NAME.
    letsencrypt_dir: Path = Path("/etc/letsencrypt")
    letsencrypt_email: str = ""  # for a new Let's Encrypt account; without it, one is made without an address
    # The TransIP login and key, shared with mailctl so they're entered once per server.
    transip_settings: Path = Path("/etc/transip/transip.json")
    transip_key: Path = Path("/etc/transip/transip.key")

    def home(self, user: str) -> Path:
        return self.home_root / user

    def docroot_of(self, user: str) -> Path:
        return self.home(user) / self.docroot

    def logs_of(self, user: str) -> Path:
        return self.home(user) / self.logs

    def template_path(self) -> Path:
        return self.ols_root / self.template_file

    def transip_access(self) -> Access:
        return Access(self.transip_settings, self.transip_key, self.hostname, TOOL)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("DOMAINCTL_CONFIG", DEFAULT_PATH))
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise CtlError(f"Can't read {path}: {error.strerror}.",
                       hint="Run the Ansible playbook for this server.") from None
    except ValueError as error:  # invalid JSON or invalid UTF-8
        raise CtlError(f"{path} isn't valid JSON: {error}.") from None
    if not isinstance(data, dict):
        raise CtlError(f"{path} should hold a JSON object.")

    settings = {setting.name: setting for setting in fields(Config)}
    unknown = sorted(data.keys() - settings.keys())
    if unknown:
        raise CtlError(f"Unknown setting in {path}: {', '.join(unknown)}.")
    missing = [name for name, setting in settings.items()
               if setting.default is MISSING and setting.default_factory is MISSING and name not in data]
    if missing:
        raise CtlError(f"{path} is missing: {', '.join(missing)}.")

    values = {}
    for name, value in data.items():
        convert = _path if isinstance(settings[name].default, Path) else _text
        try:
            values[name] = convert(value)
        except (TypeError, ValueError):
            raise CtlError(f"Invalid value for {name} in {path}.") from None
    return Config(**values)


def _text(value) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value


def _path(value) -> Path:
    return Path(_text(value))
