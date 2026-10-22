"""Verify the official SignalR cookie fix against a local affinity-gated socket.

Run in an isolated environment containing pyeasee==0.8.19.
No credentials or real charger calls are used.
"""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json

from aiohttp import web
from pysignalr.protocol.json import JSONProtocol
from pysignalr.transport.websocket import WebsocketTransport
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def main():
    async def negotiate(request):
        response = web.json_response({"connectionId": "test-connection"})
        response.set_cookie("ARRAffinity", "local-backend", path="/")
        return response

    async def socket(request):
        if request.cookies.get("ARRAffinity") != "local-backend":
            return web.Response(status=404, text="No Connection with that ID")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("affinity-confirmed")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_post("/hub/negotiate", negotiate)
    app.router.add_get("/hub", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "localhost", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    async def callback(message):
        pass
    transport = WebsocketTransport(f"ws://localhost:{port}/hub", JSONProtocol(), callback)
    try:
        async with asyncio.timeout(10):
            await transport._negotiate()
            async with connect(transport._url, additional_headers=transport._headers, proxy=None) as connection:
                assert await connection.recv() == "affinity-confirmed"
            without_cookie = {k: v for k, v in transport._headers.items() if k.lower() != "cookie"}
            try:
                async with connect(transport._url, additional_headers=without_cookie, proxy=None):
                    raise AssertionError("Missing affinity cookie must fail")
            except InvalidStatus as err:
                assert err.response.status_code == 404
        print(json.dumps({"pyeasee": version("pyeasee"), "pysignalr": version("pysignalr"),
            "with_negotiate_cookie": "connected", "without_cookie": "HTTP 404"}))
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
