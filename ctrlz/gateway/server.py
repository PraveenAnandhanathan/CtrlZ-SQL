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
* **It is not required for undo.** Capture lives inside the database. Stop the
  gateway and every change is still recorded and still reversible.

The two hops are configured separately and mean different things. The client
hop is secured by giving the gateway a certificate (``ServerTLS``); without
one it declines ``SSLRequest`` and runs in plaintext, which is only reasonable
on localhost or a trusted segment. The database hop honours ``sslmode`` in the
upstream DSN.

Encrypting **both** hops does not work, and the gateway refuses to start in
that configuration rather than let it fail later at authentication. The reason
is worth stating plainly: an encrypted upstream makes PostgreSQL offer
``SCRAM-SHA-256-PLUS``, whose whole purpose is to bind authentication to one
specific TLS session so that a man-in-the-middle cannot relay it. There are two
TLS sessions here and the gateway is the thing in the middle, so the binding
data cannot match. That is channel binding working exactly as designed against
exactly the thing it was designed against. See ``strip_channel_binding`` for
the one deliberate way out.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from ..actor import GATEWAY, Actor
from ..policy import Policy
from . import protocol
from .interceptor import Interceptor
from .tls import ServerTLS, TLSConfigError

log = logging.getLogger("ctrlz.gateway")

STARTUP_HEADER = 8
MAX_STARTUP_BYTES = 1 << 20

#: How many client connections to serve at once.
#:
#: This is not about the gateway's own memory. The gateway opens **one upstream
#: database connection per client**, so without a cap it turns an unbounded
#: number of clients into an unbounded number of database connections and
#: exhausts the server's `max_connections` -- locking out everybody, including
#: clients that never went near the gateway.
#:
#: That is the one failure mode the whole design is supposed to make
#: impossible. Failing open covers a bug in the checkpoint; it does not cover
#: the checkpoint using up the resource it sits in front of. Set this below the
#: database's own limit, leaving room for direct connections and for whatever
#: else connects.
DEFAULT_MAX_CONNECTIONS = 100

#: Seconds a client has to get from "connected" to "authenticated".
#:
#: Without it, a connection that opens and then says nothing holds a task, a
#: socket and (once past the startup packet) a database connection for as long
#: as it likes. Ten of those are a nuisance; the cap above makes a hundred of
#: them an outage. Generous enough for a slow network and a SCRAM round trip.
DEFAULT_HANDSHAKE_TIMEOUT = 30.0


@dataclass
class Upstream:
    """Where to send traffic, parsed from a DSN."""

    host: str = "127.0.0.1"
    port: int = 5432
    #: The database named in the DSN, for logging. The client's own startup
    #: packet decides which database it gets; the gateway does not rewrite it.
    database: Optional[str] = None
    #: libpq's sslmode for the gateway-to-database hop. Defaults to
    #: ``disable``, which is *not* libpq's default, and the reason is
    #: important enough to state here rather than bury.
    #:
    #: If the upstream hop is encrypted and the client hop is not, PostgreSQL
    #: offers SCRAM-SHA-256-PLUS -- channel binding -- because from its side
    #: the connection is protected. The gateway relays that challenge verbatim
    #: to a client on a plaintext socket, and the client correctly refuses:
    #: "server offered SCRAM-SHA-256-PLUS authentication over a non-SSL
    #: connection".
    #:
    #: That is channel binding working as designed. It exists to stop exactly
    #: the thing a proxy does, and no amount of care on our side changes it.
    #: The only honest resolutions are to leave both hops in the same state
    #: (the default) or to secure the client hop too, which needs a
    #: certificate and is not something to half-build.
    #:
    #: Set ``?sslmode=require`` on the upstream DSN when the database demands
    #: encryption and does not use channel binding; the gateway warns.
    sslmode: str = "disable"

    @classmethod
    def from_dsn(cls, dsn: str) -> "Upstream":
        parsed = urlparse(dsn)
        database = unquote(parsed.path.lstrip("/")) or None
        options = parse_qs(parsed.query or "")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            database=database,
            sslmode=(options.get("sslmode") or ["disable"])[0].lower(),
        )

    @property
    def wants_tls(self) -> bool:
        return self.sslmode not in ("disable", "allow")

    @property
    def requires_tls(self) -> bool:
        """Whether a refusal to encrypt should end the connection."""
        return self.sslmode in ("require", "verify-ca", "verify-full")

    def ssl_context(self) -> Optional["ssl.SSLContext"]:
        """A context matching the requested sslmode.

        ``verify-full`` and ``verify-ca`` verify the chain; ``require`` and
        ``prefer`` encrypt without verifying, which is what libpq does and is
        stated plainly rather than presented as security it is not.
        """
        if not self.wants_tls:
            return None
        context = ssl.create_default_context()
        if self.sslmode != "verify-full":
            context.check_hostname = False
        if self.sslmode not in ("verify-ca", "verify-full"):
            context.verify_mode = ssl.CERT_NONE
        return context


