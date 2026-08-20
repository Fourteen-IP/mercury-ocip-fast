# mercury-ocip-fast

mercury-ocip-fast is the throughput-focused counterpart to [mercury-ocip](https://github.com/Fourteen-IP/mercury-ocip). It leans on session pooling and async concurrency, so it holds up when a back-end has to push a lot of OCI-P traffic at once.

Reach for mercury-ocip when you are scripting or automating. Reach for mercury-ocip-fast when you need to run many requests and want stability under load. Both speak the same command definitions, so you can move command code between them without rewriting it.

## Installation

```bash
pip install mercury-ocip-fast
```

## Public API

The top-level package exports everything you need:

```python
from mercury_ocip_fast import (
    Client,
    SessionClient,
    SessionPair,
    SessionPoolSettings,
    SOAPSessionAtom,
    SOAPSessionSettings,
    TCPSessionAtom,
    TCPSessionSettings,
)
```

Two entry points cover the two ways you are likely to work:

- **`Client`** logs in once as a single admin identity, keeps a pool of live sessions, and hands you throughput. Use it when one identity drives all the work.
- **`SessionClient`** opens a session per user, lets you send commands over it, and lets you resume it later from a stored token. Use it when a service acts on behalf of many users.

You pick the transport with the `atom_type` argument: `TCPSessionAtom` for raw TCP, `SOAPSessionAtom` for SOAP over HTTP or HTTPS. There is no `conn_type` string.

## Basic usage (TCP)

`Client` opens a pool of sessions and logs each one in as your user. Drive it inside an `async with` block:

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password",
    atom_type=TCPSessionAtom,
    session_config=TCPSessionSettings(),
    pool_config=SessionPoolSettings(),
) as client:
    response = await client.command(
        UserGetRequest23V2(user_id="user@domain.com"),
    )
    print(response.first_name)
```

A client needs an async setup step before you can use it, and the `async with` block runs that step for you. If you would rather manage the lifetime yourself, build the client with `await Client.create(...)` and call `await client.close()` when you are done.

`host`, `username`, `password`, `atom_type`, `session_config`, and `pool_config` are required. `port` and `tls` are optional, and `tls` defaults to `True`.

## Basic usage (SOAP)

For SOAP, pass `atom_type=SOAPSessionAtom` and a `SOAPSessionSettings`, and give the full endpoint URL as the host (no `?wsdl` suffix):

```python
from mercury_ocip_fast import Client, SessionPoolSettings, SOAPSessionAtom, SOAPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2

async with Client(
    host="https://your-broadworks.server/webservice/services/ProvisioningService",
    username="admin",
    password="your-password",
    atom_type=SOAPSessionAtom,
    session_config=SOAPSessionSettings(),
    pool_config=SessionPoolSettings(),
) as client:
    response = await client.command(
        UserGetRequest23V2(user_id="user@domain.com"),
    )
    print(response.first_name)
```

The command layer behaves the same over both transports. Single commands, batches, and error handling all work the same way, so your command code carries across TCP and SOAP unchanged.

### Response types are inferred

Each request knows its own response class, so `command()` returns the matching type without any hint from you. Send a `UserGetRequest23V2` and you get back a `UserGetResponse23V2` (or ErrorResponse), typed and ready:

```python
# Typed as UserGetResponse23V2 | ErrorResponse, inferred from the request:
response = await client.command(UserGetRequest23V2(user_id="user@domain.com"))

if isinstance(response, ErrorResponse):
    raise exception

print(response.first_name)
```

Pass the `response_type` keyword only when you want to override that default and parse into a different class:

```python
response = await client.command(request, response_type=SomeOtherResponse)
```

Most code never needs it.

## Batch operations

Hand `command()` a list to send a batch:

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password",
    atom_type=TCPSessionAtom,
    session_config=TCPSessionSettings(),
    pool_config=SessionPoolSettings(),
) as client:
    users = ["user1@domain.com", "user2@domain.com", "user3@domain.com"]

    responses = await client.command(
        [UserGetRequest23V2(user_id=user) for user in users],
    )

    for response in responses:
        print(f"{response.user_id}: {response.first_name}")
```

