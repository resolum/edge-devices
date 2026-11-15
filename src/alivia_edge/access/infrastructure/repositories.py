from __future__ import annotations

from datetime import datetime, timezone

from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress
from alivia_edge.access.infrastructure.models import AccessNodeModel, AccessTableModel
from alivia_edge.shared.infrastructure.database import db


class SqliteAccessTableRepository:
    """Guarda la última tabla completa. Cada guardado reemplaza a la anterior dentro de una transacción."""

    def load(self) -> EdgeAccessTable | None:
        row = AccessTableModel.select().order_by(AccessTableModel.id.desc()).first()
        if row is None:
            return None
        nodes = []
        for n in AccessNodeModel.select().where(AccessNodeModel.table == row):
            nodes.append(NodeRef(
                n.device_id, MacAddress(n.mac), DeviceType(n.device_type), AssetStatus(n.status), n.room_key, n.function
            ))
        return EdgeAccessTable(row.installation_id, row.household_id, row.version, tuple(nodes))

    def save(self, table: EdgeAccessTable) -> None:
        with db.atomic():
            AccessTableModel.delete().execute()  # los nodos caen en cascada
            row = AccessTableModel.create(
                installation_id=table.installation_id,
                household_id=table.household_id,
                version=table.version,
                saved_at=datetime.now(timezone.utc),
            )
            if table.nodes:
                AccessNodeModel.insert_many([
                    {"table": row.id, "device_id": n.device_id, "mac": n.mac.value, "device_type": n.device_type.value,
                     "status": n.status.value, "room_key": n.room_key, "function": n.function}
                    for n in table.nodes
                ]).execute()

    def clear(self) -> None:
        with db.atomic():
            AccessTableModel.delete().execute()
