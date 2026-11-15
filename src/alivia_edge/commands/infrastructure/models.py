from __future__ import annotations

from peewee import AutoField, CharField, DateTimeField, IntegerField, Model

from alivia_edge.shared.infrastructure.database import db


class CommandRecordModel(Model):
    id = AutoField()
    node = CharField(max_length=17)
    device_id = IntegerField(null=True)
    action = CharField(max_length=32)
    status = CharField(max_length=16)
    detail = CharField(default="")
    created_at = DateTimeField(index=True)

    class Meta:
        database = db
        table_name = "command_records"
