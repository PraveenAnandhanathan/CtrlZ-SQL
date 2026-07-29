"""The gateway: a PostgreSQL-speaking checkpoint in front of a database.

Point any client at it instead of at the database and nothing about the client
changes. Statements the rulebook refuses come back as ordinary database errors,
which is why no plugin is needed for `psql`, DBeaver, a BI tool or an
application.

What it deliberately does not do:

* **It does not pool.** One upstream connection per client connection.
  Multiplexing sessions would break ``SET LOCAL``, temporary tables and
  transactions in ways that are extremely hard to see and impossible to
  explain.
* **It does not read credentials.** Authentication messages are relayed
  verbatim, so ``md5`` and ``SCRAM`` work because we do not participate
  (D2.4). The cost, stated rather than hidden: we cannot learn the
  authenticated identity, so attribution uses the startup packet's ``user``
  and ``application_name``.
* **It does not terminate TLS from the client.** It must read SQL, so it
  answers ``SSLRequest`` with a decline and proceeds in plaintext. Bind it to
  localhost or a trusted segment.
* **It is not required for undo.** Capture lives inside the database. Stop the
  gateway and every change is still recorded and still reversible.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlparse

from ..actor import GATEWAY, Actor
from ..policy import Policy
from . import protocol
from .interceptor import Interceptor

log = logging.getLogger("ctrlz.gateway")

STARTUP_HEADER = 8
MAX_STARTUP_BYTES = 1 << 20


@dataclass
class Upstream:
    """Where to send traffic, parsed from a DSN."""

    host: str = "127.0.0.1"
    port: int = 5432
    #: Overrides the database the client asked for, when set.
    database: Optional[str] = None

    @classmethod
    def from_dsn(cls, dsn: str) -> "Upstream":
        parsed = urlparse(dsn)
        database = unquote(parsed.path.lstrip("/")) or None
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            database=database,
        )


@dataclass
class Stats:
    connections: int = 0
    statements: int = 0
    refused: int = 0
    failed_open: int = 0
    errors: list[str] = field(default_factory=list)


class Gateway:
    """Accepts client connections and proxies them to one upstream."""

    def __init__(
        self,
        upstream: Upstream,
        policy: Optional[Policy] = None,
        tracked: tuple[str, ...] = (),
        environment: str = "default",
        attribute: bool = True,
    ):
        self.upstream = upstream
        self.interceptor = Interceptor(
            policy=policy, tracked=tracked, environment=environment
        )
        self.attribute = attribute
        self.stats = Stats()
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 6543) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        bound = self._server.sockets[0].getsockname()[1]
        log.info(
            "ctrlz gateway listening on %s:%s -> %s:%s",
            host, bound, self.upstream.host, self.upstream.port,
        )
        return bound

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop listening and end sessions still in flight.

        Without cancelling them, a client that never disconnects would keep a
        handler alive past shutdown -- fine for a daemon, noisy and misleading
        in a test.
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._sessions):
            task.cancel()
        if self._sessions:
            await asyncio.gather(*self._sessions, return_exceptions=True)
            self._sessions.clear()

    # -- one client --------------------------------------------------------

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
        try:
            await self._session(client_reader, client_writer)
        finally:
            if task is not None:
                self._sessions.discard(task)

    async def _session(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        self.stats.connections += 1
        server_writer: Optional[asyncio.StreamWriter] = None
        try:
            startup = await self._negotiate(client_reader, client_writer)
            if startup is None:
                return

            server_reader, server_writer = await asyncio.open_connection(
                self.upstream.host, self.upstream.port
            )

            if startup.is_cancel_request:
                # Cancellation arrives on its own connection and gets no reply.
                server_writer.write(startup.raw)
                await server_writer.drain()
                return

            server_writer.write(startup.raw)
            await server_writer.drain()

            actor = Actor.resolve(
                channel=GATEWAY,
                user=startup.user or None,
                application=startup.application or None,
            )

            ready = await self._relay_authentication(
                client_reader, server_reader, client_writer, server_writer, actor
            )
            if not ready:
                return

            await asyncio.gather(
                self._pump_client_to_server(
                    client_reader, server_writer, client_writer, actor
                ),
                self._pump_server_to_client(server_reader, client_writer),
                return_exceptions=True,
            )
        except (
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
            asyncio.CancelledError,
        ):
            pass
        except Exception as exc:  # noqa: BLE001 - one bad session, not the server
            log.exception("gateway session failed")
            self.stats.errors.append(str(exc))
        finally:
            _close(client_writer)
            _close(server_writer)

    # -- startup -----------------------------------------------------------

    async def _negotiate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Optional[protocol.StartupPacket]:
        """Read the startup packet, declining TLS if it is offered."""
        while True:
            packet = await self._read_startup(reader)
            if packet is None:
                return None
            if packet.is_ssl_request or packet.is_gssenc_request:
                # We have to read SQL, so we cannot pass an encrypted stream
                # through. Decline and let the client retry in plaintext.
                writer.write(protocol.SSL_DECLINED)
                await writer.drain()
                continue
            return packet

    async def _read_startup(
        self, reader: asyncio.StreamReader
    ) -> Optional[protocol.StartupPacket]:
        try:
            header = await reader.readexactly(STARTUP_HEADER)
        except asyncio.IncompleteReadError:
            return None
        length = struct.unpack("!I", header[:4])[0]
        if not STARTUP_HEADER <= length <= MAX_STARTUP_BYTES:
            raise protocol.ProtocolError(f"implausible startup length {length}")
        body = await reader.readexactly(length - STARTUP_HEADER)
        return protocol.parse_startup(header + body)

    async def _relay_authentication(
        self,
        client_reader: asyncio.StreamReader,
        server_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_writer: asyncio.StreamWriter,
        actor: Actor,
    ) -> bool:
        """Relay auth traffic in both directions until the session is ready.

        Authentication is a dialogue, not an announcement: the server issues a
        challenge and waits for the client's answer. Both directions therefore
        have to flow here, and we relay them without looking inside -- which is
        precisely why ``md5`` and ``SCRAM`` work (D2.4).

        The one thing held back is the first ``ReadyForQuery``. That lets us
        slip the attribution settings in while the client still believes the
        connection is being established; injecting them afterwards would
        produce a ``ReadyForQuery`` the client never asked for, and any client
        that counts them would desynchronise.
        """
        reader = protocol.MessageReader()
        from_client = asyncio.create_task(client_reader.read(65536))
        from_server = asyncio.create_task(server_reader.read(65536))

        try:
            while True:
                done, _ = await asyncio.wait(
                    {from_client, from_server},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if from_client in done:
                    data = from_client.result()
                    if not data:
                        return False
                    server_writer.write(data)
                    await server_writer.drain()
                    from_client = asyncio.create_task(client_reader.read(65536))

                if from_server in done:
                    data = from_server.result()
                    if not data:
                        return False
                    reader.feed(data)

                    released = False
                    for message in reader.messages():
                        if message.tag != protocol.READY_FOR_QUERY:
                            client_writer.write(message.encode())
                            continue
                        await client_writer.drain()
                        if self.attribute:
                            await self._inject_attribution(
                                server_reader, server_writer, reader, actor
                            )
                        client_writer.write(message.encode())
                        await client_writer.drain()
                        released = True
                        break
                    if released:
                        return True
                    await client_writer.drain()
                    from_server = asyncio.create_task(server_reader.read(65536))
        finally:
            for task in (from_client, from_server):
                if not task.done():
                    task.cancel()

    async def _inject_attribution(
        self,
        server_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
        reader: protocol.MessageReader,
        actor: Actor,
    ) -> None:
        """Set the actor for the session, swallowing the response.

        Session-scoped rather than transaction-scoped, so every statement on
        this connection carries it. Best effort: an upstream that rejects the
        call costs us attribution, not the session.

        Shares the caller's reader so that anything already buffered behind the
        held ``ReadyForQuery`` is consumed here rather than stranded.
        """
        assignments = ", ".join(
            f"set_config({_literal(key)}, {_literal(value)}, false)"
            for key, value in actor.as_settings().items()
        )
        server_writer.write(protocol.query(f"SELECT {assignments}"))
        await server_writer.drain()

        while True:
            for message in reader.messages():
                if message.tag == protocol.ERROR_RESPONSE:
                    log.warning("upstream refused the attribution settings")
                if message.tag == protocol.READY_FOR_QUERY:
                    return
            chunk = await server_reader.read(65536)
            if not chunk:
                return
            reader.feed(chunk)

    # -- steady state ------------------------------------------------------

    async def _pump_client_to_server(
        self,
        client_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
        client_writer: asyncio.StreamWriter,
        actor: Actor,
    ) -> None:
        reader = protocol.MessageReader()
        # After refusing an extended-protocol statement the backend would be in
        # error state, ignoring everything until Sync. We imitate that.
        in_error_state = False

        while True:
            chunk = await client_reader.read(65536)
            if not chunk:
                return
            reader.feed(chunk)

            for message in reader.messages():
                if in_error_state:
                    if message.tag == protocol.SYNC:
                        in_error_state = False
                        client_writer.write(protocol.ready_for_query())
                        await client_writer.drain()
                    continue

                verdict = self.interceptor.inspect(message, actor=actor.user)
                self.stats.statements = self.interceptor.evaluated
                self.stats.refused = self.interceptor.refused
                self.stats.failed_open = self.interceptor.failed_open

                if not verdict.refused:
                    server_writer.write(message.encode())
                    await server_writer.drain()
                    continue

                log.info(
                    "refused a %s from %s: %s",
                    verdict.decision.analysis.statement if verdict.decision else "?",
                    actor.user or "unknown",
                    verdict.decision.blockers[0] if verdict.decision else "",
                )
                client_writer.write(verdict.reply)
                if message.tag == protocol.QUERY:
                    # Simple protocol: the error ends the exchange.
                    client_writer.write(protocol.ready_for_query())
                else:
                    # Extended protocol: stay in error state until Sync.
                    in_error_state = True
                await client_writer.drain()

    async def _pump_server_to_client(
        self, server_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Everything the server says is relayed byte for byte."""
        while True:
            chunk = await server_reader.read(65536)
            if not chunk:
                return
            client_writer.write(chunk)
            await client_writer.drain()


# -- helpers ----------------------------------------------------------------


def _literal(value: str) -> str:
    """A single-quoted SQL literal.

    Only ever applied to actor fields, which are already bounded and stripped
    of newlines by ``Actor``; the doubling here is belt and braces.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _close(writer: Optional[asyncio.StreamWriter]) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:  # noqa: BLE001
        pass


async def run_gateway(
    listen: str,
    upstream_dsn: str,
    policy: Optional[Policy] = None,
    tracked: tuple[str, ...] = (),
    environment: str = "default",
) -> None:
    host, _, port = listen.rpartition(":")
    gateway = Gateway(
        Upstream.from_dsn(upstream_dsn),
        policy=policy,
        tracked=tracked,
        environment=environment,
    )
    await gateway.start(host or "127.0.0.1", int(port or 6543))
    await gateway.serve_forever()
