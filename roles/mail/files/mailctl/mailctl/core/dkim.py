"""DKIM keys for OpenDKIM, selector "mail".

The key directory is the single source of truth: OpenDKIM's KeyTable and SigningTable are rebuilt from it,
so every domain with a key gets signed.
"""

import os
import tempfile
from pathlib import Path

from . import names, system
from .config import Config
from .errors import MailctlError

SELECTOR = "mail"


def record_name(domain: str) -> str:
    return f"{SELECTOR}._domainkey.{domain}"


def key_domains(config: Config) -> list[str]:
    """The domains that have a key. Folders in the key directory that aren't domain names are left out."""
    folders = (path.parent.name.lower() for path in config.dkim_keys.glob(f"*/{SELECTOR}.private"))
    return sorted({folder for folder in folders if names.valid_domain(folder)})


def has_key(config: Config, domain: str) -> bool:
    return _private_key(config, domain).is_file()


def create_key(config: Config, domain: str) -> bool:
    """Creates a key for a domain without one and returns whether it did. An existing key is never replaced."""
    if has_key(config, domain):
        return False
    directory = _key_dir(config, domain)
    directory.mkdir(parents=True, exist_ok=True)
    system.run("opendkim-genkey", "-b", "2048", "-s", SELECTOR, "-d", domain, "-D", str(directory))
    private_key = _private_key(config, domain)
    owner = system.find_user(config.dkim_user)
    os.chown(private_key, owner.pw_uid, owner.pw_gid)
    private_key.chmod(0o600)
    return True


def delete_key(config: Config, domain: str) -> None:
    system.remove_tree(_key_dir(config, domain))


def write_tables(config: Config) -> bool:
    """Rebuilds KeyTable and SigningTable from the key directory. Returns whether either of them changed."""
    domains = key_domains(config)
    key_table = "".join(f"{record_name(domain)} {domain}:{SELECTOR}:{_private_key(config, domain)}\n" for domain in domains)
    signing_table = "".join(f"*@{domain} {record_name(domain)}\n" for domain in domains)
    changes = [_replace(config.dkim_key_table, key_table), _replace(config.dkim_signing_table, signing_table)]
    return any(changes)


def update_opendkim(config: Config) -> bool:
    """Rebuilds the tables and has OpenDKIM read them if they changed. Returns whether they changed.

    When OpenDKIM can't be reloaded, the old tables are put back, so the next update tries again.
    """
    tables = (config.dkim_key_table, config.dkim_signing_table)
    previous = [_read(table) for table in tables]
    if not write_tables(config):
        return False
    try:
        system.reload("opendkim")
    except MailctlError:
        for table, content in zip(tables, previous):
            _restore(table, content)
        raise
    return True


def record_value(config: Config, domain: str) -> str:
    """The TXT record value, made from the public half of the domain's private key."""
    private_key = _private_key(config, domain)
    if not private_key.is_file():
        raise MailctlError(f"{domain} has no DKIM key.", hint=f"Create one with: mailctl dkim create {domain}")
    pem = system.run("openssl", "pkey", "-in", str(private_key), "-pubout")
    public_key = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    return f"v=DKIM1; h=sha256; k=rsa; p={public_key}"


def _key_dir(config: Config, domain: str) -> Path:
    """The domain's key folder. The old helper script kept the capitals a domain was given with."""
    if config.dkim_keys.is_dir():
        for folder in config.dkim_keys.iterdir():
            if folder.name.lower() == domain:
                return folder
    return config.dkim_keys / domain


def _private_key(config: Config, domain: str) -> Path:
    return _key_dir(config, domain) / f"{SELECTOR}.private"


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _restore(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _replace(path, content)


def _replace(path: Path, content: str) -> bool:
    """Replaces the file in one step, so OpenDKIM never reads half a table. Returns whether the content changed."""
    if _read(path) == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        file.write(content)
    os.chmod(file.name, 0o644)
    os.replace(file.name, path)
    return True
