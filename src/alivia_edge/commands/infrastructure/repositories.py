from __future__ import annotations

from alivia_edge.commands.domain.models import CommandRecord
from alivia_edge.commands.infrastructure.models import CommandRecordModel


class SqliteCommandHistoryRepository:
    def add(self, record: CommandRecord) -> None:
        CommandRecordModel.create(
            node=record.node, device_id=record.device_id, action=record.action,
            status=record.status, detail=record.detail[:250], created_at=record.created_at,
        )

    def recent(self, limit: int = 20) -> list[CommandRecord]:
        rows = CommandRecordModel.select().order_by(CommandRecordModel.id.desc()).limit(limit)
        return [CommandRecord(r.node, r.device_id, r.action, r.status, r.detail, r.created_at) for r in rows]
