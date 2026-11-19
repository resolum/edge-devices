"""Cola de eventos hacia el backend y los avisos que la usan."""
from __future__ import annotations

import asyncio
import collections
import logging
from typing import Callable, Protocol

from alivia_edge.alerts.domain import HELP_REQUESTED, LOW_BATTERY, BatteryWatch, EdgeEvent

log = logging.getLogger(__name__)


class EventSender(Protocol):
    async def send(self, event: EdgeEvent) -> int: ...


class EventReporter:
    """Guarda los eventos en memoria (hasta `queue_max`) y los entrega en orden, con reintentos crecientes."""

    def __init__(self, sender: EventSender, *, queue_max: int = 200, retry_max_s: float = 60.0,
                 first_retry_s: float = 1.0) -> None:
        self._sender = sender
        self._pending: collections.deque[EdgeEvent] = collections.deque(maxlen=queue_max)
        self._wake = asyncio.Event()
        self._retry_max, self._first_retry = retry_max_s, first_retry_s
        self._task: asyncio.Task | None = None
        self.delivered = 0

    @property
    def pending(self) -> int:
        return len(self._pending)

    def submit(self, event: EdgeEvent) -> None:
        if len(self._pending) == self._pending.maxlen:
            log.warning("cola de eventos llena: se descarta el más antiguo")
        self._pending.append(event)
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="edge-events")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        from alivia_edge.alerts.infrastructure.backend import RejectedError, RetryableError

        delay = self._first_retry
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            event = self._pending[0]
            try:
                notified = await self._sender.send(event)
            except RetryableError as exc:
                wait = exc.retry_after if exc.retry_after is not None else delay
                log.warning("evento %s %s no entregado (%s); reintento en %.0f s", event.type, event.event_id, exc, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, self._retry_max)
                continue
            except RejectedError as exc:
                log.error("el backend rechazó el evento %s %s: %s; se descarta", event.type, event.event_id, exc)
            except Exception:  # noqa: BLE001
                log.exception("error inesperado al entregar el evento %s; se descarta", event.event_id)
            else:
                self.delivered += 1
                log.info("evento %s entregado: %d familiar(es) notificado(s)", event.type, notified)
            delay = self._first_retry
            if self._pending and self._pending[0] is event:
                self._pending.popleft()


class AlertService:
    """Lo que el nodo de voz avisa al familiar. `device_id_of(mac)` da el id del micrófono en la tabla del backend."""

    def __init__(self, events: EventReporter, battery: BatteryWatch,
                 device_id_of: Callable[[str], int | None] = lambda _mac: None) -> None:
        self._events, self._battery, self._device_id_of = events, battery, device_id_of

    def request_help(self, mac: str, words: str = "") -> None:
        log.warning("[%s] PEDIDO DE AYUDA: %r", mac, words)
        self._events.submit(EdgeEvent(HELP_REQUESTED, device_id=self._safe_device_id(mac), detail=words.strip()))

    def on_battery(self, mac: str, battery_pct) -> None:
        if self._battery.check(mac, battery_pct):
            self._events.submit(EdgeEvent(LOW_BATTERY, device_id=self._safe_device_id(mac),
                                          battery_pct=int(float(battery_pct))))

    def _safe_device_id(self, mac: str) -> int | None:
        try:
            return self._device_id_of(mac)
        except Exception:  # noqa: BLE001
            return None
