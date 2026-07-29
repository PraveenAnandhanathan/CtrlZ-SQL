"""Certificate handling for the client-facing hop.

The gateway has always encrypted the hop to the database and never the hop from
the client, which is the wrong way round: the database is usually the near end,
on a trusted segment, and the client is the far end, on whatever network the
user happens to be on. Until now the answer was "bind it to localhost", which
is honest but small.

This module holds everything about certificates, so that `server.py` deals in
"do I have a context" and nothing more.

What is deliberately strict:

* **A private key readable by anyone else is refused.** PostgreSQL itself will
  not start with a group- or world-readable key, and a proxy that holds the
  same material should not be more relaxed than the thing it sits in front of.
* **TLS 1.2 is the floor.** 1.0 and 1.1 are withdrawn; offering them to be
  accommodating would only accommodate an attacker downgrading the session.
* **Certificates are loaded eagerly.** A path that does not resolve fails at
  startup, where somebody is watching, rather than at the first connection.
"""

from __future__ import annotations

import logging
import os
import ssl
import stat
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("ctrlz.gateway")

#: Permission bits that must not be set on a private key: group and other, in
#: any combination. Matches PostgreSQL's own rule for `ssl_key_file`.
KEY_MUST_NOT_BE_ACCESSIBLE_TO = stat.S_IRWXG | stat.S_IRWXO


class TLSConfigError(Exception):
    """The certificate material cannot be used, and we will not guess."""


@dataclass
class ServerTLS:
    """Certificate material for connections coming *from* clients.

    Separate from the upstream `sslmode` on purpose. The two hops are different
    trust decisions and conflating them is how you end up encrypting the one
    that was already safe.
    """

    certfile: str
    keyfile: str
    #: A CA bundle used to verify *client* certificates. Supplying one turns on
    #: mutual TLS; without it, clients are not asked for a certificate.
    ca_file: Optional[str] = None
    #: Whether a client that presents no certificate is rejected. Only
    #: meaningful with `ca_file`, and checked rather than assumed.
    require_client_cert: bool = False
    #: Skip the private key permission check. Exists because a key delivered by
    #: a secrets mount can arrive with permissions the pod does not control;
    #: refusing to start would then be the wrong answer. Named so that nobody
    #: sets it without knowing what they are giving up.
    allow_insecure_key_permissions: bool = False

    def __post_init__(self) -> None:
        if self.require_client_cert and not self.ca_file:
            raise TLSConfigError(
                "requiring a client certificate needs a CA to verify it against; "
                "pass a CA bundle as well, or drop the requirement"
            )

    def context(self) -> ssl.SSLContext:
        """Build the server-side context, refusing anything unsafe.

        Called again on every reload, so it must be safe to call repeatedly and
        must never leave a half-configured context behind: a fresh one is built
        and only swapped in by the caller once it is complete.
        """
        for label, path in (("certificate", self.certfile), ("private key", self.keyfile)):
            if not os.path.exists(path):
                raise TLSConfigError(f"TLS {label} not found: {path}")

        self._check_key_permissions()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(self.certfile, self.keyfile)
        except ssl.SSLError as exc:
            raise TLSConfigError(
                f"could not load the TLS certificate and key: {exc}. "
                f"A mismatched pair and an encrypted key both look like this."
            ) from exc

        if self.ca_file:
            if not os.path.exists(self.ca_file):
                raise TLSConfigError(f"TLS CA bundle not found: {self.ca_file}")
            context.load_verify_locations(self.ca_file)
            context.verify_mode = (
                ssl.CERT_REQUIRED if self.require_client_cert else ssl.CERT_OPTIONAL
            )
        return context

    def _check_key_permissions(self) -> None:
        """Refuse a private key that other users on the host can read."""
        if self.allow_insecure_key_permissions or os.name != "posix":
            return
        mode = os.stat(self.keyfile).st_mode
        if mode & KEY_MUST_NOT_BE_ACCESSIBLE_TO:
            raise TLSConfigError(
                f"private key {self.keyfile} is accessible to other users "
                f"(mode {stat.filemode(mode)}). PostgreSQL refuses to start in "
                f"this state and so does ctrlz.\n"
                f"  Fix it with:  chmod 600 {self.keyfile}\n"
                f"  Or, if the permissions are set by a secrets mount you do "
                f"not control, pass --tls-allow-insecure-key."
            )

    def describe(self) -> str:
        """One line for the startup log. Never the key's contents or path bits."""
        parts = [f"cert={os.path.basename(self.certfile)}"]
        if self.ca_file:
            parts.append(
                "client certs "
                + ("required" if self.require_client_cert else "accepted")
            )
        return ", ".join(parts)
