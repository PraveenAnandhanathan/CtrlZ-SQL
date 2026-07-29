"""The contract every analysis backend implements."""

from __future__ import annotations

import abc

from ..model import Analysis


class Backend(abc.ABC):
    """Turns SQL text into one ``Analysis`` per statement."""

    #: Identifier recorded on every Analysis this backend produces.
    name: str = "base"

    @classmethod
    def available(cls) -> bool:
        """Whether this backend can run here (its dependency is installed)."""
        return True

    @abc.abstractmethod
    def analyze_script(self, sql: str, dialect: str | None = None) -> list[Analysis]:
        """Analyse every statement in ``sql``.

        Implementations may raise -- the registry catches and falls back. What
        they may *not* do is return a confident answer they cannot support.
        """
