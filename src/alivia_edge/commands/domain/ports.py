from __future__ import annotations

from typing import Protocol

from alivia_edge.commands.domain.models import ActionResult, CommandRecord, CommandRequest


class CommandBus(Protocol):
    @property
    def connected(self) -> bool: ...

    async def execute(self, requests: list[CommandRequest], *, node: str, timeout_s: float) -> list[ActionResult]: ...


class CommandHistoryRepository(Protocol):
    def add(self, record: CommandRecord) -> None: ...

    def recent(self, limit: int = 20) -> list[CommandRecord]: ...
