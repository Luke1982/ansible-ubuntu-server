"""Passwords this server makes up itself.

A database user's password, or the first password of an administrator account: nobody has to remember them, so
they are long and random rather than something a person could type from memory. They come from the system's
random source, not from Python's ordinary one, which is predictable from the numbers it has already given out.
"""

import secrets

LENGTH = 24  # bytes of randomness; the text is about a third longer


def new_password(length: int = LENGTH) -> str:
    """A password of random characters, safe to put in a URL or a shell command."""
    return secrets.token_urlsafe(length)
