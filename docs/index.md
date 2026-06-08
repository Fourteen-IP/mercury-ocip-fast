# mercury-ocip-fast

mercury-ocip-fast is a counterpart to [mercury-ocip](https://github.com/Fourteen-IP/mercury-ocip), built for high-volume production workloads. It's significantly faster through connection pooling and async concurrency, making it suitable for backend services and bulk operations.

Where mercury-ocip excels at scripting and automation, mercury-ocip-fast is designed for stability and throughput when you need to handle thousands of requests.

## Installation

```bash
pip install mercury-ocip-fast
```

## Basic usage

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.commands.commands import UserGetRequest21sp1

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password"
) as client:
    response = await client.command(
        UserGetRequest21sp1(user_id="user@domain.com")
    )

    print(response.first_name)
```

The client handles authentication automatically. It speaks both transports BroadWorks exposes: raw TCP (the default) and SOAP, over TLS or plain connections. See [SOAP transport](#soap-transport) for the SOAP-specific bits.

## Bulk operations

Pass a list of commands to execute them concurrently:

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.commands.commands import UserGetRequest21sp1

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password"
) as client:
    users = ["user1@domain.com", "user2@domain.com", "user3@domain.com"]

    responses = await client.command([
        UserGetRequest21sp1(user_id=user) for user in users
    ])

    for response in responses:
        print(f"{response.user_id}: {response.first_name}")
```

Commands are batched into groups of 15 per the OCI-P spec and sent concurrently across the connection pool. Responses are returned in the same order as the input commands.

## Connection warming

Pre-create connections to avoid cold-start latency on bulk operations:

```python
async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password"
) as client:
    await client.warm(50)  # Create 50 connections upfront

    responses = await client.command([...])
```

`warm()` works for both transports. On TCP it opens connections; on SOAP it opens and logs in sessions (see below). Warming matters more for SOAP, because sessions are otherwise created lazily one at a time, and each one fetches the WSDL and logs in before it can be used.

## Pool configuration

The connection pool can be configured for your specific workload:

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.pool import PoolConfig

config = PoolConfig(
    max_connections=50,           # Max TCP connections to maintain
    max_concurrent_requests=100,  # Max in-flight requests at once
    connect_timeout=10.0,         # Timeout for establishing connection
    read_timeout=30.0,            # Timeout for reading response
    max_connection_age=300.0,     # Recycle connections after 5 minutes
    idle_timeout=60.0,            # Close idle connections after 1 minute
)

async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password",
    config=config
) as client:
    pass
```

Start with conservative values and adjust based on your BroadWorks cluster capacity.

## SOAP transport

Set `conn_type="SOAP"` and pass the full SOAP endpoint URL as the host (no `?wsdl` suffix):

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.commands.commands import UserGetRequest21sp1

async with Client(
    host="https://your-broadworks.server/webservice/services/ProvisioningService",
    username="admin",
    password="your-password",
    conn_type="SOAP",
) as client:
    response = await client.command(UserGetRequest21sp1(user_id="user@domain.com"))
    print(response.first_name)
```

Everything else (single commands, bulk lists, response handling) works exactly as it does over TCP, so code is portable between the two transports.

### How SOAP pooling works

BroadWorks ties an OCI-P login to the HTTP session (its `JSESSIONID` cookie), not to the session id in the request body. So instead of one shared SOAP client, mercury-ocip-fast keeps a pool of independently logged-in **sessions**, each with its own HTTP client, its own cookie jar, and its own session id. This is the SOAP equivalent of the TCP connection pool: each session handles one request at a time, and requests fan out across the pool.

Because each session logs in once and is reused, you get several authenticated sessions running concurrently rather than serialising everything through a single login.

### SOAP configuration

Use `SOAPPoolConfig` to size the session pool:

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.pool import SOAPPoolConfig

config = SOAPPoolConfig(
    pool_size=8,            # Number of logged-in sessions (also the concurrency limit)
    acquire_timeout=30.0,   # How long to wait for a free session
    max_session_age=300.0,  # Re-authenticate sessions after 5 minutes
    idle_timeout=60.0,      # Close sessions idle longer than this
    verify_ssl=True,        # Verify the server's TLS certificate
)

