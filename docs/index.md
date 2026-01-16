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

The client handles authentication automatically. Both TLS and non-TLS connections are supported.

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

The authentication flow adjusts automatically based on the TLS setting.

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

::: mercury_ocip_fast.client