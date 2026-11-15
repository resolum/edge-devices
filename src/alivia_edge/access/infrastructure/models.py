from __future__ import annotations

from peewee import AutoField, CharField, DateTimeField, ForeignKeyField, IntegerField, Model

from alivia_edge.shared.infrastructure.database import db


class AccessTableModel(Model):
    """Una sola fila: la tabla vigente."""

    id = AutoField()
    installation_id = IntegerField()
    household_id = IntegerField()
    version = IntegerField()
    saved_at = DateTimeField()

    class Meta:
        database = db
        table_name = "access_tables"


class AccessNodeModel(Model):
    table = ForeignKeyField(AccessTableModel, backref="nodes", on_delete="CASCADE")
    device_id = IntegerField()
    mac = CharField(max_length=17)
    device_type = CharField(max_length=16)
    status = CharField(max_length=16)
    room_key = CharField(default="")
    function = CharField(default="")

    class Meta:
        database = db
        table_name = "access_nodes"
        indexes = ((("table", "device_id"), True),)
