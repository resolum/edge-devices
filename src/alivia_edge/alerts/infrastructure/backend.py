"""Cliente HTTP de POST /api/v1/edge/events (alivia-web-services)."""
from __future__ import annotations

import httpx

from alivia_edge.alerts.domain import EdgeEvent

EVENTS_PATH = "/api/v1/edge/events"


class RetryableError(Exception):
    """Sin red, o el backend pidió reintentar (429, 5xx). `retry_after` en segundos si lo indicó."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class RejectedError(Exception):
    """El backend rechazó el evento (400, 401, 403, 404): reintentarlo no cambia nada."""


class HttpEventSender:
    def __init__(self, base_url: str, credential: str, *, request_timeout_s: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not credential:
            raise ValueError("falta la credencial del edge (formato <edgeId>.<secreto>)")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=request_timeout_s, transport=transport,
            headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, event: EdgeEvent) -> int:
        """Entrega el evento; devuelve cuántos familiares se notificaron."""
        try:
            r = await self._client.post(EVENTS_PATH, json=event.to_json())
        except httpx.HTTPError as exc:
            raise RetryableError(f"red: {exc.__class__.__name__}") from exc
        if r.status_code in (200, 201, 202):
            try:
                return int(r.json().get("notified", 0))
            except (ValueError, AttributeError, TypeError):
                return 0
        if r.status_code == 429 or r.status_code >= 500:
            try:
                retry_after = float(r.headers.get("Retry-After", ""))
            except ValueError:
                retry_after = None
            raise RetryableError(f"HTTP {r.status_code}", retry_after)
        raise RejectedError(f"HTTP {r.status_code}")
