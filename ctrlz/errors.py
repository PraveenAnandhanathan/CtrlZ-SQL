"""Exception hierarchy for ctrlz."""


class CtrlzError(Exception):
    """Base class for every error raised by the toolkit."""


class ConfigError(CtrlzError):
    """The DSN, CLI arguments, or environment are unusable."""


class NotInitialized(CtrlzError):
    """The ctrlz metadata store is missing from the target database."""


class NotTracked(CtrlzError):
    """A table was referenced that has no capture triggers installed."""


class NoIdentity(CtrlzError):
    """A table cannot be tracked because rows have no stable identity."""


class UnknownOperation(CtrlzError):
    """No operation exists with the given id."""


class NotUndoable(CtrlzError):
    """The operation cannot be reversed, and we refuse to pretend otherwise."""


class UndoConflict(CtrlzError):
    """Rows drifted since capture; undoing would clobber somebody's change."""


class PreflightBlocked(CtrlzError):
    """A guardrail refused to run the statement as written."""
