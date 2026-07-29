"""The wire-protocol codec, in isolation.

These need no database. The gateway tests prove it works against real clients;
these pin the edges those tests cannot reach on demand -- a message split at
every possible byte, a truncated startup packet, a `Parse` with no terminator.
"""

from __future__ import annotations

import struct

import pytest

from ctrlz.gateway import protocol as p


def message(tag: bytes, payload: bytes) -> bytes:
    return p.Message(tag, payload).encode()


# -- framing ---------------------------------------------------------------


def test_a_message_round_trips():
    original = p.Message(p.QUERY, b"SELECT 1\x00")
    reader = p.MessageReader()
    reader.feed(original.encode())
    assert reader.messages() == [original]


def test_length_includes_itself_but_not_the_tag():
    encoded = p.Message(p.QUERY, b"ab").encode()
    assert encoded[0:1] == p.QUERY
    assert struct.unpack("!I", encoded[1:5])[0] == 2 + 4
    assert len(encoded) == 1 + 4 + 2


@pytest.mark.parametrize("split", range(1, 14))
def test_a_message_split_across_reads_is_reassembled(split):
    """TCP does not preserve message boundaries; every split must survive."""
    encoded = p.Message(p.QUERY, b"SELECT 1\x00").encode()
    reader = p.MessageReader()

    reader.feed(encoded[:split])
    assert reader.messages() == [], f"emitted early at byte {split}"

    reader.feed(encoded[split:])
    assert [m.tag for m in reader.messages()] == [p.QUERY]


def test_several_messages_in_one_read_all_come_out():
    reader = p.MessageReader()
    reader.feed(
        message(p.QUERY, b"a\x00") + message(p.SYNC, b"") + message(p.TERMINATE, b"")
    )
    assert [m.tag for m in reader.messages()] == [p.QUERY, p.SYNC, p.TERMINATE]


def test_a_trailing_partial_message_stays_buffered():
    reader = p.MessageReader()
    complete = message(p.QUERY, b"a\x00")
    reader.feed(complete + message(p.SYNC, b"")[:3])
    assert [m.tag for m in reader.messages()] == [p.QUERY]
    assert len(reader) == 3


@pytest.mark.parametrize("length", [0, 1, 3, p.MAX_MESSAGE_BYTES + 1])
def test_an_implausible_length_is_rejected(length):
    """A stream claiming a 2 GiB message is not a session we should follow."""
    reader = p.MessageReader()
    reader.feed(p.QUERY + struct.pack("!I", length) + b"payload")
    with pytest.raises(p.ProtocolError):
        reader.messages()


# -- statement extraction --------------------------------------------------


def test_a_query_message_carries_its_sql():
    assert p.statement_of(p.Message(p.QUERY, b"DELETE FROM t\x00")) == "DELETE FROM t"


def test_a_parse_message_carries_sql_after_the_statement_name():
    payload = b"stmt1\x00SELECT $1\x00" + struct.pack("!H", 0)
    assert p.statement_of(p.Message(p.PARSE, payload)) == "SELECT $1"


def test_an_unnamed_parse_is_read_correctly():
    payload = b"\x00SELECT 1\x00" + struct.pack("!H", 0)
    assert p.statement_of(p.Message(p.PARSE, payload)) == "SELECT 1"


@pytest.mark.parametrize("tag", [p.SYNC, p.TERMINATE, b"B", b"D", b"d", b"c"])
def test_other_messages_carry_no_statement(tag):
    """Anything without SQL in it is relayed without being understood."""
    assert p.statement_of(p.Message(tag, b"\x00\x01binary")) is None


def test_a_missing_terminator_does_not_raise():
    """Malformed input is the server's to reject, not ours to crash on."""
    assert p.statement_of(p.Message(p.QUERY, b"SELECT 1")) == "SELECT 1"


def test_invalid_utf8_is_replaced_not_fatal():
    assert p.statement_of(p.Message(p.QUERY, b"SELECT '\xff\xfe'\x00")) is not None


# -- startup ---------------------------------------------------------------


def startup(version: int, parameters: bytes = b"") -> bytes:
    body = struct.pack("!I", version) + parameters
    return struct.pack("!I", len(body) + 4) + body


def test_a_startup_packet_yields_its_parameters():
    packet = p.parse_startup(
        startup(196608, b"user\x00ada\x00database\x00app\x00application_name\x00psql\x00\x00")
    )
    assert packet.is_startup
    assert packet.user == "ada"
    assert packet.database == "app"
    assert packet.application == "psql"


def test_the_database_defaults_to_the_user():
    packet = p.parse_startup(startup(196608, b"user\x00ada\x00\x00"))
    assert packet.database == "ada"


@pytest.mark.parametrize(
    "version,attribute",
    [
        (p.SSL_REQUEST, "is_ssl_request"),
        (p.CANCEL_REQUEST, "is_cancel_request"),
        (p.GSSENC_REQUEST, "is_gssenc_request"),
    ],
)
def test_special_startup_packets_are_recognised(version, attribute):
    packet = p.parse_startup(startup(version))
    assert getattr(packet, attribute) is True
    assert packet.is_startup is False


def test_the_startup_packet_is_kept_verbatim_for_forwarding():
    """Re-serialising would drop parameters we do not model."""
    raw = startup(196608, b"user\x00ada\x00some_future_option\x00yes\x00\x00")
    assert p.parse_startup(raw).raw == raw


@pytest.mark.parametrize("raw", [b"", b"\x00", b"\x00\x00\x00\x08"])
def test_a_truncated_startup_packet_is_rejected(raw):
    with pytest.raises(p.ProtocolError):
        p.parse_startup(raw)


def test_a_startup_packet_whose_length_lies_is_rejected():
    raw = struct.pack("!II", 999, 196608)
    with pytest.raises(p.ProtocolError):
        p.parse_startup(raw)


# -- error construction ----------------------------------------------------


def test_an_error_response_carries_the_fields_a_client_renders():
    encoded = p.error_response("refused", detail="because", hint="try this")
    assert encoded[0:1] == p.ERROR_RESPONSE
    assert struct.unpack("!I", encoded[1:5])[0] == len(encoded) - 1

    payload = encoded[5:]
    assert b"SERROR\x00" in payload
    assert b"C" + p.SQLSTATE_BLOCKED.encode() + b"\x00" in payload
    assert b"Mrefused\x00" in payload
    assert b"Dbecause\x00" in payload
    assert b"Htry this\x00" in payload
    assert payload.endswith(b"\x00\x00")


def test_an_error_response_is_readable_back_as_a_message():
    reader = p.MessageReader()
    reader.feed(p.error_response("refused"))
    assert [m.tag for m in reader.messages()] == [p.ERROR_RESPONSE]


def test_ready_for_query_reports_the_transaction_state():
    for state in (b"I", b"T", b"E"):
        reader = p.MessageReader()
        reader.feed(p.ready_for_query(state))
        assert reader.messages()[0].payload == state


# -- the reader must not crash on hostile input ----------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"Q",
        b"Q\x00\x00",
        b"\xff" * 64,
        bytes(range(256)),
    ],
)
def test_the_reader_survives_junk(data):
    reader = p.MessageReader()
    reader.feed(data)
    try:
        reader.messages()
    except p.ProtocolError:
        pass  # a refusal is fine; a crash is not
