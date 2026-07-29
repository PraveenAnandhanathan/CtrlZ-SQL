# ctrlz — Phase 2 Task Breakdown

**Status:** complete
**Scope:** Phase 2 (gateway + SDK wrapper) — decisions D-2, D2.1–D2.6
**Author:** Praveen Anandhanathan
**Co-author:** Claude
**Companion documents:** [`spec.md`](./spec.md) · [`plan.md`](./plan.md) · [`tasks.md`](./tasks.md)

---

## What Phase 2 delivers

> 🟢 **In plain terms**
> Phase 1 taught the tool to read SQL and gave it a rulebook, but only people
> who type `ctrlz run` ever benefit — which is almost nobody. Phase 2 puts a
> checkpoint on the door instead. You point `psql`, DBeaver, your BI tool or
> your application at the checkpoint rather than at the database, change nothing
> else, and dangerous statements get stopped with a clear explanation before the
> database ever sees them.
>
> Undo still does not depend on any of it. If the checkpoint is switched off,
> capture carries on exactly as before.

Two doors onto the same rulebook (D-2):

| Component | Who it serves |
|---|---|
| `ctrlz/gateway/` | any client that can be pointed at a different host and port |
| `ctrlz/sdk/` | Python applications that cannot re-point their connection string |

Both call the **same** `ctrlz.policy` evaluator (D2.6). Two implementations of
the same rules would drift, and a safety rule that holds at one door but not
the other is worse than no rule at all.

---

## Commit plan

| # | Commit | Tasks |
|---|---|---|
| P1 | PostgreSQL wire-protocol codec | G1–G4 |
| P2 | Gateway server: startup, auth relay, message pump | G5–G8 |
| P3 | Policy interception and attribution injection | G9–G12 |
| P4 | Python SDK wrapper | S1–S4 |
| P5 | CLI, docs, benchmarks | X1–X4 |

---

## P1 — Wire protocol

### G1 · Message framing
`ctrlz/gateway/protocol.py`

Backend and frontend messages share a shape after startup: a one-byte type tag,
an `int32` length that **includes itself**, then the payload. The startup packet
is the exception — no tag, because at that point neither side knows what the
other is yet.

**Done when:** round-trip encode/decode for every message we construct, and a
reader that handles a message split across TCP reads.

### G2 · Startup packet parsing
Distinguish `StartupMessage`, `SSLRequest` (80877103), `CancelRequest`
(80877102) and `GSSENCRequest` (80877104) by the version field.

**Done when:** parameters (`user`, `database`, `application_name`) are extracted
for attribution, and the packet is reproduced byte-identically for forwarding.

### G3 · ErrorResponse construction
A blocked statement must come back as a **protocol-native error** (D2.3), so
`psql` renders it the way it renders any database error — no plugin, no special
client.

Fields: severity, SQLSTATE, message, detail, hint.

> 🟢 The refusal has to look like it came from the database, or every tool that
> talks to the database would have to learn about us first.

**Done when:** `psql` displays severity, message and hint, and the session
survives.

### G4 · Codec tests
`tests/test_gateway_protocol.py` — framing, split reads, startup variants,
error encoding, and a fuzz pass asserting the reader never raises on
truncated or malformed input.

---

## P2 — Server

### G5 · Connection handling
`ctrlz/gateway/server.py` — asyncio; one upstream connection per client
connection; no pooling (a proxy that multiplexes sessions would break
`SET LOCAL`, temp tables, and transactions).

### G6 · SSL negotiation
The gateway must read SQL, so it cannot pass TLS through end to end. It answers
`SSLRequest` with `N` and proceeds in plaintext.

**Consequence, documented rather than hidden:** bind to localhost or a trusted
network segment. TLS to the *upstream* is still supported and is a separate
setting.

### G7 · Authentication relay
Credentials are relayed, never read, stored or re-issued (D2.4).
`cleartext`, `md5` and `SCRAM` all work because we do not participate.

**Consequence:** we cannot learn the authenticated identity from SCRAM, so
attribution uses the startup packet's `user` and `application_name`. Honest
about what it knows.

