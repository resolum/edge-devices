"""Servidor WebSocket para el ESP32 (cliente de ws://EDGE:8765/ws).

Detalles que el firmware exige:
- Sin subprotocolo y sin permessage-deflate (compression=None).
- Mensajes sin fragmentar; texto = JSON, binario = PCM.
- El ESP32 manda ping WS cada 10 s; la librería responde sola. El servidor no manda pings propios:
  la caída se detecta por falta de datos (el micrófono envía cada 40 ms).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from alivia_edge.shared.config import ServerCfg
from alivia_edge.voice.interfaces.transports.base import Link, LinkClosed

log = logging.getLogger(__name__)


class WsLink(Link):
    kind = "ws"
    priority = 2

    def __init__(self, ws: ServerConnection, idle_timeout_s: float) -> None:
        self._ws = ws
        self._idle = idle_timeout_s
        self.peer = str(ws.remote_address)

    @property
    def closed(self) -> bool:
        return self._ws.close_code is not None

    async def recv(self):
        while True:
            try:
                msg = await asyncio.wait_for(self._ws.recv(), timeout=self._idle)
            except asyncio.TimeoutError:
                raise LinkClosed("sin datos del nodo") from None
            except ConnectionClosed:
                raise LinkClosed("conexión cerrada") from None
            if isinstance(msg, bytes):
                return "audio", msg
            try:
                return "json", json.loads(msg)
            except ValueError:
                log.warning("[%s] JSON inválido ignorado: %r", self.peer, msg[:80])

    async def send_json(self, obj: dict) -> None:
        try:
            await self._ws.send(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        except ConnectionClosed:
            raise LinkClosed("conexión cerrada") from None

    async def send_audio(self, pcm: bytes) -> None:
        try:
            await self._ws.send(pcm)
        except ConnectionClosed:
            raise LinkClosed("conexión cerrada") from None

    async def close(self, reason: str = "") -> None:
        try:
            await self._ws.close(1000, reason[:100])
        except Exception:  # noqa: BLE001 - cierre best-effort
            pass


async def serve_ws(cfg: ServerCfg, on_link: Callable[[Link], Awaitable[None]]):
    """Arranca el servidor; on_link atiende todo el ciclo de vida de cada conexión."""

    async def handler(ws: ServerConnection) -> None:
        await on_link(WsLink(ws, cfg.idle_timeout_s))

    def process_request(connection: ServerConnection, request):
        if request.path.split("?")[0] != cfg.path:
            return connection.respond(HTTPStatus.NOT_FOUND, "ruta no válida\n")
        return None

    return await serve(
        handler,
        cfg.host,
        cfg.port,
        process_request=process_request,
        compression=None,
        ping_interval=None,
        max_size=64 * 1024,
    )
