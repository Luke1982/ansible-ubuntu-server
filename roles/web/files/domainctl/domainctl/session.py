"""What a command works with: this server's configuration, its own addresses and a DNS resolver."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property

from serverctl import system
from serverctl.dns import IPAddress, Resolver, SystemResolver

from . import config as configuration
from .config import TOOL, Config


class Session:
    def __init__(self, settings: Config) -> None:
        self.config = settings

    @cached_property
    def server_ips(self) -> set[IPAddress]:
        """Looked up on first use, so a command that never needs them never fails on them."""
        return system.server_ips()

    @cached_property
    def resolver(self) -> Resolver:
        return SystemResolver()


@contextmanager
def open_session() -> Iterator[Session]:
    system.require_root(TOOL)
    yield Session(configuration.load())
