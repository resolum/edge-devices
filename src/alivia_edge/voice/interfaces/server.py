"""Servidor del edge: handshake, sesiones por nodo y transportes (WebSocket + Bluetooth)."""
from __future__ import annotations

import asyncio
import hmac
import logging

from alivia_edge.access.domain.errors import InvalidMacAddress
from alivia_edge.access.domain.value_objects import MacAddress
from alivia_edge.app import Services
from alivia_edge.voice.integration.access_acl import MicrophoneDenied
from alivia_edge.voice.interfaces.protocol import HandshakeError, error_msg, parse_hello, ready_msg
from alivia_edge.voice.application.session import DeviceSession
from alivia_edge.voice.interfaces.transports.base import Link, LinkClosed
from alivia_edge.voice.interfaces.transports.bluetooth import BluetoothConnector
from alivia_edge.voice.interfaces.transports.websocket import serve_ws

log = logging.getLogger(__name__)


class EdgeServer:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.cfg = svc.cfg
        self.sessions: dict[str, DeviceSession] = {}
        self._ws_server = None
        self._bt: BluetoothConnector | None = None
        svc.gate.subscribe(self._on_table_changed)  # si la tabla cambia, se corta a quien ya no tenga permiso

    @property
    def port(self) -> int:
        """Puerto real de escucha (útil si se configuró port = 0)."""
        return self._ws_server.sockets[0].getsockname()[1]

    def _on_table_changed(self, _table) -> None:
        for mac, session in list(self.sessions.items()):
            link = session.link
            if link is None or link.closed or self.svc.gate.still_allowed(mac):
                continue
            log.warning("[%s] acceso revocado por la tabla del backend; se cierra el enlace", session.device_id)
            asyncio.get_running_loop().create_task(self._revoke(link))

    @staticmethod
    async def _revoke(link: Link) -> None:
        try:
            await link.send_json(error_msg("acceso revocado"))
        except Exception:  # noqa: BLE001
            pass
        await link.close("acceso revocado")

    def wifi_alive(self, mac: str) -> bool:
        try:
            mac = MacAddress(mac).value
        except InvalidMacAddress:
            return False
        s = self.sessions.get(mac)
        return bool(s and s.link and not s.link.closed and s.link.kind == "ws")

    async def handle_link(self, link: Link) -> None:
        """Ciclo de vida de una conexión: hello -> ready -> audio hasta que se cierre."""
        try:
            try:
                kind, msg = await asyncio.wait_for(link.recv(), timeout=self.cfg.server.hello_timeout_s)
                if kind != "json":
                    raise HandshakeError("se esperaba hello")
                hello = parse_hello(msg)
                if not hmac.compare_digest(hello.token.encode(), self.cfg.server.token.encode()):
                    raise HandshakeError("token inválido")
                try:
                    auth = self.svc.gate.authorize(hello.mac)  # tabla de acceso del backend: ¿puede enviar datos?
                except MicrophoneDenied as exc:
                    raise HandshakeError(f"dispositivo no autorizado: {exc}") from exc
            except HandshakeError as exc:
                log.warning("[%s] hello rechazado: %s", link.peer, exc)
                try:
                    await link.send_json(error_msg(str(exc)))
                finally:
                    await link.close("hello rechazado")
                return
            except (asyncio.TimeoutError, LinkClosed):
                log.info("[%s] sin hello válido (¿sondeo TCP?)", link.peer)
                await link.close("sin hello")
                return

            session = self.sessions.get(auth.mac)
            if session is None:
                session = self.sessions[auth.mac] = DeviceSession(auth.mac, hello.device_id, self.svc)
            if not session.accept(link):
                log.info("[%s] %s ya conectado por un transporte preferente; se rechaza %s", hello.device_id, hello.device_id, link.kind)
                await link.send_json(error_msg("ya conectado por un transporte preferente"))
                await link.close("duplicado")
                return
            await link.send_json(ready_msg())
            log.info("[%s] conectado por %s (%s)", hello.device_id, link.kind, link.peer)
            await session.run_link(link)
        except LinkClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("error en el enlace %s", link.peer)
            await link.close("error interno")

    async def start(self) -> None:
        self._ws_server = await serve_ws(self.cfg.server, self.handle_link)
        log.info("WebSocket escuchando en ws://%s:%d%s", self.cfg.server.host, self.cfg.server.port, self.cfg.server.path)
        if self.cfg.bluetooth.enabled and self.cfg.bluetooth.links:
            self._bt = BluetoothConnector(self.cfg.bluetooth, self.handle_link, self.wifi_alive)
            self._bt.start()
            log.info("Bluetooth activo para: %s", [l.mac for l in self.cfg.bluetooth.links])

    async def stop(self) -> None:
        if self._bt:
            await self._bt.stop()
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        for s in self.sessions.values():
            await s.speaker.cancel()
