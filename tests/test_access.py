"""Contexto access: dominio, política, persistencia y sincronización con el backend (simulado con el contrato real)."""
import asyncio

import pytest

from alivia_edge.access.application.ports import (
    BackendUnauthorized, BackendUnavailable, EdgeNotAssigned, TableVersionAhead,
)
from alivia_edge.access.application.services import AccessService
from alivia_edge.access.application.sync import AccessSyncService
from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.errors import AccessDenied, InvalidMacAddress
from alivia_edge.access.domain.policy import AccessPolicy
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress
from alivia_edge.access.infrastructure.backend_client import HttpBackendGateway, parse_table
from alivia_edge.access.infrastructure.models import AccessNodeModel, AccessTableModel
from alivia_edge.access.infrastructure.repositories import SqliteAccessTableRepository
from alivia_edge.shared.infrastructure.database import close_db, init_db

from .fake_backend import CREDENTIAL, FakeBackend, node
from .fakes import MemoryAccessRepository, sample_table

MIC = MacAddress("AA:BB:CC:00:00:01")


# ------------------------------------------------------------------ dominio
def test_mac_is_normalized_like_the_backend():
    assert MacAddress("aa-bb-cc-00-00-01").value == "AA:BB:CC:00:00:01"
    assert MacAddress(" aa:bb:cc:00:00:01 ") == MIC
    for bad in ("", "AA:BB:CC", "ZZ:BB:CC:00:00:01", "alivia-mic-01", "AABBCC000001"):
        with pytest.raises(InvalidMacAddress):
            MacAddress(bad)


def test_table_supersedes_rules():
    v1, v2 = sample_table(version=1), sample_table(version=2)
    assert v2.supersedes(v1) and v1.supersedes(None)
    assert not v1.supersedes(v2) and not v2.supersedes(v2)  # vieja o repetida: se ignora
    assert sample_table(version=1, installation=9).supersedes(v2)  # otro hogar: aunque la versión sea menor
    assert v1.etag == '"7.1"'


@pytest.mark.parametrize(
    "status,reason",
    [(AssetStatus.MAINTENANCE, "mantenimiento"), (AssetStatus.BROKEN, "averiado"),
     (AssetStatus.RETIRED, "retirado"), (AssetStatus.AVAILABLE, "sin asignar")],
)
def test_only_in_use_devices_are_allowed(status, reason):
    table = EdgeAccessTable(7, 3, 1, (
        NodeRef(1, MIC, DeviceType.MICROPHONE, status, "sala", ""),
        NodeRef(11, MacAddress("AA:BB:CC:00:00:11"), DeviceType.LIGHT, status, "sala", ""),
    ))
    with pytest.raises(AccessDenied, match=reason):
        AccessPolicy.authorize_microphone(table, MIC)
    with pytest.raises(AccessDenied, match=reason):
        AccessPolicy.authorize_actuator(table, 11)


def test_policy_denies_everything_else():
    table = sample_table()
    assert AccessPolicy.authorize_microphone(table, MIC).room_key == "sala"
    assert AccessPolicy.authorize_actuator(table, 11).device_type is DeviceType.LIGHT
    with pytest.raises(AccessDenied, match="sin tabla"):
        AccessPolicy.authorize_microphone(None, MIC)
    with pytest.raises(AccessDenied, match="sin tabla"):
        AccessPolicy.authorize_actuator(None, 11)
    with pytest.raises(AccessDenied, match="no registrado"):
        AccessPolicy.authorize_microphone(table, MacAddress("AA:BB:CC:99:99:99"))
    with pytest.raises(AccessDenied, match="no es un micrófono"):  # una puerta no puede enviar audio
        AccessPolicy.authorize_microphone(table, MacAddress("AA:BB:CC:00:00:10"))
    with pytest.raises(AccessDenied, match="no es un actuador"):  # un micrófono no recibe comandos
        AccessPolicy.authorize_actuator(table, 1)
    with pytest.raises(AccessDenied, match="ya no está"):
        AccessPolicy.authorize_actuator(table, 999)


def test_access_service_applies_only_newer_notifies_and_clears():
    svc = AccessService(MemoryAccessRepository())
    seen = []
    svc.subscribe(lambda t: seen.append(t.version if t else None))
    assert svc.apply(sample_table(version=3))
    assert not svc.apply(sample_table(version=2)) and not svc.apply(sample_table(version=3))
    assert svc.table.version == 3 and seen == [3]
    assert svc.apply(sample_table(version=1), force=True)  # el backend retrocedió (409): se acepta aunque sea menor
    assert svc.table.version == 1
    svc.clear("prueba")
    assert svc.table is None and seen == [3, 1, None]
    with pytest.raises(AccessDenied):
        svc.authorize_microphone(MIC)