A batch call takes one session from the pool and sends the commands over it in groups of 15, one group after the next, as the OCI-P spec requires. It never spreads a single call across multiple sessions, and the responses come back in the order you sent them.

To run work in parallel, fire off several `command()` calls together, for example with `asyncio.gather`. Each call grabs its own session, so you get concurrency up to the size of the pool.

## Pool configuration

`SessionPoolSettings` controls the pool's size and its wait times:

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings

pool_config = SessionPoolSettings(
    max_size=5,             # How many sessions the pool holds.
    acquire_timeout=10.0,   # Seconds to wait to acquire a session.
    wait_timeout=10.0,      # Seconds to wait for a free session.
)

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password",
    atom_type=TCPSessionAtom,
    session_config=TCPSessionSettings(),
    pool_config=pool_config,
) as client:
    ...
```

`max_size` sets how many sessions the pool holds, which is also the ceiling on how many `command()` calls run at once, since each call takes one session. Raise it to send more in parallel, up to what your BroadWorks cluster can absorb. The values shown are the defaults.

## Session configuration

Session settings hold the transport timeouts for each session. Use `TCPSessionSettings` for TCP and `SOAPSessionSettings` for SOAP.

`TCPSessionSettings`:

```python
from mercury_ocip_fast import TCPSessionSettings

session_config = TCPSessionSettings(
    connect_timeout=30,     # Seconds to wait for the socket to open.
    read_timeout=30,        # Seconds to wait for a reply.
    read_chunk_size=8192,   # Bytes to read from the socket at a time.
    max_ttl_seconds=900,    # Session lifetime before it goes stale.
)
```

`SOAPSessionSettings`:

```python
from mercury_ocip_fast import SOAPSessionSettings

session_config = SOAPSessionSettings(
    connect_timeout=30.0,   # Seconds to wait for the HTTP connection.
    read_timeout=30.0,      # Seconds to wait for the HTTP reply.
    write_timeout=30.0,     # Seconds to wait to send the request.
    max_ttl_seconds=900,    # Session lifetime before it goes stale.
)
```

Both take keyword arguments only, and the values above are the defaults.

### How SOAP pooling works

BroadWorks ties an OCI-P login to the HTTP session, keyed on its `JSESSIONID` cookie, rather than to the session id in the request body. Sharing one SOAP client would share one login, so mercury-ocip-fast does not do that. The pool holds several sessions instead, and each one logs in on its own with its own httpx client, its own cookie jar, and its own session id. It is the SOAP form of the TCP session pool: one request at a time per session, many sessions running side by side.

Every session logs in once and then serves request after request, so you end up with a handful of authenticated sessions working in parallel rather than a single login funneling everything.

## TLS and non-TLS

`tls` defaults to `True`, and the default TCP port is 2209.

```python
# TLS on, TCP, default port 2209:
async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password",
    atom_type=TCPSessionAtom,
    session_config=TCPSessionSettings(),
    pool_config=SessionPoolSettings(),
) as client:
    ...
```

For a plaintext TCP link, set `tls=False`. The port stays at 2209 unless you set it, so pass the plaintext port yourself (usually 2208):

```python
# TLS off, TCP, plaintext port 2208:
async with Client(
    host="your-broadworks.server",
    port=2208,
    username="admin",
    password="your-password",
    atom_type=TCPSessionAtom,
    session_config=TCPSessionSettings(),
    pool_config=SessionPoolSettings(),
    tls=False,
) as client:
    ...
```

The `tls` flag also picks the login flow:

- **TLS on:** the client sends the plain-text login. The link is encrypted, so the password is safe to send as-is.
- **TLS off:** the client sends the encrypted login. The password is hashed, so it never crosses the wire in clear text.

For TCP, `tls` also turns the socket TLS on or off and controls certificate checks. For SOAP, HTTP versus HTTPS comes from the host URL you pass, and `tls` selects the login flow and controls the httpx certificate check.

## Response handling

A command that fails on the server does not raise. The server sends back an `ErrorResponse`, and `command()` returns it like any other response. Each request's response type is the union `Response | ErrorResponse`, so you check which one you got with `isinstance`:

```python
from mercury_ocip_fast.commands.commands import ErrorResponse

