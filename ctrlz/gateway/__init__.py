"""A PostgreSQL-speaking checkpoint in front of a database.

    ctrlz gateway --listen 127.0.0.1:6543 --upstream postgresql://localhost/app

Point any client at the gateway instead of the database. Statements the
rulebook refuses come back as ordinary database errors, so no client needs to
know the gateway exists.

The gateway is optional by construction. Capture and undo live inside the
database and do not depend on it -- switching it off loses the checkpoint,
never the recorder.
"""

from .interceptor import FORWARD, REFUSE, Interceptor, Verdict
from .protocol import Message, MessageReader, ProtocolError, StartupPacket
from .server import Gateway, Upstream, run_gateway

__all__ = [
    "FORWARD",
    "REFUSE",
    "Gateway",
    "Interceptor",
    "Message",
    "MessageReader",
    "ProtocolError",
    "StartupPacket",
    "Upstream",
    "Verdict",
    "run_gateway",
]
