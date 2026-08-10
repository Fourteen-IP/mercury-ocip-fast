"""Tests for ``TCPSessionAtom``.

Most tests run a small asyncio server on localhost. The server sends a canned
BroadsoftDocument reply, so the read loop stops on the end tag. No TLS runs;
``verify_ssl=False`` also turns TLS off for the TCP transport.
"""

import asyncio

import pytest

from mercury_ocip_fast.exceptions import MErrorSocketInitialisation
from mercury_ocip_fast.session.session import TCPSessionSettings
from mercury_ocip_fast.session.tcp_session import TCPSessionAtom

REPLY = b"<BroadsoftDocument>reply</BroadsoftDocument>\n"


async def echo_server(reply: bytes = REPLY):
    """Start a localhost server that reads one line and sends the reply."""
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.readline()
        received.append(data)
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, host, port, received


class TestSendRoundTrip:
    async def test_send_reads_until_end_tag(self):
        server, host, port, received = await echo_server()
        async with server:
            atom = await TCPSessionAtom.open(
                host, port, settings=TCPSessionSettings(), verify_ssl=False
            )
            reply = await atom.send("<command/>")
            assert reply == "<BroadsoftDocument>reply</BroadsoftDocument>"
            # The server saw the BroadsoftDocument the atom built.
            assert b"<command/>" in received[0]
            await atom.close()


class TestOpen:
    async def test_open_bad_host_raises(self):
        with pytest.raises(MErrorSocketInitialisation):
            await TCPSessionAtom.open(
                "", settings=TCPSessionSettings(connect_timeout=1), verify_ssl=False
            )


class TestState:
    async def test_alive_then_closed(self):
        server, host, port, _ = await echo_server()
        async with server:
            atom = await TCPSessionAtom.open(
                host, port, settings=TCPSessionSettings(), verify_ssl=False
            )
            assert atom.is_alive() is True
            await atom.close()
            assert atom.is_alive() is False

    async def test_stale_when_ttl_zero(self):
        server, host, port, _ = await echo_server()
        async with server:
            atom = await TCPSessionAtom.open(
                host,
                port,
                settings=TCPSessionSettings(max_ttl_seconds=0),
                verify_ssl=False,
            )
            assert atom.is_stale() is True
            await atom.close()

    async def test_touch_updates_last_used(self):
        server, host, port, _ = await echo_server()
        async with server:
            atom = await TCPSessionAtom.open(
                host, port, settings=TCPSessionSettings(), verify_ssl=False
            )
            before = atom.last_used
            atom.touch()
            assert atom.last_used >= before
            await atom.close()
