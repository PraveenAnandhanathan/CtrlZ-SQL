"""Client-facing TLS, proved against a real `psql` over a real handshake.

The gateway used to decline every `SSLRequest`, so the hop most likely to cross
untrusted network was the one hop that was never encrypted. These tests hold
the fix to the only standard that means anything for TLS: an unmodified client,
negotiating for itself, refusing what it should refuse.

Certificates are generated with the `openssl` binary rather than a Python
library, so the tests depend on nothing the runtime does not already need.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess

import pytest

from ctrlz.gateway import Gateway, ServerTLS, TLSConfigError, Upstream

# `sandbox` is a fixture; importing it binds it into this module's namespace,
# which is where pytest looks.
from .test_gateway import PG_DSN, RunningGateway, sandbox  # noqa: F401

pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="set CTRLZ_TEST_PG_DSN to run the gateway tests"
)

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is not installed"
)
requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed"
)


# -- certificate material --------------------------------------------------


def make_cert(directory, name: str = "server", common_name: str = "localhost",
              ca: tuple | None = None) -> tuple[str, str]:
    """A real certificate and key. Returns (certfile, keyfile)."""
    cert = str(directory / f"{name}.crt")
    key = str(directory / f"{name}.key")

    if ca is None:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", cert, "-days", "1",
             "-subj", f"/CN={common_name}",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )
    else:
        ca_cert, ca_key = ca
        csr = str(directory / f"{name}.csr")
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", csr, "-subj", f"/CN={common_name}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", csr, "-CA", ca_cert,
             "-CAkey", ca_key, "-CAcreateserial", "-out", cert, "-days", "1"],
            check=True, capture_output=True,
        )

    os.chmod(key, 0o600)
    return cert, key


@pytest.fixture
def certs(tmp_path):
    return make_cert(tmp_path)


# -- configuration is checked before anything binds a port -----------------


def test_a_world_readable_private_key_is_refused(tmp_path):
    """PostgreSQL will not start like this, and neither should a proxy holding
    the same material."""
    cert, key = make_cert(tmp_path)
    os.chmod(key, 0o644)

    with pytest.raises(TLSConfigError) as caught:
        ServerTLS(certfile=cert, keyfile=key).context()

    message = str(caught.value)
    assert "accessible to other users" in message
    assert "chmod 600" in message


def test_an_insecure_key_can_be_allowed_deliberately(tmp_path):
    """A secrets mount may set permissions the pod does not control.

    Refusing to start would be the wrong answer there, but it has to be asked
    for by name rather than happening quietly.
    """
    cert, key = make_cert(tmp_path)
    os.chmod(key, 0o644)

    context = ServerTLS(
        certfile=cert, keyfile=key, allow_insecure_key_permissions=True
    ).context()
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("missing", ["certfile", "keyfile"])
def test_a_missing_file_is_named(tmp_path, certs, missing):
    cert, key = certs
    fields = {"certfile": cert, "keyfile": key}
    fields[missing] = str(tmp_path / "nope.pem")

    with pytest.raises(TLSConfigError) as caught:
        ServerTLS(**fields).context()
    assert "nope.pem" in str(caught.value)


def test_a_mismatched_certificate_and_key_are_refused(tmp_path):
    cert, _ = make_cert(tmp_path, name="one")
    _, key = make_cert(tmp_path, name="two")

    with pytest.raises(TLSConfigError) as caught:
        ServerTLS(certfile=cert, keyfile=key).context()
    assert "could not load" in str(caught.value)


def test_requiring_a_client_certificate_needs_a_ca(certs):
    cert, key = certs
    with pytest.raises(TLSConfigError) as caught:
        ServerTLS(certfile=cert, keyfile=key, require_client_cert=True)
    assert "needs a CA" in str(caught.value)


def test_tls_one_two_is_the_floor(certs):
    """1.0 and 1.1 are withdrawn; offering them only helps a downgrade."""
    cert, key = certs
    context = ServerTLS(certfile=cert, keyfile=key).context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_requiring_tls_without_a_certificate_is_refused():
    """Otherwise the gateway would reject plaintext and offer no alternative,
    so nothing at all could connect."""
    with pytest.raises(TLSConfigError) as caught:
        Gateway(Upstream.from_dsn(PG_DSN), require_tls=True)
    assert "no encrypted alternative" in str(caught.value)


def test_encrypting_both_hops_is_refused_at_configuration_time(certs):
    """The failure this prevents happens at authentication, on the client, with
    a message that never mentions the gateway."""
    cert, key = certs
    with pytest.raises(TLSConfigError) as caught:
        Gateway(
            Upstream.from_dsn("postgresql://h/db?sslmode=require"),
            tls=ServerTLS(certfile=cert, keyfile=key),
        )
    message = str(caught.value)
    assert "SCRAM-SHA-256-PLUS" in message
    assert "--strip-channel-binding" in message


def test_both_hops_are_allowed_once_the_downgrade_is_explicit(certs):
    cert, key = certs
    gateway = Gateway(
        Upstream.from_dsn("postgresql://h/db?sslmode=require"),
        tls=ServerTLS(certfile=cert, keyfile=key),
        strip_channel_binding=True,
    )
    assert gateway.strip_channel_binding is True


# -- a real client, a real handshake ---------------------------------------


@requires_openssl
@requires_psql
def test_psql_connects_over_tls_and_queries(sandbox, certs):
    """The whole point: an unmodified client, encrypted, still proxied."""
    cert, key = certs
    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        result = psql_tls(gw.port, "require",
                          f"SELECT name FROM {sandbox.schema}.users ORDER BY id")

    assert result.returncode == 0, result.stderr
    assert "ada" in result.stdout


@requires_openssl
@requires_psql
def test_the_rulebook_still_applies_over_tls(sandbox, certs):
    """Encryption must not become a way around the checkpoint."""
    cert, key = certs
    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        refused = psql_tls(gw.port, "require", f"DELETE FROM {sandbox.schema}.users")
        survived = psql_tls(gw.port, "require",
                            f"SELECT count(*) FROM {sandbox.schema}.users")

    # Refused by the rulebook specifically -- not merely "something went
    # wrong", which a TLS fault would also satisfy. (psql prints the SQLSTATE
    # only under VERBOSITY verbose, so the rule name is the identifying part.)
    assert "unfiltered-write" in refused.stderr, refused.stderr
    assert survived.stdout.strip() == "2", "the rows must still be there"


@requires_openssl
@requires_psql
def test_a_client_that_wants_plaintext_is_still_served_by_default(sandbox, certs):
    """Offering TLS is not the same as demanding it."""
    cert, key = certs
    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        result = psql_tls(gw.port, "disable",
                          f"SELECT count(*) FROM {sandbox.schema}.users")
    assert result.returncode == 0, result.stderr


@requires_openssl
@requires_psql
def test_require_tls_refuses_a_plaintext_client(sandbox, certs):
    """hostssl. The refusal must be a message the client renders, not a
    dropped socket the user has to guess at."""
    cert, key = certs
    with RunningGateway(
        sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key), require_tls=True
    ) as gw:
        refused = psql_tls(gw.port, "disable", "SELECT 1")
        allowed = psql_tls(gw.port, "require", "SELECT 1")

    assert refused.returncode != 0
    assert "requires TLS" in refused.stderr
    assert allowed.returncode == 0, allowed.stderr


@requires_openssl
@requires_psql
def test_verify_full_succeeds_against_our_certificate(sandbox, tmp_path):
    """`require` encrypts; only `verify-full` proves who answered.

    A client checking the hostname against the certificate is the strongest
    statement available that the gateway is what it claims to be, so it is
    worth proving rather than assuming.
    """
    cert, key = make_cert(tmp_path, common_name="localhost")
    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        result = psql_tls(gw.port, "verify-full", "SELECT 1",
                          root_cert=cert, host="localhost")
    assert result.returncode == 0, result.stderr


@requires_openssl
@requires_psql
def test_verify_full_fails_against_an_unrelated_certificate(sandbox, tmp_path):
    """The negative case, because a verification test that cannot fail proves
    nothing about verification."""
    cert, key = make_cert(tmp_path, name="server", common_name="localhost")
    other, _ = make_cert(tmp_path, name="other", common_name="localhost")

    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        result = psql_tls(gw.port, "verify-full", "SELECT 1",
                          root_cert=other, host="localhost")

    assert result.returncode != 0
    assert "certificate" in result.stderr.lower()


@requires_openssl
@requires_psql
def test_a_failed_handshake_does_not_stop_the_gateway(sandbox, tmp_path):
    """Fail open, as everywhere else: one bad client is not an outage."""
    cert, key = make_cert(tmp_path, common_name="localhost")
    other, _ = make_cert(tmp_path, name="other")

    with RunningGateway(sandbox.dsn, tls=ServerTLS(certfile=cert, keyfile=key)) as gw:
        rejected = psql_tls(gw.port, "verify-full", "SELECT 1",
                            root_cert=other, host="localhost")
        assert rejected.returncode != 0

        # The next client must be served as if nothing happened.
        after = psql_tls(gw.port, "require",
                         f"SELECT count(*) FROM {sandbox.schema}.users")
    assert after.returncode == 0, after.stderr


# -- mutual TLS ------------------------------------------------------------


@requires_openssl
@requires_psql
def test_a_required_client_certificate_is_enforced(sandbox, tmp_path):
    """A client with a certificate gets in; one without is refused.

    The positive half runs through a real `psql`, because that is the client
    people will actually use. The negative half does the handshake directly --
    see `tls_handshake` for why asserting on psql's wording was a mistake.
    """
    ca_cert, ca_key = make_cert(tmp_path, name="ca", common_name="ctrlz-test-ca")
    server_cert, server_key = make_cert(
        tmp_path, name="server", common_name="localhost", ca=(ca_cert, ca_key)
    )
    client_cert, client_key = make_cert(
        tmp_path, name="client", common_name="ada", ca=(ca_cert, ca_key)
    )

    tls = ServerTLS(
        certfile=server_cert, keyfile=server_key,
        ca_file=ca_cert, require_client_cert=True,
    )
    with RunningGateway(sandbox.dsn, tls=tls) as gw:
        with_cert = psql_tls(gw.port, "require", "SELECT 1",
                             client_cert=client_cert, client_key=client_key)
        assert with_cert.returncode == 0, with_cert.stderr

        # No certificate: the handshake itself must fail, which is the only
        # place it can -- there is no way to send an application-level error
        # before TLS is up.
        with pytest.raises(HandshakeRefused):
            tls_handshake(gw.port)

        # And the same handshake with a certificate completes, so the failure
        # above is attributable to the missing certificate and nothing else.
        tls_handshake(gw.port, client_cert=client_cert, client_key=client_key)


@requires_openssl
def test_client_certificates_are_optional_unless_required(sandbox, tmp_path):
    """A CA without --require-client-cert accepts clients that present none."""
    ca_cert, ca_key = make_cert(tmp_path, name="ca", common_name="ctrlz-test-ca")
    server_cert, server_key = make_cert(
        tmp_path, name="server", common_name="localhost", ca=(ca_cert, ca_key)
    )
    tls = ServerTLS(certfile=server_cert, keyfile=server_key, ca_file=ca_cert)

    with RunningGateway(sandbox.dsn, tls=tls) as gw:
        tls_handshake(gw.port)        # must not raise


# -- reload ----------------------------------------------------------------


@requires_openssl
def test_reloading_picks_up_a_new_certificate(tmp_path):
    """Certificates get renewed. A proxy that can only notice by restarting
    drops every open session to do it."""
    cert, key = make_cert(tmp_path, common_name="first")
    gateway = Gateway(
        Upstream.from_dsn(PG_DSN), tls=ServerTLS(certfile=cert, keyfile=key)
    )
    gateway.reload_tls()
    first = gateway._tls_context
    assert first is not None

    make_cert(tmp_path, name="server", common_name="second")   # same paths
    gateway.reload_tls()
    assert gateway._tls_context is not first


@requires_openssl
def test_a_broken_reload_keeps_the_running_certificate(tmp_path):
    """Half a certificate on disk during a deploy must not end the service."""
    cert, key = make_cert(tmp_path)
    gateway = Gateway(
        Upstream.from_dsn(PG_DSN), tls=ServerTLS(certfile=cert, keyfile=key)
    )
    gateway.reload_tls()
    working = gateway._tls_context

    with open(cert, "w") as handle:
        handle.write("-----BEGIN CERTIFICATE-----\nnot a certificate\n")

    with pytest.raises(TLSConfigError):
        gateway.reload_tls()
    assert gateway._tls_context is working, "the old context must survive"


# -- channel binding -------------------------------------------------------


def test_stripping_removes_only_the_plus_variant():
    from ctrlz.gateway import protocol as p

    message = sasl_offer([b"SCRAM-SHA-256-PLUS", b"SCRAM-SHA-256"])
    stripped, changed = p.without_channel_binding(message)

    assert changed is True
    assert p.sasl_mechanisms(stripped) == [b"SCRAM-SHA-256"]


def test_an_offer_without_channel_binding_is_untouched():
    from ctrlz.gateway import protocol as p

    message = sasl_offer([b"SCRAM-SHA-256"])
    stripped, changed = p.without_channel_binding(message)

    assert changed is False
    assert stripped is message


def test_an_offer_of_only_channel_binding_is_passed_through():
    """Stripping the last mechanism would send an empty list.

    That is not a weaker negotiation, it is a malformed message -- turning a
    refusal the client can explain into a parse error it cannot.
    """
    from ctrlz.gateway import protocol as p

    message = sasl_offer([b"SCRAM-SHA-256-PLUS"])
    stripped, changed = p.without_channel_binding(message)

    assert changed is False
    assert stripped is message


@pytest.mark.parametrize(
    "message",
    [
        None,                       # a non-authentication message
        b"\x00\x00\x00\x00",        # AuthenticationOk, not SASL
        b"\x00\x00",                # truncated
        b"",
    ],
)
def test_stripping_ignores_anything_that_is_not_a_sasl_offer(message):
    from ctrlz.gateway import protocol as p

    if message is None:
        subject = p.Message(p.READY_FOR_QUERY, b"I")
    else:
        subject = p.Message(p.AUTHENTICATION, message)

    result, changed = p.without_channel_binding(subject)
    assert changed is False
    assert result is subject


def sasl_offer(mechanisms: list[bytes]):
    import struct

    from ctrlz.gateway import protocol as p

    payload = struct.pack("!I", p.AUTH_SASL)
    payload += b"".join(name + b"\x00" for name in mechanisms) + b"\x00"
    return p.Message(p.AUTHENTICATION, payload)


# -- doing the handshake ourselves -----------------------------------------


class HandshakeRefused(Exception):
    """The gateway would not carry on with this TLS connection."""


def tls_handshake(port: int, client_cert: str = "", client_key: str = "") -> None:
    """Drive PostgreSQL's SSLRequest dance and see whether we are let in.

    Exists because asserting on `psql`'s error text was a mistake I made and
    CI caught. A rejected handshake is reported by libpq as "SSL SYSCALL
    error: EOF detected" on one build and "server closed the connection
    unexpectedly" on another. Both are correct; a test pinned to either passes
    for the right reason on one machine and fails on the other. That wording
    belongs to libpq, not to us.

    Detecting the refusal is subtler than "an exception is raised", which was
    my second mistake. Under TLS 1.3 the server asks for a client certificate
    *after* the client believes the handshake is finished, so a client with
    none completes `wrap_socket` happily and learns of the rejection as a
    clean end-of-stream. Refusal therefore means any of: an SSL alert, a
    reset, or EOF with nothing read.

    Acceptance is the read *timing out* -- the gateway is holding the
    connection open waiting for a startup packet, which is exactly what being
    let in looks like.
    """
    import socket
    import struct

    from ctrlz.gateway import protocol as p

    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    wrapped = None
    try:
        sock.sendall(struct.pack("!II", 8, p.SSL_REQUEST))
        answer = sock.recv(1)
        assert answer == p.SSL_ACCEPTED, f"gateway declined TLS: {answer!r}"

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if client_cert:
            context.load_cert_chain(client_cert, client_key)

        try:
            wrapped = context.wrap_socket(sock)
        except ssl.SSLError as exc:
            raise HandshakeRefused(f"rejected during the handshake: {exc}") from exc

        wrapped.settimeout(3)
        try:
            if wrapped.recv(1) == b"":
                raise HandshakeRefused("connection closed with nothing sent")
        except socket.timeout:
            return          # held open, waiting for a startup packet: accepted
        except ssl.SSLError as exc:
            raise HandshakeRefused(f"rejected after the handshake: {exc}") from exc
    except (ConnectionResetError, BrokenPipeError) as exc:
        raise HandshakeRefused(f"connection reset: {exc}") from exc
    finally:
        for handle in (wrapped, sock):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass


# -- driving psql over TLS -------------------------------------------------


def psql_tls(port: int, sslmode: str, *statements: str, root_cert: str = "",
             client_cert: str = "", client_key: str = "",
             host: str = "127.0.0.1") -> subprocess.CompletedProcess:
    """`psql`, told to negotiate TLS for itself.

    Everything about the encryption is decided by libpq, not by us. That is the
    point: the gateway is only proved by a client that would have refused had
    we got it wrong.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(PG_DSN)
    env = dict(os.environ, PGPASSWORD=parsed.password or "", PGSSLMODE=sslmode)
    if root_cert:
        env["PGSSLROOTCERT"] = root_cert
    if client_cert:
        env["PGSSLCERT"] = client_cert
        env["PGSSLKEY"] = client_key

    argv = [
        "psql", "-h", host, "-p", str(port),
        "-U", parsed.username or "postgres",
        "-d", parsed.path.lstrip("/"),
        "-v", "ON_ERROR_STOP=0", "-t", "-A",
    ]
    for statement in statements:
        argv += ["-c", statement]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
