import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The application engine is created at import time, so point it at a throw-away
# file database before any src module is imported.
_APP_DB = Path(tempfile.mkdtemp(prefix="mill-api-")) / "app.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_APP_DB}"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ.setdefault("SEED_DEMO", "false")

from src.config.database import Base  # noqa: E402
from src.models.entities import Batch, Inspection  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'test.db'}", future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def Session(engine):
    return sessionmaker(engine, expire_on_commit=False, future=True)


@pytest.fixture()
def db(Session):
    with Session() as session:
        yield session


@pytest.fixture()
def statements(engine):
    captured = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    yield captured


@pytest.fixture()
def batch(db):
    obj = Batch(code="QH-TEST-A", line="一线", supplier="测试", tonnes=20, received_date="2026-11-20")
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture()
def add_samples(db, batch):
    def _add(count, base_yield=73.0, line="一线", sampled_at=None, start=0, code_prefix=None,
             recorded_at="same"):
        prefix = code_prefix or batch.code
        for i in range(count):
            # recorded_at="same" 表示当场录入；传具体时刻用于构造迟到样本；None 表示旧记录无录入时刻。
            entered = sampled_at if recorded_at == "same" else recorded_at
            db.add(
                Inspection(
                    sample_code=f"{prefix}-S{start+i:03}",
                    batch_id=batch.id,
                    flour_yield=round(base_yield + (i % 5) * 0.1, 4),
                    moisture=12.5,
                    inspector="林洁",
                    sampled_at=sampled_at,
                    recorded_at=entered,
                )
            )
        db.commit()

    return _add