async with Client(
    host="https://your-broadworks.server/webservice/services/ProvisioningService",
    username="admin",
    password="your-password",
    conn_type="SOAP",
    config=config,
) as client:
    await client.warm()  # Open and log in all 8 sessions up front
    responses = await client.command([...])
```

`pool_size` is both the number of sessions and the maximum number of requests in flight at once, since a session serves one request at a time. Raise it to send more in parallel, within what your BroadWorks cluster can take.

## TLS and non-TLS

The default is TLS on port 2209:

```python
async with Client(
    host="your-broadworks.server",
    username="admin",
    password="your-password"
) as client:
    pass
```

For non-TLS connections on port 2208:

```python
async with Client(
    host="your-broadworks.server",
    port=2208,
    username="admin",
    password="your-password",
    tls=False
) as client:
    pass
```

The login flow adjusts automatically based on the `tls` setting:

- **TLS on:** a single `LoginRequest22V5`. On TCP the socket is wrapped in SSL; on SOAP the endpoint is HTTPS. Either way the channel is encrypted, so the password is sent directly.
- **TLS off:** the two-step flow, an `AuthenticationRequest` for a nonce followed by a hashed `LoginRequest14sp4`, so the password never travels in plaintext.

For SOAP, whether the connection is HTTP or HTTPS follows the host URL you pass; the `tls` flag only selects which login flow is used.

## Monitoring the pool

`pool_stats` reports how busy the pool is, and `session_ids` lists the session id of every connection or session currently open:

```python
async with Client(host=..., username=..., password=..., conn_type="SOAP") as client:
    await client.warm()

    print(client.pool_stats)
    # {'total_sessions': 8, 'available': 8, 'in_use': 0, 'waiting': 0, 'pool_size': 8}

    print(client.session_ids)
    # ['7c61848c-...', '83ee21b1-...', ...]
```

`session_ids` only reflects what has actually been opened, so call `warm()` first if you want the whole pool listed. Treat the ids as secrets.

## Response handling

Responses are parsed into Python objects:

```python
from mercury_ocip_fast.commands.base_command import ErrorResponse

response = await client.command(some_command)

if isinstance(response, ErrorResponse):
    print(f"Error {response.error_code}: {response.summary}")
else:
    print(response.user_id)
```

For bulk operations, responses maintain the same order as the input commands:

```python
commands = [cmd1, cmd2, cmd3]
responses = await client.command(commands)

for cmd, resp in zip(commands, responses):
    # Process each pair
    pass
```

## Use cases

mercury-ocip is better for:
- Scripts and automation
- Interactive CLI tools
- General purpose work

mercury-ocip-fast is better for:
- Backend APIs and services
- Bulk data migrations
- High-volume reporting
- Production workloads requiring stability and throughput

Both libraries use identical OCI-P command definitions, so code is portable between them.

## Performance notes

This library can generate significant traffic quickly. BroadWorks clusters not sized for the load may experience impact. Consider:

- Starting with lower concurrency settings
- Monitoring cluster performance during bulk operations
- Using connection warming selectively
- Rate limiting if necessary

## Example: Bulk user fetch

```python
import asyncio
from mercury_ocip_fast import Client
from mercury_ocip_fast.commands import (
    GroupGetRequest,
    UserGetRequest21sp1
)

async def get_all_group_users(group_id: str):
    async with Client(
        host="broadworks.example.com",
        username="admin",
        password="secret"
    ) as client:
        group = await client.command(
            GroupGetRequest(service_provider_id="ent1", group_id=group_id)
        )

        await client.warm(min(50, len(group.user_ids) // 20))

        responses = await client.command([
            UserGetRequest21sp1(user_id=uid) for uid in group.user_ids
        ])

        return responses

users = asyncio.run(get_all_group_users("sales-team"))
```

## API Reference

See the [Commands Reference](/commands/) for available OCI-P commands.

### Client

::: mercury_ocip_fast.client

### Pool configuration and transport pools

::: mercury_ocip_fast.pool

::: mercury_ocip_fast.soap_pool