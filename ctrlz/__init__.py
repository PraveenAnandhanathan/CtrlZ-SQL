"""ctrlz -- undo for SQL.

Row images are captured by the database itself, as changes happen, so an undo
is built from what actually changed rather than from re-parsing the statement.
"""

from .api import Toolkit, connect, parse_duration
from .errors import (
    ConfigError,
    CtrlzError,
    NoIdentity,
    NotInitialized,
    NotTracked,
    NotUndoable,
    PreflightBlocked,
    UndoConflict,
    UnknownOperation,
)
from .model import (
    Change,
    ExecutionResult,
    Operation,
    RowVerdict,
    Undoability,
    UndoResult,
)

__version__ = "0.1.0"

__all__ = [
    "Toolkit",
    "connect",
    "parse_duration",
    "Change",
    "ExecutionResult",
    "Operation",
    "RowVerdict",
    "Undoability",
    "UndoResult",
    "CtrlzError",
    "ConfigError",
    "NoIdentity",
    "NotInitialized",
    "NotTracked",
    "NotUndoable",
    "PreflightBlocked",
    "UndoConflict",
    "UnknownOperation",
]
