# mercury-ocip-fast

mercury-ocip-fast is a counterpart to [mercury-ocip](https://github.com/Fourteen-IP/mercury-ocip). It is built for high-volume production workloads. It is faster because it uses session pooling and async concurrency. This makes it good for backend services and bulk operations.

mercury-ocip is good for scripting and automation. mercury-ocip-fast is for stability and throughput when you must handle many requests.

## Installation

```bash
pip install mercury-ocip-fast
```

## Public API

Import these names from the top-level package:

```python
from mercury_ocip_fast import (
    Client,
    SessionClient,
    SessionPoolSettings,
    SOAPSessionAtom,
    SOAPSessionSettings,
    TCPSessionAtom,
    TCPSessionSettings,
)
```

You choose the transport with the `atom_type` argument. Pass `TCPSessionAtom` for raw TCP. Pass `SOAPSessionAtom` for SOAP over HTTP or HTTPS. There is no `conn_type` string.

## Basic usage (TCP)

The `Client` opens a pool of sessions. It logs in each session as the user. It sends commands over the pool. Use the client in an `async with` block.

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2, UserGetResponse23V2

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
        response_type=UserGetResponse23V2,
    )
    print(response.first_name)
```

The client needs an async setup step before use. The `async with` block does this setup. To keep a client without an `async with` block, make it with `await Client.create(...)`, then call `await client.close()` when your work is complete.

The `host`, `username`, `password`, `atom_type`, `session_config`, and `pool_config` arguments are required. The `port` and `tls` arguments are optional. The `tls` argument is `True` by default.

## Basic usage (SOAP)

For SOAP, pass `atom_type=SOAPSessionAtom` and `session_config=SOAPSessionSettings(...)`. Give the full SOAP endpoint URL as the host (no `?wsdl` suffix):

```python
from mercury_ocip_fast import Client, SessionPoolSettings, SOAPSessionAtom, SOAPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2, UserGetResponse23V2

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
        response_type=UserGetResponse23V2,
    )
    print(response.first_name)
```

Everything else works the same over both transports. Single commands, batches, and error handling do not change. So your command code moves between the two transports without a change.

### The `response_type` argument

The `response_type` keyword tells the parser which class to build. If you give it, the result has that type. If you leave it out, the result has the base `OCIResponse` type.

```python
# Typed as UserGetResponse23V2:
response = await client.command(request, response_type=UserGetResponse23V2)

# Typed as the base OCIResponse:
response = await client.command(request)
```

## Batch operations

Pass a list of commands to send a batch:

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2, UserGetResponse23V2

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
        response_type=UserGetResponse23V2,
    )

    for response in responses:
        print(f"{response.user_id}: {response.first_name}")
```

The client splits the list into groups of 15, per the OCI-P spec. One `command()` call acquires one session from the pool. It sends the groups over that one session, one after the other. It does not spread a single call across many sessions. The responses come back in the same order as the input commands.

To run many calls at the same time, start several `command()` calls together (for example with `asyncio.gather`). Each call takes its own session from the pool, so the calls run in parallel up to the pool size.

## Pool configuration

`SessionPoolSettings` sets the size and the wait times of the pool:

```python
from mercury_ocip_fast import Client, SessionPoolSettings, TCPSessionAtom, TCPSessionSettings

pool_config = SessionPoolSettings(
    max_size=5,             # Maximum number of sessions in the pool.
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

`max_size` is the number of sessions the pool holds. It is also the limit on how many `command()` calls can run at the same time, because each call takes one session. Raise it to send more in parallel, within what your BroadWorks cluster can take. The values above are the defaults.

## Session configuration

The session config sets the timeouts for each session's transport. Use `TCPSessionSettings` for TCP and `SOAPSessionSettings` for SOAP.

`TCPSessionSettings` fields:

```python
from mercury_ocip_fast import TCPSessionSettings

session_config = TCPSessionSettings(
    connect_timeout=30,     # Seconds to wait for the socket to open.
    read_timeout=30,        # Seconds to wait for a reply.
    read_chunk_size=8192,   # Bytes to read from the socket at a time.
    max_ttl_seconds=900,    # Session lifetime before it is stale.
)
```

`SOAPSessionSettings` fields:

```python
from mercury_ocip_fast import SOAPSessionSettings

