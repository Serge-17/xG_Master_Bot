class BotError(Exception):
    """Base error for the xG-Master bot."""


class InvalidCommandError(BotError):
    """Raised when a command payload cannot be parsed."""


class DataSourceUnavailableError(BotError):
    """Raised when a parser or provider cannot be reached."""
