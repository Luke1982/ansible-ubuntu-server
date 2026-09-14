"""What a command works with: this server's configuration and, once needed, the database."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from functools import cached_property

from .core import config, system
from .core.config import Config
from .core.db import Database, connect


class Session:
    def __init__(self, configuration: Config, exits: ExitStack) -> None:
        self.config = configuration
        self._exits = exits

    @cached_property
    def db(self) -> Database:
        """Connects on first use. The connection closes when the session ends."""
        return self._exits.enter_context(connect(self.config))


@contextmanager
def open_session() -> Iterator[Session]:
    system.require_root()
    with ExitStack() as exits:
        yield Session(config.load(), exits)
