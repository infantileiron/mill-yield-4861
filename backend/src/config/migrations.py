"""Lightweight in-place schema upgrade.

`Base.metadata.create_all` creates new tables but never adds columns to tables that
already exist, so an installation created before the rolling index would be missing
the sampling-time columns. These statements are idempotent and intentionally small.
"""

import logging

from sqlalchemy import inspect, text

from src.config.database import Base, engine
from src.models import entities  # noqa: F401 - 注册所有表到 metadata

log = logging.getLogger(__name__)

COLUMNS = {
    "inspections": {
        "sampled_at": "TIMESTAMP WITH TIME ZONE",
        "recorded_at": "TIMESTAMP WITH TIME ZONE",
    },
    "reviews": {
        "source": "VARCHAR(20) DEFAULT 'legacy'",
        "index_generation_id": "INTEGER",
        "index_watermark": "BIGINT",
        "index_entry_count": "INTEGER",
        "index_lagging": "BOOLEAN DEFAULT FALSE",
        "window_hours": "INTEGER",
        "window_from": "TIMESTAMP WITH TIME ZONE",
        "window_to": "TIMESTAMP WITH TIME ZONE",
        "legacy_count": "INTEGER DEFAULT 0",
    },
    "yield_index_generations": {
        "copy_done": "BOOLEAN DEFAULT FALSE",
        "needs_reconcile": "BOOLEAN DEFAULT TRUE",
    },
}

SQLITE_COLUMNS = {
    "inspections": {"sampled_at": "DATETIME", "recorded_at": "DATETIME"},
    "reviews": {
        "source": "VARCHAR(20) DEFAULT 'legacy'",
        "index_generation_id": "INTEGER",
        "index_watermark": "BIGINT",
        "index_entry_count": "INTEGER",
        "index_lagging": "BOOLEAN DEFAULT 0",
        "window_hours": "INTEGER",
        "window_from": "DATETIME",
        "window_to": "DATETIME",
        "legacy_count": "INTEGER DEFAULT 0",
    },
    "yield_index_generations": {
        "copy_done": "BOOLEAN DEFAULT 0",
        "needs_reconcile": "BOOLEAN DEFAULT 1",
    },
}

INDEXES = [
    ("ix_inspections_sampled_at", "inspections", "(sampled_at)"),
    ("ix_yield_index_gen_line_time", "yield_index_entries", "(generation_id, line, sampled_at)"),
    ("ix_yield_index_gen_time", "yield_index_entries", "(generation_id, sampled_at)"),
    ("ix_yield_index_gen_inspection", "yield_index_entries", "(generation_id, inspection_id)"),
]


def ensure_schema(bind=engine) -> None:
    Base.metadata.create_all(bind)
    dialect = bind.dialect.name
    wanted = SQLITE_COLUMNS if dialect == "sqlite" else COLUMNS
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as conn:
        for table, columns in wanted.items():
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
                log.info("schema upgrade: %s.%s added", table, name)
        for name, table, columns in INDEXES:
            if table not in existing_tables:
                continue
            try:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}"))
            except Exception:  # pragma: no cover - 索引已存在或方言不支持
                log.debug("index %s already present", name)
        # 旧复核单按原口径归类；旧检验记录没有可靠录入时刻，保持 NULL。
        if "reviews" in existing_tables:
            conn.execute(text("UPDATE reviews SET source='legacy' WHERE source IS NULL"))
            conn.execute(text("UPDATE reviews SET legacy_count=0 WHERE legacy_count IS NULL"))
            conn.execute(text("UPDATE reviews SET index_lagging=FALSE WHERE index_lagging IS NULL"))
