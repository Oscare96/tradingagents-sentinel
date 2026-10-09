"""Append-only JSON event store plus small mutable control/lease records.

Postgres recommended for deployments; SQLite is local development only.
Trading state is rebuilt from persisted events, never a process-global portfolio.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select, update
from sqlalchemy.exc import IntegrityError

from .models import timestamp, utcnow


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


class Store:
    def __init__(self, url=None):
        url = url or os.getenv("MASTER_DATABASE_URL", "sqlite:///master_trader.db")
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, pool_pre_ping=True)
        metadata = MetaData()
        self.events = Table(
            "master_events",
            metadata,
            Column("seq", Integer, primary_key=True),
            Column("event_id", String(200), unique=True, nullable=False),
            Column("kind", String(60), nullable=False),
            Column("created_at", String(50), nullable=False),
            Column("payload", Text, nullable=False),
            Column("digest", String(64), nullable=False),
        )
        self.kv = Table(
            "master_state",
            metadata,
            Column("key", String(120), primary_key=True),
            Column("value", Text),
        )
        metadata.create_all(self.engine)

    def append(self, kind, payload, event_id):
        raw = canonical(payload)
        with self.engine.begin() as conn:
            try:
                conn.execute(
                    self.events.insert().values(
                        event_id=event_id,
                        kind=kind,
                        created_at=timestamp(),
                        payload=raw,
                        digest=hashlib.sha256(raw.encode()).hexdigest(),
                    )
                )
            except IntegrityError:
                return False
        return True

    def list(self, kind=None, limit=None):
        query = select(self.events).order_by(self.events.c.seq.desc())
        if limit is not None:
            query = query.limit(limit)
        if kind:
            query = query.where(self.events.c.kind == kind)
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in reversed(rows)]

    def get(self, key, default=None):
        with self.engine.connect() as conn:
            value = conn.execute(select(self.kv.c.value).where(self.kv.c.key == key)).scalar()
        return json.loads(value) if value else default

    def set(self, key, value):
        with self.engine.begin() as conn:
            count = conn.execute(
                update(self.kv).where(self.kv.c.key == key).values(value=canonical(value))
            ).rowcount
            if not count:
                conn.execute(self.kv.insert().values(key=key, value=canonical(value)))

    def acquire(self, owner, seconds=180, key="execution_lease"):
        """Database compare-and-swap lease prevents two worker replicas trading."""
        now = timestamp()
        until = (utcnow() + timedelta(seconds=seconds)).isoformat() + "|" + owner
        with self.engine.begin() as conn:
            result = conn.execute(
                update(self.kv)
                .where(self.kv.c.key == key)
                .where(self.kv.c.value < now)
                .values(value=until)
            )
            if result.rowcount:
                return until
            exists = conn.execute(select(self.kv.c.key).where(self.kv.c.key == key)).scalar()
            if not exists:
                try:
                    conn.execute(self.kv.insert().values(key=key, value=until))
                    return until
                except IntegrityError:
                    return False
        return False

    def release(self, token, key="execution_lease"):
        with self.engine.begin() as conn:
            conn.execute(
                update(self.kv)
                .where(self.kv.c.key == key)
                .where(self.kv.c.value == token)
                .values(value="")
            )

    def lease_valid(self, token, seconds=30, key="execution_lease"):
        if not token:
            return False
        if datetime.fromisoformat(token.split("|")[0]) <= utcnow() + timedelta(seconds=seconds):
            return False
        with self.engine.connect() as conn:
            return (
                conn.execute(select(self.kv.c.value).where(self.kv.c.key == key)).scalar() == token
            )
