"""What the gateway does when someone opens more connections than it likes.

The gateway is a network daemon that opens **one upstream database connection
per client**. That makes an unbounded client count into an unbounded database
connection count, and `max_connections` on the server is a hard limit shared
with everybody — including clients that never went near the gateway.

That is the one outcome the whole design is meant to make impossible. Failing
open covers a *bug* in the checkpoint; it does not cover the checkpoint
consuming the resource it sits in front of. So the cap is checked before
anything upstream is opened, and refusing costs a client a retry rather than
costing the database a connection.

The handshake timeout is the other half: a connection that opens and says
nothing holds a slot the cap has already counted, so without a deadline a
hundred silent sockets are an outage rather than a nuisance.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct

import pytest

from ctrlz.gateway import Gateway, Upstream, protocol

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="set CTRLZ_TEST_PG_DSN to run the gateway tests"
)

from .test_gateway import RunningGateway, sandbox  # noqa: E402,F401


def startup_packet(user: str = "ctrlz", database: str = "ctrlz_test") -> bytes:
    body = struct.pack("!I", 196608)
    body += b"user\x00" + user.encode() + b"\x00"
    body += b"database\x00" + database.encode() + b"\x00\x00"
    return struct.pack("!I", len(body) + 4) + body


def read_messages(sock: socket.socket, timeout: float = 5.0) -> list:
    sock.settimeout(timeout)
    reader = protocol.MessageReader()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            reader.feed(chunk)
            messages = reader.messages()
            if messages:
                return messages
    except (socket.timeout, OSError):
        pass
    return []


# -- the connection cap ------------------------------------------------------


def test_a_client_over_the_limit_is_refused_before_the_database_is_touched(sandbox):
    """The point of the cap, stated as a test.

    Two connections are allowed. The third must be refused, and refused with a
    protocol error rather than a dropped socket, so the client knows to retry
    instead of treating it as fatal.
    """
    import psycopg2

    with RunningGateway(sandbox.dsn, max_connections=2) as gw:
        held = []
        try:
            for _ in range(2):
                sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
                sock.sendall(startup_packet())
                held.append(sock)

            # Let the two settle so they are counted before the third arrives.
            for sock in held:
                read_messages(sock, timeout=3)

            third = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
            held.append(third)
            third.sendall(startup_packet())
            messages = read_messages(third, timeout=5)
        finally:
            for sock in held:
                try:
                    sock.close()
                except OSError:
                    pass

    assert messages, "the third connection got no reply at all"
    assert messages[0].tag == protocol.ERROR_RESPONSE
    payload = messages[0].payload
    assert protocol.SQLSTATE_TOO_MANY.encode() in payload
    assert b"too many connections" in payload
    assert gw.gateway.stats.turned_away >= 1


def test_the_refusal_never_opens_an_upstream_connection(sandbox):
    """Refusing after connecting upstream would spend the very resource the
    cap exists to protect, so the ordering is asserted rather than assumed."""
    opened = []

    with RunningGateway(sandbox.dsn, max_connections=1) as gw:
        real_connect = gw.gateway._connect_upstream

        async def counted():
            opened.append(1)
            return await real_connect()

        gw.gateway._connect_upstream = counted

        held = []
        try:
            first = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
            held.append(first)
            first.sendall(startup_packet())
            read_messages(first, timeout=3)
            before = len(opened)

            second = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
            held.append(second)
            second.sendall(startup_packet())
            read_messages(second, timeout=5)
        finally:
            for sock in held:
                try:
                    sock.close()
                except OSError:
                    pass

    assert gw.gateway.stats.turned_away >= 1
    assert len(opened) == before, (
        "the refused client still caused an upstream connection to be opened"
    )


def test_zero_means_no_limit(sandbox):
    """An operator who has sized their pool elsewhere can switch the cap off."""
    with RunningGateway(sandbox.dsn, max_connections=0) as gw:
        held = []
        try:
            for _ in range(4):
                sock = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
                sock.sendall(startup_packet())
                held.append(sock)
            for sock in held:
                read_messages(sock, timeout=3)
        finally:
            for sock in held:
                try:
                    sock.close()
                except OSError:
                    pass
    assert gw.gateway.stats.turned_away == 0


def test_a_slot_is_returned_when_a_client_leaves(sandbox):
    """A cap that leaked slots would be an outage on a timer."""
    with RunningGateway(sandbox.dsn, max_connections=1) as gw:
        first = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
        first.sendall(startup_packet())
        read_messages(first, timeout=3)
        first.close()

        # Give the handler a moment to notice the close and drop its slot.
        import time

        for _ in range(50):
            if len(gw.gateway._sessions) == 0:
                break
            time.sleep(0.1)

        second = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
        try:
            second.sendall(startup_packet())
            messages = read_messages(second, timeout=5)
        finally:
            second.close()

    tags = [m.tag for m in messages]
    assert protocol.ERROR_RESPONSE not in tags or gw.gateway.stats.turned_away == 0


# -- the handshake timeout ---------------------------------------------------


def test_a_client_that_never_speaks_is_dropped(sandbox):
    """Slowloris, in the form that matters here: the silent socket holds a
    slot the cap has already counted."""
    with RunningGateway(sandbox.dsn, handshake_timeout=1.0) as gw:
        sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
        try:
            sock.settimeout(10)
            # Say nothing at all, and wait for the gateway to give up.
            data = sock.recv(65536)
        except (socket.timeout, OSError):
            data = b""
        finally:
            sock.close()

    assert gw.gateway.stats.timed_out >= 1, "the silent client was never dropped"
    assert data == b"", "nothing should have been sent to a client that never spoke"


def test_a_half_sent_startup_packet_is_dropped(sandbox):
    """The subtler form: enough bytes to look like a session starting, then
    nothing. `readexactly` would wait for the rest indefinitely."""
    with RunningGateway(sandbox.dsn, handshake_timeout=1.0) as gw:
        sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
        try:
            packet = startup_packet()
            sock.sendall(packet[:6])        # a partial header, then silence
            sock.settimeout(10)
            try:
                sock.recv(65536)
            except (socket.timeout, OSError):
                pass
        finally:
            sock.close()

    assert gw.gateway.stats.timed_out >= 1


def test_the_timeout_does_not_disturb_a_normal_session(sandbox):
    """A deadline on the handshake must not become a deadline on the session:
    a database connection that sits idle between statements is ordinary."""
    from .test_gateway import psql

    with RunningGateway(sandbox.dsn, handshake_timeout=1.0) as gw:
        result = psql(gw.port, f"SELECT count(*) FROM {sandbox.schema}.users")

    assert result.returncode == 0, result.stderr
    assert gw.gateway.stats.timed_out == 0


# -- the defaults ------------------------------------------------------------


def test_the_defaults_are_set_and_documented():
    """A limit nobody set is a limit nobody has: these must default to
    something, not to unbounded."""
    from ctrlz.gateway.server import (
        DEFAULT_HANDSHAKE_TIMEOUT,
        DEFAULT_MAX_CONNECTIONS,
    )

    gateway = Gateway(Upstream.from_dsn(PG_DSN))
    assert gateway.max_connections == DEFAULT_MAX_CONNECTIONS
    assert gateway.handshake_timeout == DEFAULT_HANDSHAKE_TIMEOUT
    assert 0 < DEFAULT_MAX_CONNECTIONS <= 1000
    assert 0 < DEFAULT_HANDSHAKE_TIMEOUT <= 120
