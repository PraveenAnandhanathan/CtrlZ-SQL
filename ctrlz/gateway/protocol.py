"""The PostgreSQL wire protocol, only as far as we need it.

The gateway's job is to read the SQL out of two message types and relay
everything else untouched. That means this module deliberately does *not*
model the protocol: it frames messages, extracts a statement from the two
that carry one, and builds an error. Anything else stays an opaque
``bytes`` object all the way through.

That restraint is the safety property. A protocol we do not parse is a
protocol we cannot corrupt -- ``COPY`` streams, binary parameters,
replication and whatever PostgreSQL adds next all pass through because we
never look inside them.

Reference: PostgreSQL "Frontend/Backend Protocol", message formats.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# Message type tags we care about. Everything else is relayed by tag alone.
QUERY = b"Q"           # simple query, carries SQL
PARSE = b"P"           # extended protocol, carries SQL
SYNC = b"S"            # ends an extended-protocol sequence
TERMINATE = b"X"
READY_FOR_QUERY = b"Z"
ERROR_RESPONSE = b"E"
COMMAND_COMPLETE = b"C"
BACKEND_KEY_DATA = b"K"
AUTHENTICATION = b"R"

#: Version field values that mean "this is not a normal startup packet".
SSL_REQUEST = 80877103
CANCEL_REQUEST = 80877102
GSSENC_REQUEST = 80877104

#: Sent in reply to SSLRequest. One byte, before any framing exists: 'S' begins
#: a TLS handshake on the same socket, 'N' carries on in the clear.
SSL_DECLINED = b"N"
SSL_ACCEPTED = b"S"

#: SQLSTATE for a statement we refused. 42501 is insufficient_privilege, which
#: is what a refusal by policy actually is, and clients already understand it.
SQLSTATE_BLOCKED = "42501"

#: SQLSTATE for a connection refused because the gateway is at its limit.
#: PostgreSQL's own code for the same condition, so clients and pools already
#: know to back off and retry rather than treat it as fatal.
SQLSTATE_TOO_MANY = "53300"

MAX_MESSAGE_BYTES = 1 << 30  # 1 GiB; beyond this the stream is not a session


class ProtocolError(Exception):
    """The byte stream is not a protocol we recognise."""


@dataclass(frozen=True)
class Message:
    """A tagged protocol message, kept in its original bytes."""

    tag: bytes
    payload: bytes

    def encode(self) -> bytes:
        return self.tag + struct.pack("!I", len(self.payload) + 4) + self.payload

    @property
    def size(self) -> int:
        return 1 + 4 + len(self.payload)


@dataclass(frozen=True)
class StartupPacket:
    """The untagged first packet a client sends."""

    version: int
    parameters: dict[str, str] = field(default_factory=dict)
    raw: bytes = b""

    @property
    def is_ssl_request(self) -> bool:
        return self.version == SSL_REQUEST

    @property
    def is_cancel_request(self) -> bool:
        return self.version == CANCEL_REQUEST

    @property
    def is_gssenc_request(self) -> bool:
        return self.version == GSSENC_REQUEST

    @property
    def is_startup(self) -> bool:
        return not (
            self.is_ssl_request or self.is_cancel_request or self.is_gssenc_request
        )

    @property
    def user(self) -> str:
        return self.parameters.get("user", "")

    @property
    def database(self) -> str:
        return self.parameters.get("database", self.user)

    @property
    def application(self) -> str:
        return self.parameters.get("application_name", "")


# -- decoding ---------------------------------------------------------------


def parse_startup(raw: bytes) -> StartupPacket:
    """Read a startup packet, keeping the original bytes for forwarding.

    The packet is reproduced verbatim rather than re-encoded: a client may
    send parameters we do not model, and re-serialising would silently drop
    them.
    """
    if len(raw) < 8:
        raise ProtocolError("startup packet is too short")

    length, version = struct.unpack("!II", raw[:8])
    if length != len(raw):
        raise ProtocolError(
            f"startup packet claims {length} bytes but {len(raw)} were read"
        )

    parameters: dict[str, str] = {}
    if version not in (SSL_REQUEST, CANCEL_REQUEST, GSSENC_REQUEST):
        parameters = _parse_parameters(raw[8:])

    return StartupPacket(version=version, parameters=parameters, raw=raw)


def _parse_parameters(body: bytes) -> dict[str, str]:
    """Null-terminated key/value pairs, ended by an empty key."""
    parameters: dict[str, str] = {}
    parts = body.split(b"\x00")
    for index in range(0, len(parts) - 1, 2):
        key = parts[index]
        if not key:
            break
        value = parts[index + 1] if index + 1 < len(parts) else b""
        parameters[_text(key)] = _text(value)
    return parameters


def statement_of(message: Message) -> Optional[str]:
    """The SQL a message carries, or None if it does not carry any.

    Only ``Query`` and ``Parse`` do. Returning None is the signal to relay
    the message without looking at it further.
    """
    if message.tag == QUERY:
        return _text(_cstring(message.payload, 0)[0])
    if message.tag == PARSE:
        # Parse is: statement-name cstring, query cstring, then parameter types.
        _name, offset = _cstring(message.payload, 0)
        query, _ = _cstring(message.payload, offset)
        return _text(query)
    return None


def _cstring(payload: bytes, offset: int) -> tuple[bytes, int]:
    end = payload.find(b"\x00", offset)
    if end == -1:
        # Tolerate a missing terminator rather than raising: the caller is a
        # guardrail, and a malformed message should be relayed to the server
        # to reject, not crashed on here.
        return payload[offset:], len(payload)
    return payload[offset:end], end + 1


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


#: Sub-type of an Authentication ('R') message announcing SASL, followed by the
#: mechanisms the server will accept as a list of null-terminated strings.
AUTH_SASL = 10

#: The SCRAM variant that binds authentication to a specific TLS session.
CHANNEL_BINDING_MECHANISM = b"SCRAM-SHA-256-PLUS"


def sasl_mechanisms(message: Message) -> Optional[list[bytes]]:
    """The mechanisms offered by an AuthenticationSASL message, or None."""
    if message.tag != AUTHENTICATION or len(message.payload) < 4:
        return None
    if struct.unpack("!I", message.payload[:4])[0] != AUTH_SASL:
        return None
    body = message.payload[4:]
    return [name for name in body.split(b"\x00") if name]


def without_channel_binding(message: Message) -> tuple[Message, bool]:
    """Remove SCRAM-SHA-256-PLUS from a SASL offer.

    Returns the message to send and whether anything was removed, so the caller
    can log a downgrade it performed rather than leave it invisible.

    If stripping would leave no mechanisms at all -- a server configured to
    accept only channel binding -- the offer is passed through untouched. An
    empty list is not a weaker negotiation, it is a broken message, and turning
    a refusal we understand into a parse error the client cannot explain would
    be strictly worse.
    """
    offered = sasl_mechanisms(message)
    if not offered or CHANNEL_BINDING_MECHANISM not in offered:
        return message, False

    kept = [name for name in offered if name != CHANNEL_BINDING_MECHANISM]
    if not kept:
        return message, False

    payload = struct.pack("!I", AUTH_SASL)
    payload += b"".join(name + b"\x00" for name in kept) + b"\x00"
    return Message(AUTHENTICATION, payload), True


# -- encoding ---------------------------------------------------------------


def error_response(
    message: str,
    severity: str = "ERROR",
    sqlstate: str = SQLSTATE_BLOCKED,
    detail: str = "",
    hint: str = "",
) -> bytes:
    """A protocol-native error, so every client renders it natively.

    Field tags: S severity, V non-localised severity, C SQLSTATE, M message,
    D detail, H hint. Terminated by a zero byte.
    """
    fields = [
        (b"S", severity),
        (b"V", severity),
        (b"C", sqlstate),
        (b"M", message),
    ]
    if detail:
        fields.append((b"D", detail))
    if hint:
        fields.append((b"H", hint))

    payload = b"".join(
        tag + value.encode("utf-8") + b"\x00" for tag, value in fields
    ) + b"\x00"
    return Message(ERROR_RESPONSE, payload).encode()


def ready_for_query(state: bytes = b"I") -> bytes:
    """``I`` idle, ``T`` in a transaction, ``E`` in a failed transaction."""
    return Message(READY_FOR_QUERY, state).encode()


def query(sql: str) -> bytes:
    return Message(QUERY, sql.encode("utf-8") + b"\x00").encode()


def terminate() -> bytes:
    return Message(TERMINATE, b"").encode()


# -- streaming --------------------------------------------------------------


class MessageReader:
    """Reassembles tagged messages from a stream of arbitrary reads.

    TCP does not preserve message boundaries, so a message can arrive split
    across reads or several can arrive together. Everything not yet complete
    stays buffered.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)

    def __len__(self) -> int:
        return len(self._buffer)

    def messages(self) -> list[Message]:
        """Every complete message currently buffered."""
        out: list[Message] = []
        while True:
            message = self._next()
            if message is None:
                return out
            out.append(message)

    def _next(self) -> Optional[Message]:
        if len(self._buffer) < 5:
            return None
        length = struct.unpack("!I", self._buffer[1:5])[0]
        if length < 4:
            raise ProtocolError(f"message length {length} is below the minimum")
        if length > MAX_MESSAGE_BYTES:
            raise ProtocolError(f"message length {length} is implausible")
        total = 1 + length
        if len(self._buffer) < total:
            return None
        tag = bytes(self._buffer[0:1])
        payload = bytes(self._buffer[5:total])
        del self._buffer[:total]
        return Message(tag, payload)
