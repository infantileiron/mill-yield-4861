import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from src.config.database import Session
from src.models.entities import Batch, Inspection, Review, YieldIndexGeneration
from src.services import rolling_index as ri
from src.services.statistics import diagnose
import src.worker as worker


@pytest.fixture()
def clean_schema():
    from src.config.migrations import ensure_schema
    from src.config.database import engine

    ensure_schema(engine)
    with Session() as db:
        for model in (Review, Inspection, Batch, YieldIndexGeneration):
            db.query(model).delete()
        db.commit()
    yield
    with Session() as db:
        for model in (Review, Inspection, Batch, YieldIndexGeneration):
            db.query(model).delete()
        db.commit()


def test_worker_builds_index_from_queue(clean_schema, monkeypatch):
    with Session() as db:
        batch = Batch(code="W-1", line="二线", supplier="s", tonnes=1, received_date="2026-11-20")
        db.add(batch)
        db.flush()
        now = datetime.now(timezone.utc)
        for i in range(6):
            db.add(Inspection(sample_code=f"W-1-{i}", batch_id=batch.id, flour_yield=70 + i,
                              moisture=12, inspector="x", sampled_at=now - timedelta(hours=1),
                              recorded_at=now - timedelta(hours=1)))
        db.commit()
        generation = ri.begin_build(db)

    monkeypatch.setattr(worker, "recover_on_start", lambda: None)
    worker.run_index_build(generation.id)

    with Session() as db:
        status = ri.index_status(db)
        db.commit()
        assert status["active"]["generation_id"] == generation.id
        assert status["active"]["entry_count"] == 6


def test_worker_sync_failure_keeps_last_watermark(clean_schema, monkeypatch):
    with Session() as db:
        batch = Batch(code="W-2", line="二线", supplier="s", tonnes=1, received_date="2026-11-20")
        db.add(batch)
        db.flush()
        now = datetime.now(timezone.utc)
        for i in range(6):
            db.add(Inspection(sample_code=f"W-2-{i}", batch_id=batch.id, flour_yield=70 + i,
                              moisture=12, inspector="x", sampled_at=now - timedelta(hours=1),
                              recorded_at=now - timedelta(hours=1)))
        db.commit()
        ri.build(db)

    def boom(*args, **kwargs):
        raise RuntimeError("redis 超时")

    monkeypatch.setattr(ri, "_source_select", boom)
    worker.run_index_sync()

    with Session() as db:
        status = ri.index_status(db)
        db.commit()
        assert status["built"] is True
        assert status["lagging"] is True
        assert status["active"]["entry_count"] >= 6


def test_worker_retry_after_failure_does_not_double_count(clean_schema, monkeypatch):
    with Session() as db:
        batch = Batch(code="W-3", line="二线", supplier="s", tonnes=1, received_date="2026-11-20")
        db.add(batch)
        db.flush()
        now = datetime.now(timezone.utc)
        for i in range(6):
            db.add(Inspection(sample_code=f"W-3-{i}", batch_id=batch.id, flour_yield=70 + i,
                              moisture=12, inspector="x", sampled_at=now - timedelta(hours=1),
                              recorded_at=now - timedelta(hours=1)))
        db.commit()
        generation = ri.build(db)
        before = generation.entry_count

    worker.run_index_sync()
    worker.run_index_sync()
    worker.run_index_sync()

    with Session() as db:
        gen = db.get(YieldIndexGeneration, generation.id)
        assert gen.entry_count == before
        from src.models.entities import YieldIndexEntry
        from sqlalchemy import func

        assert db.scalar(select(func.count()).select_from(YieldIndexEntry)
                         .where(YieldIndexEntry.generation_id == generation.id)) == before


def test_worker_review_result_is_written(clean_schema, monkeypatch, tmp_path):
    snapshot = [
        {"sample_code": f"R-{i}", "batch_code": "B", "line": "一线", "flour_yield": 70 + i,
         "moisture": 12, "inspector": "x", "sampled_at": None, "late": False}
        for i in range(6)
    ]
    with Session() as db:
        review = Review(title="worker 复核", line="一线", status="queued", snapshot=snapshot,
                        result={}, created_by="analyst@demo.local")
        db.add(review)
        db.commit()
        review_id = review.id

    monkeypatch.setattr(worker, "ARTIFACT_DIR", tmp_path)
    worker.run_review(review_id)

    with Session() as db:
        obj = db.get(Review, review_id)
        assert obj.status == "completed"
        assert obj.result["n"] == 6
        assert obj.result["checksum"] == diagnose(snapshot)["checksum"]
    assert (tmp_path / f"review-{review_id}.json").exists()
