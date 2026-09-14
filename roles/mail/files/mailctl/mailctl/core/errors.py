class MailctlError(Exception):
    """An expected failure, shown to the user as a message with an optional hint."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