### G8 · Cancel requests
A `CancelRequest` arrives on a *new* connection carrying the backend key of the
session to cancel. Forward it and close.

**Done when:** Ctrl+C in `psql` through the gateway cancels the running query.

---

## P3 — Interception

### G9 · Statement extraction
Inspect `Query` ('Q') and `Parse` ('P') only. Every other message type —
including `COPY` data, function calls and replication — is relayed byte for
byte (D2.1).

### G10 · Policy application and blocking
On `block`: reply with `ErrorResponse`, do not forward, and leave the session
usable.

Simple protocol: `ErrorResponse` + `ReadyForQuery`.
Extended protocol: `ErrorResponse`, then swallow messages until `Sync`, then
`ReadyForQuery` — which is exactly what a real backend does in error state.

### G11 · Fail open, always
Any internal exception logs and forwards the statement unchanged (D2.2, NFR-3).

> 🟢 A bug in the checkpoint must never be able to take the database offline.
> If our code cannot decide, the answer is "let it through", because the
> recorder inside the database is still running either way.

**Done when:** a fault-injection test forces the interceptor to raise on every
statement and the client still completes its work.

### G12 · Attribution injection
After authentication and before the client is told the session is ready, run
`set_config` for the actor fields, consume the response, then release the
held `ReadyForQuery`.

**Done when:** a change made through the gateway shows the client's `user` and
`application_name` in `ctrlz log`.

---

## P4 — SDK wrapper

### S1 · DB-API connection and cursor proxy
`ctrlz/sdk/` — wraps any DB-API 2.0 connection; `execute`/`executemany` consult
the policy first.

### S2 · SQLAlchemy hook
A `before_cursor_execute` listener applying the same evaluator.

### S3 · Shared evaluator proof
A test asserting gateway and SDK reach the **same verdict** for the same
statement and policy (D2.6). Not a code-review promise — an assertion.

### S4 · SDK tests
Blocking, `--force` equivalent, attribution, and no behaviour change when the
policy allows.

---

## P5 — Surface

### X1 · `ctrlz gateway` command
`ctrlz gateway --listen 127.0.0.1:6543 --upstream postgresql://…`

### X2 · End-to-end tests
Real `psql` subprocess through the gateway; psycopg2 for the extended protocol;
cancel; fault injection.

### X3 · Latency benchmark
NFR-2: under 1 ms p99 added per statement. Phase 1 measured 0.46 ms for parse
plus policy, so the budget is known to be achievable.

### X4 · Docs
README gateway section, the plaintext consequence of G6, and a restatement that
capture does not depend on the gateway.

---

## Definition of done

- [x] `psql` connects through the gateway with no client-side change
- [x] A blocked statement yields a native error and the session survives
- [x] Forcing the interceptor to raise does not prevent database access
- [x] Gateway, SDK **and CLI** produce identical verdicts
- [x] Attribution from the gateway appears in `ctrlz log`
- [x] p99 added latency < 1 ms — **measured 0.26 ms cold, 0.0015 ms warm**
- [x] Existing tests pass unmodified (512 total, up from 412)
- [x] Commits authored by Praveen Anandhanathan, co-authored by Claude

## What the tests found

| Found by | Defect |
|---|---|
| first live `psql` connection | authentication was buffered instead of relayed, so every connection hung. Auth is a dialogue, not an announcement |
| extended-protocol test | psycopg2 interpolates parameters client-side and uses the *simple* protocol, so the test that claimed to cover the extended path did not. Covered with psycopg3 instead, and the old test renamed to say what it actually exercises |
| multi-statement test | a batch arrives as one message, so it is one decision. The protocol offers no way to let half through; refusing the batch is pinned by a test rather than left to be discovered |
| gateway shutdown | in-flight sessions were never cancelled, leaving handlers alive past `stop()` |

## Deviation from the commit plan

Planned as five commits, delivered as four: the server and the policy
interception (P2 and P3) are not separably green — a proxy that relays traffic
without applying policy is not a state worth committing, and one that applies
policy without a server cannot be run. Everything else split as planned.