# ------------------------------------------------------------------ persistencia
def test_sqlite_repository_roundtrip_survives_restart(tmp_path):
    path = tmp_path / "edge.db"
    init_db(path, [AccessTableModel, AccessNodeModel])
    repo = SqliteAccessTableRepository()
    assert repo.load() is None
    repo.save(sample_table(version=5))
    repo.save(sample_table(version=6))  # reemplaza, no acumula
    assert AccessTableModel.select().count() == 1 and AccessNodeModel.select().count() == 4
    close_db()
    init_db(path, [AccessTableModel, AccessNodeModel])  # "reinicio del edge"
    svc = AccessService(SqliteAccessTableRepository())
    table = svc.load()
    assert table == sample_table(version=6)
    assert svc.authorize_microphone(MIC).room_key == "sala"
    repo.clear()
    assert repo.load() is None
    close_db()


def test_parse_table_drops_unreadable_nodes_only():
    data = {"installationId": 7, "householdId": 3, "version": 4, "nodes": [
        node(1, "aa:bb:cc:00:00:01", "MICROPHONE"),
        node(2, "no-es-mac", "LIGHT"),
        node(3, "AA:BB:CC:00:00:03", "ROBOT"),  # tipo futuro desconocido
        node(4, "AA:BB:CC:00:00:04", "DOOR", status="BROKEN"),
    ]}
    t = parse_table(data)
    assert [n.device_id for n in t.nodes] == [1, 4] and t.version == 4
    assert t.nodes[0].mac == MIC


# ------------------------------------------------------------------ sincronización con el backend
NODES = [node(1, "AA:BB:CC:00:00:01", "MICROPHONE"), node(10, "AA:BB:CC:00:00:10", "DOOR", room="entrada")]


@pytest.fixture
def backend():
    b = FakeBackend(NODES, version=3).start()
    yield b
    b.stop()


def make_sync(backend, access=None, credential=CREDENTIAL, wait_s=1):
    access = access or AccessService(MemoryAccessRepository())
    gw = HttpBackendGateway(backend.url, credential, request_timeout_s=3.0, max_wait_s=wait_s)
    return access, AccessSyncService(access, gw, wait_s=wait_s, retry_min_s=0.05, retry_max_s=0.2, idle_retry_s=0.1), gw


async def test_first_sync_downloads_table_and_acknowledges_version(backend):
    access, sync, gw = make_sync(backend)
    await sync.sync_once()
    assert access.table.version == 3 and [n.device_id for n in access.table.nodes] == [1, 10]
    assert backend.acks == [3]
    assert backend.requests[0]["inm"] is None and "wait" not in backend.requests[0]["query"]  # sin tabla: respuesta inmediata
    assert access.authorize_microphone(MIC)
    await gw.aclose()


async def test_up_to_date_edge_gets_304_after_the_wait_and_does_not_repeat_ack(backend):
    access, sync, gw = make_sync(backend)
    await sync.sync_once()
    t0 = asyncio.get_running_loop().time()
    await sync.sync_once()  # If-None-Match vigente + ?wait=1
    assert asyncio.get_running_loop().time() - t0 >= 0.9
    assert backend.requests[-1]["inm"] == '"7.3"' and "wait=1" in backend.requests[-1]["query"]
    assert backend.acks == [3] and access.table.version == 3
    await gw.aclose()


async def test_long_poll_returns_as_soon_as_the_table_changes(backend):
    access, sync, gw = make_sync(backend, wait_s=5)
    await sync.sync_once()
    changes = []
    access.subscribe(lambda t: changes.append(t.version))
    task = asyncio.create_task(sync.sync_once())
    await asyncio.sleep(0.2)
    t0 = asyncio.get_running_loop().time()
    backend.change(NODES + [node(11, "AA:BB:CC:00:00:11", "LIGHT")])
    await asyncio.wait_for(task, 3)
    assert asyncio.get_running_loop().time() - t0 < 1.0  # no esperó los 5 s
    assert access.table.version == 4 and len(access.table.nodes) == 3 and changes == [4]
    assert backend.acks == [3, 4]
    await gw.aclose()


async def test_bearer_credential_is_required(backend):
    _, sync, gw = make_sync(backend, credential="5.otra")
    with pytest.raises(BackendUnauthorized):
        await sync.sync_once()
    await gw.aclose()


async def test_errors_map_to_domain_exceptions(backend):
    access, sync, gw = make_sync(backend)
    for mode, exc in (("404", EdgeNotAssigned), ("503", BackendUnavailable), ("429", BackendUnavailable), ("bad-json", BackendUnavailable)):
        backend.mode = mode
        with pytest.raises(exc):
            await sync.sync_once()
    backend.mode, backend.retry_after = "503", 7
    with pytest.raises(BackendUnavailable) as e:
        await sync.sync_once()
    assert e.value.retry_after_s == 7.0
    await gw.aclose()


