"""Despacho de comandos: vuelve a autorizar CADA acción en el momento de ejecutar y registra el resultado."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from alivia_edge.access.domain.errors import AccessDenied
from alivia_edge.commands.domain.catalog import parse_device_key, payload_for
from alivia_edge.commands.domain.models import Action, ActionResult, CommandRecord, CommandRequest
from alivia_edge.commands.domain.ports import CommandBus, CommandHistoryRepository
from alivia_edge.commands.integration.access_acl import AccessAntiCorruptionLayer

log = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(self, bus: CommandBus, acl: AccessAntiCorruptionLayer, history: CommandHistoryRepository | None = None) -> None:
        self._bus, self._acl, self._history = bus, acl, history

    @property
    def connected(self) -> bool:
        return self._bus.connected

    async def execute(self, actions: list[Action], *, node: str, timeout_s: float) -> list[ActionResult]:
        results: dict[int, ActionResult] = {}
        requests: list[tuple[int, CommandRequest]] = []
        for i, a in enumerate(actions):
            device_id = parse_device_key(a.device)
            try:
                if device_id is None:
                    raise AccessDenied("dispositivo desconocido")
                target = self._acl.authorize(device_id)  # el estado de HOY, no el de cuando se armó el catálogo
                payload = payload_for(target.kind, a.action)
                if payload is None:
                    raise AccessDenied("acción no soportada por el dispositivo")
            except AccessDenied as exc:
                log.warning("[%s] comando denegado %s %s: %s", node, a.action, a.device, exc)
                results[i] = ActionResult(a, "denied", str(exc))
                continue
            requests.append((i, CommandRequest(a, target, payload)))

        if requests:
            sent = await self._bus.execute([r for _, r in requests], node=node, timeout_s=timeout_s)
            for (i, _), res in zip(requests, sent):
                results[i] = res

        ordered = [results[i] for i in range(len(actions))]
        self._record(node, ordered)
        return ordered

    def _record(self, node: str, results: list[ActionResult]) -> None:
        if self._history is None:
            return
        now = datetime.now(timezone.utc)
        for r in results:
            try:
                self._history.add(CommandRecord(node, parse_device_key(r.action.device), r.action.action, r.status, r.detail, now))
            except Exception:  # noqa: BLE001
                log.exception("no se pudo guardar el historial de comandos")
