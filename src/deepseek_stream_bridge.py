"""把非流式 OpenAI Chat Completions 响应包装成标准 SSE。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Mapping, Optional

import requests
from aiohttp import web


_DROP_HEADERS = {
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "transfer-encoding",
}


class DeepSeekNonstreamBridge:
    def __init__(self, upstreams: Mapping[str, str]) -> None:
        self._upstreams = {
            provider: base_url.rstrip("/")
            for provider, base_url in upstreams.items()
        }
        self._runner: Optional[web.AppRunner] = None
        self._port: Optional[int] = None

    async def start(self) -> None:
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post("/{provider}/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]

    def base_url(self, provider: str) -> str:
        return f"http://127.0.0.1:{self._port}/{provider}"

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        payload = await request.json()
        payload["stream"] = False
        payload.pop("stream_options", None)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _DROP_HEADERS
        }
        target = (
            f"{self._upstreams[provider]}/"
            f"{request.match_info['tail'].lstrip('/')}"
        )

        try:
            upstream = await asyncio.to_thread(
                requests.post,
                target,
                headers=headers,
                json=payload,
                timeout=(10, 180),
            )
        except requests.RequestException:
            return web.json_response({"error": "upstream request failed"}, status=502)
        if not upstream.ok:
            return web.Response(body=upstream.content, status=upstream.status_code)

        completion = upstream.json()
        choice = completion["choices"][0]
        event = {
            "id": completion.get("id") or "chatcmpl-nonstream-bridge",
            "object": "chat.completion.chunk",
            "created": completion.get("created") or int(time.time()),
            "model": completion.get("model") or payload.get("model"),
            "choices": [
                {
                    "index": choice.get("index", 0),
                    "delta": choice["message"],
                    "finish_reason": choice.get("finish_reason") or "stop",
                    "logprobs": choice.get("logprobs"),
                }
            ],
            "usage": completion.get("usage"),
        }
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return web.Response(
            text=f"data: {data}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
