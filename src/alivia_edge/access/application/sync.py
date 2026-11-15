"""Sincronización con el backend: pide la tabla (ETag + espera larga), la aplica y confirma la versión."""
from __future__ import annotations

import asyncio
import logging

from alivia_edge.access.application.ports import (
    BackendGateway, BackendUnauthorized, BackendUnavailable, EdgeNotAssigned, TableVersionAhead,
)
from alivia_edge.access.application.services import AccessService

log = logging.getLogger(__name__)


class AccessSyncService:
    def __init__(
        self,
        access: AccessService,
        gateway: BackendGateway,
        *,
        wait_s: int = 25,
        retry_min_s: float = 2.0,
        retry_max_s: float = 60.0,
        idle_retry_s: float = 30.0,
    ) -> None:
        self._access = access
        self._gateway = gateway
        self._wait_s = max(0, min(wait_s, 25))  # el backend recorta a 25
        self._retry_min_s, self._retry_max_s, self._idle_retry_s = retry_min_s, retry_max_s, idle_retry_s
        self._acked: int | None = None
        self._acked_installation: int | None = None
        self._task: asyncio.Task | None = None
        self._backoff = retry_min_s
        self.last_error: str = ""

    # --- ciclo de vida ----------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="access-sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._gateway.aclose()

    async def run(self) -> None:
        force_full = False
        while True:
            delay = 0.0
            try:
                force_full = await self.sync_once(force_full)
                self._backoff = self._retry_min_s
                self.last_error = ""
            except TableVersionAhead:
                log.warning("El backend tiene una versión menor (¿restauración?): se pide la tabla completa")
                force_full = True
            except EdgeNotAssigned:
                self._access.clear("el edge no está asignado a una instalación activa (404)")
                self._set_error("edge sin instalación")
                delay = self._idle_retry_s
            except BackendUnauthorized:
                # Se conserva la última tabla: una credencial revocada no debe apagar la casa; se deja la alarma.
                log.error("El backend rechazó la credencial del edge (401/403). Se sigue con la última tabla guardada.")
                self._set_error("credencial rechazada")
                delay = self._idle_retry_s
            except BackendUnavailable as exc:
                self._set_error(str(exc))
                delay = exc.retry_after_s or self._backoff
                self._backoff = min(self._backoff * 2, self._retry_max_s)
                log.warning("Backend no disponible (%s); se reintenta en %.0f s", exc, delay)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("error inesperado sincronizando la tabla de acceso")
                self._set_error("error inesperado")
                delay = self._backoff
                self._backoff = min(self._backoff * 2, self._retry_max_s)
            if delay:
                await asyncio.sleep(delay)

    def _set_error(self, text: str) -> None:
        self.last_error = text

    # --- una vuelta -------------------------------------------------------------------------
    async def sync_once(self, force_full: bool = False) -> bool:
        """Una petición al backend. Devuelve True si la siguiente debe pedir la tabla completa (sin ETag)."""
        current = self._access.table
        etag = None if (force_full or current is None) else current.etag
        # Sin tabla o pidiendo la completa: respuesta inmediata. Con tabla: espera larga.
        wait = 0 if etag is None else self._wait_s
        fetched = await self._gateway.fetch_table(etag, wait)
        if fetched.table is not None:
            self._access.apply(fetched.table, force=force_full)
        await self._acknowledge()
        return False

    async def _acknowledge(self) -> None:
        table = self._access.table
        if table is None:
            return
        if self._acked_installation == table.installation_id and self._acked is not None and self._acked >= table.version:
            return
        try:
            await self._gateway.acknowledge(table.version)
        except (BackendUnavailable, BackendUnauthorized, EdgeNotAssigned) as exc:
            log.warning("No se pudo confirmar la versión %s: %s", table.version, exc)
            return
        self._acked, self._acked_installation = table.version, table.installation_id
