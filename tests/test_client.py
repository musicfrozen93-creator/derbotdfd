"""11. WebSocket reconnection against a local mock Deriv server."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.deriv.client import DerivAPIError, DerivClient


class MockDeriv:
    """Speaks just enough of the Deriv protocol: ping, ticks subscribe, an error, and
    drops the first connection after a few ticks."""

    def __init__(self):
        self.connections = 0
        self.subscribe_requests = 0

    async def handler(self, ws):
        self.connections += 1
        conn_no = self.connections
        epoch = 1_000 + conn_no * 100
        streaming = False
        async def stream():
            nonlocal epoch
            sent = 0
            while True:
                await asyncio.sleep(0.02)
                epoch += 1
                # raw text, as Deriv sends it: the trailing zero is present on the wire
                await ws.send('{"msg_type":"tick","tick":{"symbol":"R_10","epoch":%d,'
                              '"quote":4849.430,"pip_size":3}}' % epoch)
                sent += 1
                if conn_no == 1 and sent >= 5:
                    await ws.close()
                    return
        task = None
        try:
            async for raw in ws:
                if raw == "not json":
                    continue
                req = json.loads(raw)
                rid = req.get("req_id")
                if "ping" in req:
                    await ws.send(json.dumps({"msg_type": "ping", "ping": "pong", "req_id": rid}))
                elif "ticks" in req:
                    self.subscribe_requests += 1
                    await ws.send('{"msg_type":"tick","req_id":%d,"subscription":{"id":"s1"},'
                                  '"tick":{"symbol":"R_10","epoch":%d,"quote":4849.430,"pip_size":3}}'
                                  % (rid, epoch))
                    if not streaming:
                        streaming = True
                        task = asyncio.create_task(stream())
                elif "bogus" in req:
                    await ws.send(json.dumps({"msg_type": "error", "req_id": rid,
                                              "error": {"code": "UnknownMethod", "message": "nope"}}))
        finally:
            if task:
                task.cancel()


async def _scenario():
    mock = MockDeriv()
    async with serve(mock.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = DerivClient(f"ws://127.0.0.1:{port}", backoff_min=0.1, backoff_max=0.2, idle_reconnect=5,
                             ping_interval=0.3)
        ticks = []
        client.on_tick = lambda t: ticks.append((t["epoch"], str(t["quote"])))

        async def resubscribe():
            await client.subscribe_ticks("R_10")

        client.add_connect_hook(resubscribe)
        runner = asyncio.create_task(client.run())
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if client.connect_count >= 2 and len(ticks) >= 10:
                    break
            with pytest.raises(DerivAPIError):
                await client.request({"bogus": 1})
            with pytest.raises(PermissionError):
                await client.request({"buy": "x", "price": 1})
            pong = await client.request({"ping": 1})
            assert pong["ping"] == "pong"
            await client._ws.send("not json")       # server ignores; client must survive malformed traffic
        finally:
            await client.stop()
            runner.cancel()
            try:
                await runner
            except BaseException:
                pass
        return mock, client, ticks


def test_reconnects_and_resubscribes():
    mock, client, ticks = asyncio.run(_scenario())
    assert client.connect_count >= 2
    assert mock.subscribe_requests >= 2          # re-subscribed after the drop
    assert len(ticks) >= 10
    assert all(q == "4849.430" for _, q in ticks)   # Decimal parse keeps the raw text, trailing zero included


def test_malformed_server_message_is_ignored():
    client = DerivClient("ws://unused")
    seen = []
    client.on_tick = seen.append
    client._dispatch({"msg_type": "tick", "tick": "garbage"})
    client._dispatch({"msg_type": "tick", "tick": {"symbol": "R_10"}})
    assert seen == [{"symbol": "R_10"}]           # passed through; the processor validates it
