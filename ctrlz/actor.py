"""Who made this change.

An undo history without attribution answers "what happened" but not "who did
this, and why" -- which is the first question anyone asks after an incident.

The values are gathered from the environment rather than from a login, because
ctrlz has no identity system of its own and inventing one would be worse than
being honest about what we know. What we know is: the OS user, the machine, the
application, and whatever ticket the person chose to name.

Attribution is recorded in the *database's own* change log, not alongside it, so
it survives the tool being bypassed: a change made directly in psql still
carries the OS user that made it.
"""

from __future__ import annotations

import getpass
import os
import socket
from dataclasses import dataclass, replace
from typing import Optional

#: How the change reached the database. Phase 2 adds 'gateway' and 'sdk'.
CLI = "cli"
LIBRARY = "library"
GATEWAY = "gateway"
SDK = "sdk"

UNKNOWN = "unknown"

#: Values are written into database session settings and columns; keep them
#: bounded so a hostile or accidental environment variable cannot bloat a row.
MAX_FIELD = 200


@dataclass(frozen=True)
class Actor:
    """The person and place behind an operation."""

    user: str = UNKNOWN
    host: str = UNKNOWN
    application: str = "ctrlz"
    ticket: Optional[str] = None
    channel: str = LIBRARY

    @classmethod
    def resolve(
        cls,
        channel: str = LIBRARY,
        user: Optional[str] = None,
        ticket: Optional[str] = None,
        application: Optional[str] = None,
    ) -> "Actor":
        """Work out who is running this, without ever failing.

        Every lookup here can raise on a sufficiently unusual host -- no
        password database, no resolvable hostname, a stripped container. None
        of that is a reason to refuse to record a change.
        """
        return cls(
            user=_clean(user or os.environ.get("CTRLZ_ACTOR") or _os_user()),
            host=_clean(os.environ.get("CTRLZ_HOST") or _hostname()),
            application=_clean(
                application or os.environ.get("CTRLZ_APPLICATION") or "ctrlz"
            ),
            ticket=_clean(ticket or os.environ.get("CTRLZ_TICKET") or "") or None,
            channel=_clean(channel or LIBRARY),
        )

    def with_channel(self, channel: str) -> "Actor":
        return replace(self, channel=channel)

    def as_settings(self) -> dict[str, str]:
        """The session settings the capture layer propagates.

        Keys match the `ctrlz.*` custom settings already used for labels, so
        attribution rides the mechanism that exists rather than adding one
        (design decision D1.4).
        """
        return {
            "ctrlz.actor_user": self.user,
            "ctrlz.actor_host": self.host,
            "ctrlz.actor_app": self.application,
            "ctrlz.ticket": self.ticket or "",
            "ctrlz.channel": self.channel,
        }

    def describe(self) -> str:
        text = f"{self.user}@{self.host}"
        if self.ticket:
            text += f" ({self.ticket})"
        return text


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry, no $USER, stripped image
        return os.environ.get("USER") or os.environ.get("USERNAME") or UNKNOWN


def _hostname() -> str:
    try:
        return socket.gethostname() or UNKNOWN
    except Exception:  # noqa: BLE001 - unresolvable hostname
        return UNKNOWN


def _clean(value: object) -> str:
    text = str(value or "").strip()
    # Newlines would corrupt log output and settings values alike.
    text = text.replace("\n", " ").replace("\r", " ")
    return text[:MAX_FIELD]
