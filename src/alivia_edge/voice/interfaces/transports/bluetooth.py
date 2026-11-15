"""Enlace Bluetooth SPP (clásico) como cliente serie.

El ESP32 es el servidor SPP ("Alivia-Mic-01"); el PC debe estar emparejado con él y abre el puerto serie:
  - Windows: el puerto COM *saliente* que crea el emparejamiento (Administrador de dispositivos -> Puertos).
  - Linux:   `sudo rfcomm bind 0 <MAC> 1`  ->  /dev/rfcomm0.
El PC reintenta con backoff; mientras el nodo esté conectado por WiFi no intenta abrir Bluetooth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from alivia_edge.shared.config import BluetoothCfg, BtLinkCfg
from alivia_edge.voice.interfaces.transports.base import Link, LinkClosed
from alivia_edge.voice.interfaces.transports.frames import (
    MAX_PAYLOAD,
    MAX_PONG_ECHO,
    TYPE_AUDIO,
    TYPE_PING,
    TYPE_PONG,
    TYPE_TEXT,
    FrameDecoder,
    encode,
)

log = logging.getLogger(__name__)

KEEPALIVE_S = 10.0  # el ESP32 cierra tras 30 s sin ninguna trama


class BtLink(Link):
    kind = "bt"
    priority = 1

    def __init__(self, ser: Any, loop: asyncio.AbstractEventLoop, name: str) -> None:
        self._ser = ser
        self._loop = loop
        self.peer = name
        self._q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._decoder = FrameDecoder()
        self._wlock = threading.Lock()
        self._wexec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bt-write")
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name=f"bt-read-{name}", daemon=True)
        self._reader.start()
        self._keepalive = loop.create_task(self._keepalive_loop())

    # --- hilo lector -----------------------------------------------------------------
    def _push(self, item: tuple[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, item)
        except RuntimeError:  # loop cerrado
            pass

    def _read_loop(self) -> None:
        reason = "puerto cerrado"
        try:
            while not self._closed:
                data = self._ser.read(self._ser.in_waiting or 1)
                if not data:
                    continue
                for ftype, payload in self._decoder.feed(data):
                    if ftype == TYPE_PING:
                        self._write(encode(TYPE_PONG, payload[:MAX_PONG_ECHO]))
                    elif ftype == TYPE_PONG:
                        pass
                    elif ftype == TYPE_AUDIO:
                        self._push(("audio", payload))
                    elif ftype == TYPE_TEXT:
                        try:
                            self._push(("json", json.loads(payload.decode("utf-8"))))
                        except (ValueError, UnicodeDecodeError):
                            log.warning("[%s] JSON inválido por Bluetooth ignorado", self.peer)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo del puerto cierra el enlace
            reason = f"error de puerto: {exc}"
        finally:
            self._closed = True
            self._push(("closed", reason))

    def _write(self, frame: bytes) -> None:
        with self._wlock:
            self._ser.write(frame)

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(KEEPALIVE_S)
                await self._send_frame(encode(TYPE_PING, b"e"))
        except (LinkClosed, asyncio.CancelledError):
            pass

    async def _send_frame(self, frame: bytes) -> None:
        if self._closed:
            raise LinkClosed("puerto cerrado")
        try:
            await self._loop.run_in_executor(self._wexec, self._write, frame)
        except Exception as exc:  # noqa: BLE001
            self._closed = True
            raise LinkClosed(f"error al escribir: {exc}") from None

    # --- interfaz Link ---------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    async def recv(self):
        kind, value = await self._q.get()
        if kind == "closed":
            self._q.put_nowait((kind, value))  # los recv() posteriores también fallan
            raise LinkClosed(value)
        return kind, value

    async def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        await self._send_frame(encode(TYPE_TEXT, payload))

    async def send_audio(self, pcm: bytes) -> None:
        if len(pcm) > MAX_PAYLOAD:
            raise ValueError("audio excede 2048 bytes por trama")
        await self._send_frame(encode(TYPE_AUDIO, pcm))

    async def close(self, reason: str = "") -> None:
        self._closed = True
        self._keepalive.cancel()
        try:
            await self._loop.run_in_executor(None, self._ser.close)
        except Exception:  # noqa: BLE001
            pass
        self._wexec.shutdown(wait=False)


class BluetoothConnector:
    """Mantiene abierto el puerto serie de cada nodo configurado (reintentos con backoff)."""

    def __init__(
        self,
        cfg: BluetoothCfg,
        on_link: Callable[[Link], Awaitable[None]],
        wifi_alive: Callable[[str], bool],
    ) -> None:
        self._cfg = cfg
        self._on_link = on_link
        self._wifi_alive = wifi_alive
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for link_cfg in self._cfg.links:
            self._tasks.append(asyncio.create_task(self._run_port(link_cfg)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @staticmethod
    def _open(link_cfg: BtLinkCfg):
        import serial  # pyserial

        return serial.Serial(port=link_cfg.port, baudrate=link_cfg.baudrate, timeout=0.05, write_timeout=2.0)

    async def _run_port(self, link_cfg: BtLinkCfg) -> None:
        loop = asyncio.get_running_loop()
        delay = self._cfg.retry_min_s
        first_failure = True
        while True:
            if self._wifi_alive(link_cfg.mac):
                await asyncio.sleep(5.0)
                continue
            try:
                ser = await loop.run_in_executor(None, self._open, link_cfg)
            except Exception as exc:  # noqa: BLE001 - nodo fuera de alcance, puerto inexistente, etc.
                (log.warning if first_failure else log.debug)(
                    "Bluetooth %s (%s): no se pudo abrir (%s); reintento en %.0fs",
                    link_cfg.port, link_cfg.mac, exc, delay,
                )
                first_failure = False
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, self._cfg.retry_max_s)
                continue
            delay = self._cfg.retry_min_s
            first_failure = True
            log.info("Bluetooth %s abierto (%s)", link_cfg.port, link_cfg.mac)
            link = BtLink(ser, loop, link_cfg.port)
            try:
                await self._on_link(link)
            finally:
                await link.close("fin de sesión")
            await asyncio.sleep(1.0)