async def test_version_ahead_409_forces_full_table_even_if_lower(backend):
    access, sync, gw = make_sync(backend)
    access.apply(sample_table(version=9, installation=7))  # el edge cree estar en la 9; el backend retrocedió a la 3
    with pytest.raises(TableVersionAhead):
        await sync.sync_once()
    await sync.sync_once(force_full=True)  # sin If-None-Match: toma lo que reciba
    assert access.table.version == 3 and backend.requests[-2]["inm"] is None
    await gw.aclose()


async def test_run_loop_survives_outage_applies_changes_and_handles_401_404(backend):
    access, sync, gw = make_sync(backend)
    sync.start()

    async def until(pred, timeout=4.0):
        async def _w():
            while not pred():
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_w(), timeout)

    await until(lambda: access.table is not None and access.table.version == 3)

    backend.mode = "503"  # caída del backend: el edge sigue con la última tabla
    await asyncio.sleep(0.4)
    assert access.table.version == 3 and sync.last_error
    backend.mode = "ok"
    backend.change(NODES + [node(11, "AA:BB:CC:00:00:11", "LIGHT")])
    await until(lambda: access.table.version == 4)
    assert sync.last_error == ""

    backend.mode = "401"  # credencial revocada: se conserva la última tabla (decisión de negocio) y se deja la alarma
    await asyncio.sleep(0.4)
    assert access.table is not None and access.table.version == 4 and sync.last_error == "credencial rechazada"

    backend.mode = "404"  # el edge ya no pertenece a ninguna instalación: se niega todo
    await until(lambda: access.table is None)
    with pytest.raises(AccessDenied):
        access.authorize_microphone(MIC)

    backend.mode = "ok"  # vuelve a asignarse: se recupera sola
    await until(lambda: access.table is not None and access.table.version == 4)
    await sync.stop()


# ------------------------------------------------------------------ flujo completo: backend -> edge -> ESP32
async def test_full_flow_backend_table_decides_who_can_send_data():
    """El backend (simulado con su contrato real) manda la tabla; el edge permite o niega al ESP32 y reacciona a los cambios."""
    from alivia_edge.voice.interfaces.server import EdgeServer

    from .fakes import FAKE_MAC, FakeNode, make_access, make_config, make_services

    backend = FakeBackend([node(1, FAKE_MAC, "MICROPHONE"), node(10, "AA:BB:CC:00:00:10", "DOOR", room="entrada")], version=1).start()
    access = make_access(False)  # el edge arranca sin tabla guardada
    svc = make_services(make_config(), llm_script=[], stt_script=[], access=access)
    server = EdgeServer(svc)
    await server.start()
    url = f"ws://127.0.0.1:{server.port}/ws"
    _, sync, gw = make_sync(backend, access=access, wait_s=5)
    try:
        # 1) sin tabla: el ESP32 es rechazado
        denied = await FakeNode().connect(url)
        assert denied["type"] == "error" and "sin tabla" in denied["reason"]

        # 2) el edge arranca su sincronización: llega la tabla y el ESP32 ya puede conectarse
        sync.start()
        for _ in range(200):
            if access.table:
                break
            await asyncio.sleep(0.02)
        node1 = FakeNode()
        assert (await node1.connect(url)) == {"type": "ready"}
        assert backend.acks == [1]  # el backend sabe que el edge aplicó la versión 1

        # 3) el administrador manda el micrófono a mantenimiento: la espera larga lo trae al instante y se corta la sesión
        backend.change([node(1, FAKE_MAC, "MICROPHONE", status="MAINTENANCE"), node(10, "AA:BB:CC:00:00:10", "DOOR", room="entrada")])
        await asyncio.wait_for(node1.closed.wait(), 3)
        assert any(m.get("reason") == "acceso revocado" for m in node1.messages)
        again = await FakeNode().connect(url)
        assert again["type"] == "error" and "mantenimiento" in again["reason"]
        assert backend.acks[-1] == 2

        # 4) lo repara y vuelve a asignarlo: se reconecta sin tocar el edge
        backend.change([node(1, FAKE_MAC, "MICROPHONE"), node(10, "AA:BB:CC:00:00:10", "DOOR", room="entrada")])
        for _ in range(200):
            if access.table.version == 3:
                break
            await asyncio.sleep(0.02)
        node2 = FakeNode()
        assert (await node2.connect(url)) == {"type": "ready"}
        await node2.close()
    finally:
        await sync.stop()
        await server.stop()
        backend.stop()
