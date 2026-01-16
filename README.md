[![Downloads](https://static.pepy.tech/badge/mercury-ocip)](https://pepy.tech/project/mercury-ocip-fast)
[![Downloads](https://static.pepy.tech/badge/mercury-ocip/month)](https://pepy.tech/project/mercury-ocip-fast)
[![Downloads](https://static.pepy.tech/badge/mercury-ocip/week)](https://pepy.tech/project/mercury-ocip-fast)
[![pypi version](https://img.shields.io/pypi/v/mercury-ocip.svg)](https://pypi.python.org/pypi/mercury-ocip-fast)

# mercury-ocip-fast

mercury-ocip-fast is a lightning fast and async-first Python SDK for BroadWorks OCI-P. It uses connection pooling and concurrent request batching to handle bulk operations efficiently.

---

```python
from mercury_ocip_fast import Client
from mercury_ocip_fast.commands import UserGetRequest21sp1

async with Client(
        host="broadworks.server",
        username="admin",
        password="secret"
    ) as client:
        # single request
        response = await client.command(
            UserGetRequest21sp1(
                    user_id="user@example.com"
                )
            )

        # bulk requests - batched and processed concurrently
        users = ["user1@example.com", "user2@example.com", "user3@example.com"]
        responses = await client.command(
                [
                    UserGetRequest21sp1(user_id=u)
                    for u in users
                ]
            )
```

---

![Async vs Sync Performance](assets/figure.png)
^ mercury-ocip vs mercury-ocip-fast - This test included retrieving 2000+ users from a Group, and resulted in a 67x speed increase compared to mercury-ocip!

---

This is a backend focused tool, made to be lightweight, fast, and stable as a counterpart to mercury-ocip. Not intended for general scripting and automations.

> [Checkout our automation focused library here!](https://github.com/Fourteen-IP/mercury-ocip)

Testing has shown that the tool can negatively impact BroadWorks infrastructure, as it allows a very high volume of requests to be sent within a short period of time. The extent of the impact will vary from cluster to cluster, depending on hardware specifications, network capacity, and other general factors. Please use this responsibly, high capability requires careful consideration.

---

## Important Legal Notice

Mercury is an independent, open-source project and is NOT affiliated with, endorsed by, or supported by Cisco Systems, Inc.

BroadWorks is a product and trademark of Cisco Systems, Inc. Mercury provides a client  interface to interact with BroadWorks systems via the Open Client Interface Protocol(OCI-P).

Mercury does not bypass, circumvent, or provide any additional permissions or licenses. To  use Mercury, you must:
- Have an active, licensed BroadWorks system from Cisco
- Possess valid credentials and appropriate access permissions
- Comply with all Cisco licensing terms and agreements

The OCI-P commands implemented in Mercury are generated from XML schemas. These schemas are:

`Copyright © 2018 BroadSoft Inc. (now part of Cisco Systems, Inc.)
All rights reserved.`

Mercury implements these publicly documented interfaces and does not include any    proprietary Cisco code or intellectual property. All command structures follow the     official OCI-P specification.

---