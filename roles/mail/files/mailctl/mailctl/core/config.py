"""Settings from /etc/mailctl/config.json, written by Ansible, plus system paths."""

import json
import os
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

from .errors import MailctlError

DEFAULT_PATH = Path("/etc/mailctl/config.json")


@dataclass(frozen=True)
class SendLimit:
    recipients: int
    seconds: int


@dataclass(frozen=True)
class Config:
    hostname: str
    send_limits: tuple[SendLimit, ...] = ()
    send_limits_by_account: Mapping[str, tuple[SendLimit, ...]] = field(default_factory=dict)
    db_socket: str = "/run/mysqld/mysqld.sock"
    db_user: str = "root"
    vmail_root: Path = Path("/var/vmail")
    vmail_user: str = "vmail"
    dkim_keys: Path = Path("/etc/opendkim/keys")
    dkim_key_table: Path = Path("/etc/opendkim/KeyTable")
    dkim_signing_table: Path = Path("/etc/opendkim/SigningTable")
    dkim_user: str = "opendkim"
    mail_logs: tuple[Path, ...] = (Path("/var/log/mail.log.1"), Path("/var/log/mail.log"))
    sieve_after: Path = Path("/etc/dovecot/sieve-after")
    certificate_file: Path | None = None  # by default the Let's Encrypt certificate of the hostname
    # The web root of the autoconfig and autodiscover sites, where certbot puts its challenges too.
    autodiscover_root: Path = Path("/var/www/mailautodiscover")
    # The TransIP login and key, which mailctl asks for when it first needs them.
    transip_settings: Path = Path("/etc/mailctl/transip.json")
    transip_key: Path = Path("/etc/mailctl/transip.key")
    # Webmail: SOGo, behind a site at webmail.DOMAIN in OpenLiteSpeed with a Let's Encrypt certificate
    sogo_address: str = "127.0.0.1:20000"
    sogo_resources: Path = Path("/usr/lib/GNUstep/SOGo/WebServerResources")
    ols_root: Path = Path("/usr/local/lsws")
    webmail_root: Path = Path("/var/www/webmail")
    letsencrypt_dir: Path = Path("/etc/letsencrypt")
    letsencrypt_email: str = ""  # for a new Let's Encrypt account; without it, one is made without an address

    def certificate(self) -> Path:
        """The certificate Postfix and Dovecot present, for the hostname and every mail.DOMAIN."""
        return self.certificate_file or self.letsencrypt_dir / "live" / self.hostname / "fullchain.pem"

    def limits_for(self, address: str) -> tuple[SendLimit, ...]:
        """The account's own limits if it has any (an empty tuple means no limit), else the defaults."""
        return self.send_limits_by_account.get(address, self.send_limits)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("MAILCTL_CONFIG", DEFAULT_PATH))
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise MailctlError(f"Can't read {path}: {error.strerror}.", hint="Run the Ansible playbook for this server.") from None
    except ValueError as error:  # invalid JSON or invalid UTF-8
        raise MailctlError(f"{path} isn't valid JSON: {error}.") from None
    if not isinstance(data, dict):
        raise MailctlError(f"{path} should hold a JSON object.")

    settings = {setting.name: setting for setting in fields(Config)}
    unknown = sorted(data.keys() - settings.keys())
    if unknown:
        raise MailctlError(f"Unknown setting in {path}: {', '.join(unknown)}.")
    missing = [
        name
        for name, setting in settings.items()
        if setting.default is MISSING and setting.default_factory is MISSING and name not in data
    ]
    if missing:
        raise MailctlError(f"{path} is missing: {', '.join(missing)}.")

    values = {}
    for name, value in data.items():
        convert = _CONVERTERS.get(name) or (_path if isinstance(settings[name].default, Path) else _text)
        try:
            values[name] = convert(value)
        except (TypeError, KeyError, ValueError, AttributeError):
            raise MailctlError(f"Invalid value for {name} in {path}.") from None
    return Config(**values)


def _text(value) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value


def _path(value) -> Path:
    return Path(_text(value))


def _limits(value) -> tuple[SendLimit, ...]:
    return tuple(SendLimit(recipients=int(limit["recipients"]), seconds=int(limit["seconds"])) for limit in value)


def _optional(convert):
    return lambda value: None if value is None else convert(value)


def _paths(value) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise TypeError(value)
    return tuple(_path(item) for item in value)


_CONVERTERS = {
    "send_limits": _limits,
    "send_limits_by_account": lambda value: {_text(account).lower(): _limits(limits) for account, limits in value.items()},
    "mail_logs": _paths,
    "certificate_file": _optional(_path),
}