response = await client.command(some_command)
if isinstance(response, ErrorResponse):
    print(f"The server returned an error: {response.summary}")
else:
    print(response.user_id)
```

`ErrorResponse` carries `error_code`, `summary`, `summary_english`, and `detail`, so you can read the code and message straight off the object.

Batch responses stay in the order you sent them, and a failed command shows up as an `ErrorResponse` in its own slot. One bad command does not sink the rest of the batch, so check each response on its own:

```python
commands = [cmd1, cmd2, cmd3]
responses = await client.command(commands)
for command, response in zip(commands, responses):
    if isinstance(response, ErrorResponse):
        print(f"{command} failed: {response.summary}")
    else:
        ...  # Handle the successful response.
```

Login is the exception that does raise. If a session cannot authenticate, the client raises `MErrorLogin` while it opens the session, before your command runs:

```python
from mercury_ocip_fast.exceptions import MErrorLogin

try:
    async with Client(...) as client:
        ...
except MErrorLogin as error:
    print(f"Login failed: {error.message}")
```

`MErrorLogin` subclasses `MError`, the library's base exception, and its `message` field holds the reason the login was rejected.

## SessionClient: sessions per user

`SessionClient` takes a different shape from `Client`. It carries no identity of its own and keeps no pool. You open a session for a given user, send commands over it, and close it when you are done. You can also export a session and resume it later, which suits a service that acts for many users.

`SessionClient` is SOAP only, because only a SOAP session can resume a login. `atom_type` must be `SOAPSessionAtom`.

```python
from mercury_ocip_fast import SessionClient, SOAPSessionAtom, SOAPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2

async with SessionClient(
    host="https://your-broadworks.server/webservice/services/ProvisioningService",
    atom_type=SOAPSessionAtom,
    session_config=SOAPSessionSettings(),
) as client:
    # Open a session, logged in as the user.
    session = await client.open("user_admin", "user_password")
    try:
        response = await client.command(
            session,
            UserGetRequest23V2(user_id="user@domain.com"),
        )
        print(response.first_name)
    finally:
        # The session is yours. Close it when the work is done.
        await client.close(session)
```

`SessionClient` takes no `username` or `password`, since you supply credentials to `open()` per user, and no `pool_config`, since it holds no pool.

`client.command(session, request, ...)` takes the session as its first argument and otherwise mirrors `Client.command`: single command or batch, batches split into groups of 15 and sent over the one session in order, and the same optional `response_type` override.

### Export and resume a session

Every session exposes a `pair` property, a `SessionPair` value holding the JSESSIONID cookie and the OCI-P session id. Store the pair and you can resume the session later with no fresh login. Treat it as a secret: anyone holding the pair can send commands as that user.

```python
# Export the identity of an open session.
pair = session.pair

# ... store the pair, for example between requests or across a restart ...

# Resume the session later, with no new login.
resumed = await client.resume(pair)
try:
    response = await client.command(
        resumed,
        UserGetRequest23V2(user_id="user@domain.com"),
    )
finally:
    await client.close(resumed)
```

Reading `session.pair` before the session has logged in raises `MErrorMissingSessionIdentity`.

To keep a `SessionClient` outside an `async with` block, build it with `await SessionClient.create(...)`. The client owns no sessions, so it has nothing of its own to close, but you still close every session you open.

## Use cases

Pick mercury-ocip for:

- Scripts and automation
- Interactive CLI tools
- General-purpose work

Pick mercury-ocip-fast for:

- Backend APIs and services
- Bulk data migrations
- High-volume reporting
- Production workloads that need stability and throughput

Both share the same OCI-P command definitions, so command code moves between them.

## Performance notes

This library can generate a lot of traffic fast, and a BroadWorks cluster that is not sized for it will feel the strain. A few habits keep that in check:

- Start with a small `max_size`.
- Watch the cluster while a batch runs.
- Add rate limits where you need them.

## API Reference

See the [Commands Reference](/commands/) for the OCI-P commands.

### Clients

::: mercury_ocip_fast.client

::: mercury_ocip_fast.session_client

### Sessions

::: mercury_ocip_fast.session.soap_session

::: mercury_ocip_fast.session.tcp_session