session_config = SOAPSessionSettings(
    connect_timeout=30.0,   # Seconds to wait for the HTTP connection.
    read_timeout=30.0,      # Seconds to wait for the HTTP reply.
    write_timeout=30.0,     # Seconds to wait to send the request.
    max_ttl_seconds=900,    # Session lifetime before it is stale.
)
```

Both classes take keyword arguments only. The values above are the defaults.

### How SOAP pooling works

BroadWorks ties an OCI-P login to the HTTP session (its `JSESSIONID` cookie), not to the session id in the request body. So mercury-ocip-fast does not share one SOAP client. Instead the pool holds several sessions that each log in on their own. Each session has its own httpx client, its own cookie jar, and its own session id. This is the SOAP form of the TCP session pool. Each session handles one request at a time. Different sessions run in parallel.

Each session logs in one time and then serves many requests. So you get several authenticated sessions that run at the same time, not one login for everything.

## TLS and non-TLS

The `tls` argument is `True` by default. The TCP default port is 2209.

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

For a plain TCP link, set `tls=False`. The port stays 2209 unless you set `port` yourself, so pass the plaintext port (usually 2208):

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

The `tls` argument selects the login flow:

- **TLS on:** the client uses the plain-text login. The link is encrypted, so the password is safe to send directly.
- **TLS off:** the client uses the encrypted login. The password is hashed, so it never travels in plaintext.

For TCP, `tls` also turns the socket TLS on or off and controls certificate checks. For SOAP, whether the connection is HTTP or HTTPS follows the host URL you pass. The `tls` flag selects the login flow and controls the httpx certificate check.

## Response handling

The library raises on a server error. It does not return an error object. When the server returns an `ErrorResponse`, the requester raises `MErrorResponse`. Catch it with `try`/`except`:

```python
from mercury_ocip_fast.exceptions import MErrorResponse

try:
    response = await client.command(some_command, response_type=UserGetResponse23V2)
    print(response.user_id)
except MErrorResponse as error:
    print(f"The server returned an error: {error.message}")
```

`MErrorResponse` is a subclass of `MError`, the base exception of the library. Its `message` field holds the error summary from the server.

For a batch, the responses stay in the same order as the input commands. If any command in the batch fails, the requester raises `MErrorResponse` for the batch:

```python
commands = [cmd1, cmd2, cmd3]
try:
    responses = await client.command(commands, response_type=UserGetResponse23V2)
    for command, response in zip(commands, responses):
        ...  # Process each pair.
except MErrorResponse as error:
    print(f"A command in the batch failed: {error.message}")
```

## SessionClient: multi-tenant sessions

`SessionClient` is a different entry point. It keeps no identity and no session list. You open a session for a user, you send commands over it, and you close it yourself. You can also export a session and resume it later. This is good for a service that acts for many users.

`SessionClient` works with SOAP only. Only a SOAP session can resume a login, so `atom_type` must be `SOAPSessionAtom`.

```python
from mercury_ocip_fast import SessionClient, SOAPSessionAtom, SOAPSessionSettings
from mercury_ocip_fast.commands.commands import UserGetRequest23V2, UserGetResponse23V2

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
            response_type=UserGetResponse23V2,
        )
        print(response.first_name)
    finally:
        # You own the session. Close it when your work is complete.
        await client.close(session)
```

The `SessionClient` does not take `username` or `password`. You give the credentials to `open()` for each user. It also does not take a `pool_config`, because it does not hold a pool.

`client.command(session, request, ...)` takes the session as its first argument. It works like `Client.command`. It sends a single command or a batch. It splits a batch into groups of 15 and sends them over the session, one after the other. It takes the same `response_type` keyword.

### Export and resume a session

Each session has a `pair` property. This is a `SessionPair` value. It holds the JSESSIONID cookie and the OCI-P session id. Store the pair to resume the session later. Treat the pair as a secret. A person who holds the pair can send commands as the user.

```python
# Export the identity of an open session.
pair = session.pair

# ... store the pair, for example between requests or after a restart ...

# Resume the session later, with no new login.
resumed = await client.resume(pair)
try:
    response = await client.command(
        resumed,
        UserGetRequest23V2(user_id="user@domain.com"),
        response_type=UserGetResponse23V2,
    )
finally:
    await client.close(resumed)
```

`session.pair` raises `MErrorMissingSessionIdentity` if the session has no login yet.

To keep a `SessionClient` without an `async with` block, make it with `await SessionClient.create(...)`. The client owns no sessions, so it has nothing of its own to close. You still must close each session that you open.

## Use cases

mercury-ocip is better for:

- Scripts and automation
- Interactive CLI tools
- General purpose work

mercury-ocip-fast is better for:

- Backend APIs and services
- Bulk data migrations
- High-volume reporting
- Production workloads that need stability and throughput

Both libraries use the same OCI-P command definitions. So command code moves between them.

## Performance notes

This library can make a lot of traffic quickly. A BroadWorks cluster that is not sized for the load can feel the impact. Consider these steps:

- Start with a small `max_size`.
- Watch the cluster while a batch runs.
- Add rate limits if you need them.

## API Reference

See the [Commands Reference](/commands/) for the OCI-P commands.

### Clients

::: mercury_ocip_fast.client

::: mercury_ocip_fast.session_client

### Sessions

::: mercury_ocip_fast.session.soap_session

::: mercury_ocip_fast.session.tcp_session