@dataclass
class Stats:
    connections: int = 0
    statements: int = 0
    refused: int = 0
    failed_open: int = 0
    #: Refused because the gateway was already at max_connections.
    turned_away: int = 0
    #: Dropped for not completing the handshake in time.
    timed_out: int = 0
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
        tls: Optional[ServerTLS] = None,
        require_tls: bool = False,
        strip_channel_binding: bool = False,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    ):
        self.upstream = upstream
        self.interceptor = Interceptor(
            policy=policy, tracked=tracked, environment=environment
        )
        self.attribute = attribute
        self.tls = tls
        self.require_tls = require_tls
        self.strip_channel_binding = strip_channel_binding
        self.max_connections = max_connections
        self.handshake_timeout = handshake_timeout
        self.stats = Stats()
        self._tls_context: Optional[ssl.SSLContext] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: set[asyncio.Task] = set()

        if require_tls and tls is None:
            raise TLSConfigError(
                "--require-tls refuses every plaintext client, and without a "
                "certificate there is no encrypted alternative to offer, so no "
                "client could connect at all. Supply a certificate and key."
            )
        if tls is not None and upstream.wants_tls and not strip_channel_binding:
            # Refused here, at configuration time, rather than left to fail at
            # authentication -- where the error surfaces on the client and says
            # nothing about the gateway that caused it.
            raise TLSConfigError(
                f"both hops cannot be encrypted: a client certificate is "
                f"configured and the upstream DSN says sslmode="
                f"{upstream.sslmode}.\n"
                f"  PostgreSQL will offer SCRAM-SHA-256-PLUS, whose channel "
                f"binding ties authentication to one TLS session. There are two "
                f"here, so clients will fail to authenticate.\n"
                f"  Encrypt the hop that crosses untrusted network and leave the "
                f"other plaintext -- usually: keep the certificate, set "
                f"sslmode=disable upstream.\n"
                f"  If both hops genuinely must be encrypted, "
                f"--strip-channel-binding removes SCRAM-SHA-256-PLUS from the "
                f"server's offer. Read what that costs before using it."
            )

    # -- lifecycle ---------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 6543) -> int:
        # Load certificates before binding the port. A path that does not
        # resolve should fail while somebody is still watching the terminal,
        # not on the first connection an hour later.
        self.reload_tls()

        self._server = await asyncio.start_server(self._handle, host, port)
        bound = self._server.sockets[0].getsockname()[1]
        log.info(
            "ctrlz gateway listening on %s:%s -> %s:%s",
            host, bound, self.upstream.host, self.upstream.port,
        )
        if self.tls is not None:
            log.info(
                "client connections may use TLS (%s)%s",
                self.tls.describe(),
                "; plaintext refused" if self.require_tls else "",
            )
        else:
            log.warning(
                "client connections are plaintext: no certificate configured, "
                "so SSLRequest is declined and SQL and credentials cross the "
                "network in the clear. Bind to localhost or a trusted segment, "
                "or pass --tls-cert and --tls-key."
            )
        if self.upstream.wants_tls and self.tls is None:
            log.warning(
                "upstream sslmode=%s encrypts the database hop while the client "
                "hop stays plaintext. If the server offers SCRAM-SHA-256-PLUS, "
                "clients will refuse to authenticate -- channel binding is meant "
                "to stop proxies, and this is one.",
                self.upstream.sslmode,
            )
        if self.strip_channel_binding:
            log.warning(
                "--strip-channel-binding is on: SCRAM-SHA-256-PLUS is being "
                "removed from the server's offer, so clients authenticate "
                "without binding the exchange to their TLS session. Both hops "
                "stay encrypted; what is given up is the proof that nothing "
                "sits between them -- and something does. This one."
            )
        return bound

    def reload_tls(self) -> None:
        """Rebuild the TLS context from the files on disk.

        Certificates expire, and the ones that do not expire get rotated. A
        proxy in the connection path that can only pick up a renewal by
        restarting drops every open session to do it, every sixty days.

        The new context is built first and swapped in only if it loads, so a
        reload with a half-written file leaves the running one serving.
        Connections already established keep the context they started with;
        that is how TLS works, and pretending otherwise would be worse.
        """
        if self.tls is None:
            self._tls_context = None
            return
        context = self.tls.context()      # raises before anything is replaced
        replacing = self._tls_context is not None
        self._tls_context = context
        if replacing:
            log.info("TLS certificate reloaded (%s)", self.tls.describe())

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
            # Checked before anything upstream is opened. Refusing a client is
            # a nuisance; opening the connection first and refusing afterwards
            # would spend the database resource we are trying to protect.
            if not self._admit(client_writer):
                return

            # The handshake is bounded because a client that connects and then
            # says nothing would otherwise hold a slot indefinitely, and a held
            # slot is one the cap above has already counted.
            negotiated = await asyncio.wait_for(
                self._negotiate(client_reader, client_writer),
                timeout=self.handshake_timeout,
            )
            if negotiated is None:
                return
            # A TLS upgrade replaces the stream pair, so rebind rather than
            # carry on writing to the plaintext one.
            startup, client_reader, client_writer = negotiated

            server_reader, server_writer = await self._connect_upstream()

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

            ready = await asyncio.wait_for(
                self._relay_authentication(
                    client_reader, server_reader, client_writer, server_writer, actor
                ),
                timeout=self.handshake_timeout,
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
        except asyncio.TimeoutError:
            self.stats.timed_out += 1
            log.info("dropped a connection that did not finish authenticating")
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

    def _admit(self, client_writer: asyncio.StreamWriter) -> bool:
        """Whether there is room for this client.

        `_sessions` already contains the task handling this connection, so the
        comparison is against the limit itself rather than one below it.

        A refusal is a protocol-native error carrying PostgreSQL's own
        `53300`, which connection pools and clients already understand as
        "retry later" rather than as a fatal condition.
        """
        if self.max_connections <= 0 or len(self._sessions) <= self.max_connections:
            return True

        self.stats.turned_away += 1
        log.warning(
            "refused a connection: already serving %s, the limit is %s",
            len(self._sessions) - 1, self.max_connections,
        )
        try:
            client_writer.write(
                protocol.error_response(
                    "too many connections for this ctrlz gateway",
                    sqlstate=protocol.SQLSTATE_TOO_MANY,
                    detail=f"the gateway is serving its limit of "
                           f"{self.max_connections} connections",
                    hint="retry shortly, or raise --max-connections (keeping it "
                         "below the database's own limit)",
                )
            )
        except Exception:  # noqa: BLE001 - the client may already be gone
            pass
        return False

    async def _connect_upstream(self):
        """Open the upstream connection, negotiating TLS if asked for.

        PostgreSQL does not accept TLS on connect: the client sends an
        SSLRequest in the clear and the server answers with a single byte,
        after which the socket is upgraded. So the handshake has to be driven
        here rather than handed to ``open_connection(ssl=...)``.
        """
        reader, writer = await asyncio.open_connection(
            self.upstream.host, self.upstream.port
        )
        context = self.upstream.ssl_context()
        if context is None:
            return reader, writer

        writer.write(struct.pack("!II", 8, protocol.SSL_REQUEST))
        await writer.drain()
        answer = await reader.readexactly(1)

        if answer != b"S":
            if self.upstream.requires_tls:
                _close(writer)
                raise protocol.ProtocolError(
                    f"upstream refused TLS but sslmode={self.upstream.sslmode} "
                    f"requires it"
                )
            log.info("upstream declined TLS; continuing in plaintext")
            return reader, writer

        transport = writer.transport
        protocol_obj = transport.get_protocol()
        loop = asyncio.get_running_loop()
        new_transport = await loop.start_tls(
            transport,
            protocol_obj,
            context,
            server_hostname=self.upstream.host
            if self.upstream.sslmode == "verify-full"
            else None,
        )
        protocol_obj._stream_reader._transport = new_transport
        writer._transport = new_transport
        log.info("upstream connection encrypted (sslmode=%s)", self.upstream.sslmode)
        return reader, writer

    # -- startup -----------------------------------------------------------

    async def _negotiate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Optional[tuple[protocol.StartupPacket, asyncio.StreamReader,
                        asyncio.StreamWriter]]:
        """Read the startup packet, upgrading to TLS if both sides want it.

        Returns the reader and writer to carry on with, because after a TLS
        upgrade they are not the ones we were handed.
        """
        encrypted = False
        while True:
            packet = await self._read_startup(reader)
            if packet is None:
                return None

            if packet.is_gssenc_request:
                # GSSAPI encryption is a separate mechanism we do not speak.
                # Declining is the protocol's own way of saying so, and the
                # client falls back without an error.
                writer.write(protocol.SSL_DECLINED)
                await writer.drain()
                continue

            if packet.is_ssl_request:
                if self._tls_context is None:
                    writer.write(protocol.SSL_DECLINED)
                    await writer.drain()
                    continue
                reader, writer = await self._accept_tls(reader, writer)
                if writer is None:
                    return None
                encrypted = True
                continue

            if self.require_tls and not encrypted and not packet.is_cancel_request:
                # hostssl: the client asked to speak in the clear and this
                # gateway does not. Say so in a message a client will render,
                # rather than dropping the socket and leaving them guessing.
                writer.write(
                    protocol.error_response(
                        "this ctrlz gateway requires TLS",
                        detail="the connection was attempted without encryption",
                        hint="connect with sslmode=require (or higher)",
                    )
                )
                await writer.drain()
                log.info("refused a plaintext connection (--require-tls)")
                return None

            return packet, reader, writer

    async def _accept_tls(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Answer SSLRequest with 'S' and upgrade the socket in place.

        PostgreSQL does not do TLS on connect: the request arrives in the clear
        and a single byte answers it, after which both sides start a handshake
        on the same socket. asyncio has no stream-level API for that, so the
        transport is swapped underneath the reader and writer -- the mirror of
        what `_connect_upstream` does on the other side.
        """
        writer.write(protocol.SSL_ACCEPTED)
        await writer.drain()

        transport = writer.transport
        protocol_obj = transport.get_protocol()
        loop = asyncio.get_running_loop()
        try:
            new_transport = await loop.start_tls(
                transport, protocol_obj, self._tls_context, server_side=True
            )
        except (ssl.SSLError, ConnectionResetError, asyncio.IncompleteReadError) as exc:
            # A failed handshake is the client's business -- a bad certificate,
            # no certificate where one is required, a version floor they cannot
            # meet. It is not a gateway fault and must not take the server down.
            log.info("TLS handshake with a client failed: %s", exc)
            _close(writer)
            return reader, None

        protocol_obj._stream_reader._transport = new_transport
        writer._transport = new_transport
        _no_half_close(protocol_obj)
        return reader, writer

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
                            if self.strip_channel_binding:
                                message, stripped = protocol.without_channel_binding(
                                    message
                                )
                                if stripped:
                                    log.info(
                                        "removed SCRAM-SHA-256-PLUS from the "
                                        "server's offer for %s",
                                        actor.user or "an unnamed user",
                                    )
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


def _no_half_close(protocol_obj) -> None:
    """Stop asyncio warning once per TLS connection about a half-close.

    ``StreamReaderProtocol.eof_received`` returns True, meaning "the peer is
    done writing but keep the transport open, I may still write". TLS has no
    half-close, so asyncio logs a warning and closes anyway -- every time a TLS
    session ends. Harmless, and a warning per connection is the kind of noise
    that gets a logger turned off, taking the useful messages with it.

    Returning False is also what we actually mean: when one side of a proxied
    session goes away, the session is over. The reader must still be told about
    the EOF, so the original is called for its effect and only the answer
    changes.
    """
    original = protocol_obj.eof_received

    def eof_received() -> bool:
        original()
        return False

    protocol_obj.eof_received = eof_received


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
    tls: Optional[ServerTLS] = None,
    require_tls: bool = False,
    strip_channel_binding: bool = False,
) -> None:
    host, _, port = listen.rpartition(":")
    gateway = Gateway(
        Upstream.from_dsn(upstream_dsn),
        policy=policy,
        tracked=tracked,
        environment=environment,
        tls=tls,
        require_tls=require_tls,
        strip_channel_binding=strip_channel_binding,
    )
    await gateway.start(host or "127.0.0.1", int(port or 6543))
    _reload_on_sighup(gateway)
    await gateway.serve_forever()


def _reload_on_sighup(gateway: Gateway) -> None:
    """Reload certificates on SIGHUP, the convention every server already uses.

    Without it a renewal costs a restart, and a restart in the connection path
    costs every open session. Best effort: a platform with no SIGHUP, or a loop
    that will not take a signal handler, loses the convenience and nothing else.
    """
    if gateway.tls is None:
        return
    import signal

    def reload() -> None:
        try:
            gateway.reload_tls()
        except TLSConfigError as exc:
            # Keep serving with the certificate we have. A failed reload is a
            # bad deploy, not a reason to stop answering.
            log.error("TLS reload failed, keeping the previous certificate: %s", exc)

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload)
        log.info("send SIGHUP to reload the TLS certificate")
    except (NotImplementedError, AttributeError, ValueError):
        pass
