"""Cliente HTTP del backend (alivia-web-services): GET /api/v1/edge/device-table y PUT /api/v1/edge/sync-state."""
from __future__ import annotations

import logging

import httpx

from alivia_edge.access.application.ports import (
    BackendUnauthorized, BackendUnavailable, EdgeNotAssigned, Fetched, TableVersionAhead,
)
from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.errors import InvalidMacAddress
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress

log = logging.getLogger(__name__)

TABLE_PATH = "/api/v1/edge/device-table"
SYNC_PATH = "/api/v1/edge/sync-state"


def parse_table(data: dict) -> EdgeAccessTable:
    """JSON del backend -> tabla. Un nodo ilegible se descarta (queda denegado) sin invalidar el resto."""
    nodes: list[NodeRef] = []
    for item in data.get("nodes") or []:
        try:
            nodes.append(NodeRef(
                device_id=int(item["deviceId"]),
                mac=MacAddress(item["macAddress"]),
                device_type=DeviceType(str(item["deviceType"]).upper()),
                status=AssetStatus(str(item["status"]).upper()),
                room_key=str(item.get("roomKey") or ""),
                function=str(item.get("function") or ""),
            ))
        except (KeyError, ValueError, TypeError, InvalidMacAddress) as exc:
            log.warning("Nodo de la tabla ignorado (%s): %r", exc, item)
    return EdgeAccessTable(int(data["installationId"]), int(data["householdId"]), int(data["version"]), tuple(nodes))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


class HttpBackendGateway:
    def __init__(self, base_url: str, credential: str, *, request_timeout_s: float = 10.0, max_wait_s: int = 25) -> None:
        if not credential:
            raise ValueError("falta la credencial del edge (formato <edgeId>.<secreto>)")
        self._wait_margin = request_timeout_s
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
            timeout=httpx.Timeout(request_timeout_s, read=max_wait_s + request_timeout_s),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_table(self, etag: str | None, wait_s: int) -> Fetched:
        headers = {"If-None-Match": etag} if etag else {}
        params = {"wait": str(wait_s)} if (etag and wait_s > 0) else None
        try:
            r = await self._client.get(TABLE_PATH, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"red: {exc.__class__.__name__}") from exc
        if r.status_code == 200:
            try:
                return Fetched(parse_table(r.json()))
            except (ValueError, KeyError, TypeError) as exc:
                raise BackendUnavailable(f"tabla ilegible: {exc}") from exc
        if r.status_code == 304:
            return Fetched(None)
        self._raise(r)

    async def acknowledge(self, version: int) -> None:
        try:
            r = await self._client.put(SYNC_PATH, json={"appliedVersion": version})
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"red: {exc.__class__.__name__}") from exc
        if r.status_code == 200:
            return
        if r.status_code == 422:  # la tabla del backend aún no llegó a esa versión: no es grave
            log.warning("El backend aún no alcanza la versión %s (422)", version)
            return
        self._raise(r)

    @staticmethod
    def _raise(r: httpx.Response):
        code = r.status_code
        if code in (401, 403):
            raise BackendUnauthorized(f"HTTP {code}")
        if code == 404:
            raise EdgeNotAssigned("HTTP 404")
        if code == 409:
            raise TableVersionAhead("HTTP 409")
        if code in (429, 502, 503, 504):
            raise BackendUnavailable(f"HTTP {code}", _retry_after(r))
        raise BackendUnavailable(f"HTTP {code}")
