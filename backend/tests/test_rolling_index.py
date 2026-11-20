from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from src.models.entities import Inspection, Review, YieldIndexEntry
from src.services import rolling_index as ri
from src.services.statistics import diagnose

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _rows(db, generation_id):
    return db.scalars(
        select(YieldIndexEntry).where(YieldIndexEntry.generation_id == generation_id)
    ).all()


def test_build_separates_timed_window_from_legacy_unknown_time(db, add_samples):
    add_samples(6, sampled_at=NOW - timedelta(hours=2), code_prefix="NEW")
    add_samples(4, sampled_at=None, code_prefix="OLD")
    add_samples(3, sampled_at=NOW - timedelta(hours=30), code_prefix="EXPIRED")

    generation = ri.build(db, now=NOW)

    assert generation.status == "active"
    assert generation.window_hours == 24
    rows = _rows(db, generation.id)
    timed = [r for r in rows if r.sampled_at is not None]
    legacy = [r for r in rows if r.sampled_at is None]
    assert len(timed) == 6
    assert len(legacy) == 4
    assert all(r.sample_code.startswith("NEW") for r in timed)

    report = ri.window_report(db, now=NOW)
    assert report["built"] and not report["lagging"]
    assert report["lines"]["一线"]["n"] == 6
    assert report["legacy"]["count"] == 4
    assert report["legacy"]["by_line"] == {"一线": 4}
    assert report["legacy"]["samples"][0]["sampled_at"] is None


def test_quantiles_use_linear_interpolation(db, add_samples):
    values = [70.0, 71.0, 72.0, 73.0, 74.0, 75.0]
    for i, v in enumerate(values):
        add_samples(1, base_yield=v, sampled_at=NOW - timedelta(hours=1), start=i, code_prefix="Q")
    generation = ri.build(db, now=NOW)
    summary = ri.line_summary(db, generation, "一线", now=NOW)
    assert summary["n"] == 6
    assert summary["q1"] == pytest.approx(71.25)
    assert summary["median"] == pytest.approx(72.5)
    assert summary["q3"] == pytest.approx(73.75)
    assert summary["iqr"] == pytest.approx(2.5)
    assert summary["lower"] == pytest.approx(67.5)
    assert summary["upper"] == pytest.approx(77.5)
    assert summary["outliers"]["total"] == 0
    assert sum(b["count"] for b in summary["histogram"]) == 6
    assert len(summary["histogram"]) == 20


def test_window_query_never_reads_raw_inspections(db, add_samples, engine):
    add_samples(8, sampled_at=NOW - timedelta(hours=1))
    generation = ri.build(db, now=NOW)

    with engine.connect() as conn:
        captured = []

        from sqlalchemy import event

        def _capture(c, cursor, statement, parameters, context, executemany):
            captured.append(statement)

        event.listen(conn, "before_cursor_execute", _capture)
        # Use the same connection for the ORM-free window SQL path.
        from sqlalchemy.orm import Session as SASession

        with SASession(bind=conn, expire_on_commit=False) as s:
            ri.window_report(s, now=NOW)
        event.remove(conn, "before_cursor_execute", _capture)

    assert captured
    assert all("inspections" not in sql.lower() for sql in captured), captured


def test_late_inspection_within_window_is_indexed_and_flagged(db, add_samples, Session):
    from src.services.business import add_inspection

    add_samples(5, sampled_at=NOW - timedelta(hours=3))
    ri.build(db, now=NOW)
    batch = db.scalar(select(__import__("src.models.entities", fromlist=["Batch"]).Batch))

    # Sampled 20h ago but entered now: still inside the 24h window -> indexed, marked late.
    result = add_inspection(db, {
        "sample_code": "LATE-1",
        "batch_id": batch.id,
        "flour_yield": 74.0,
        "moisture": 12.2,
        "inspector": "林洁",
        "sampled_at": NOW - timedelta(hours=20),
    })
    assert result["indexed"] == "inserted"
    assert result["late"] is True

    entry = db.scalar(select(YieldIndexEntry).where(YieldIndexEntry.sample_code == "LATE-1"))
    assert entry is not None
    report = ri.window_report(db, now=NOW)
    assert report["lines"]["一线"]["n"] == 6
    assert report["lines"]["一线"]["late_count"] == 1


def test_late_inspection_outside_window_is_not_indexed(db, add_samples):
    from src.services.business import add_inspection

    add_samples(5, sampled_at=NOW - timedelta(hours=3))
    ri.build(db, now=NOW)
    batch_id = db.scalar(select(Inspection.batch_id))
    result = add_inspection(db, {
        "sample_code": "LATE-OLD",
        "batch_id": batch_id,
        "flour_yield": 74.0,
        "moisture": 12.2,
        "inspector": "林洁",
        "sampled_at": NOW - timedelta(hours=30),
    })
    assert result["indexed"] == "out-of-window"
    assert db.scalar(select(func.count()).select_from(YieldIndexEntry).where(
        YieldIndexEntry.sample_code == "LATE-OLD")) == 0


def test_sync_increment_and_expiry_are_idempotent(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1), code_prefix="B")
    generation = ri.build(db, now=NOW)

    add_samples(2, sampled_at=NOW - timedelta(hours=2), start=10, code_prefix="C")
    first = ri.sync(db, now=NOW)
    assert first["inserted"] == 2
    assert first["expired"] == 0
    second = ri.sync(db, now=NOW)
    assert second["inserted"] == 0  # re-running does not double count

    # 25 hours later the original samples have left the window and are deleted.
    later = NOW + timedelta(hours=25)
    third = ri.sync(db, now=later)
    assert third["expired"] == 7
    remaining = _rows(db, generation.id)
    assert remaining == []
    assert generation.entry_count >= 0


def test_build_during_concurrent_inserts_has_no_gap_or_duplicate(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1), code_prefix="PRE")
    generation = ri.begin_build(db)
    # A committed inspection lands after the snapshot point but before activation.
    add_samples(3, sampled_at=NOW - timedelta(hours=1), start=50, code_prefix="DURING")
    ri.run_build(db, generation.id, now=NOW)

    rows = _rows(db, generation.id)
    ids = [r.inspection_id for r in rows]
    assert len(ids) == len(set(ids)) == 8
    assert generation.entry_count == 8
    codes = {r.sample_code for r in rows}
    assert {"PRE-S000", "DURING-S050"} <= codes


def test_failed_build_keeps_previous_watermark_and_marks_lagging(db, add_samples, monkeypatch):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    first = ri.build(db, now=NOW)
    good_watermark = first.applied_through_id

    def boom(*args, **kwargs):
        raise RuntimeError("维护故障")

    monkeypatch.setattr(ri, "copy_snapshot", boom)
    generation = ri.begin_build(db)
    with pytest.raises(RuntimeError):
        ri.run_build(db, generation.id, now=NOW)

    status = ri.index_status(db)
    db.commit()
    assert status["active"]["generation_id"] == first.id
    assert status["active"]["applied_through_id"] == good_watermark
    assert status["lagging"] is True
    assert "维护故障" in status["reason"]
    # The previous complete watermark still answers queries.
    report = ri.window_report(db, now=NOW)
    assert report["built"] and report["lagging"]
    assert report["lines"]["一线"]["n"] == 5


def test_sync_failure_keeps_serving_previous_watermark(db, add_samples, monkeypatch):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    ri.build(db, now=NOW)
    monkeypatch.setattr(ri, "_source_select", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("超时")))
    with pytest.raises(RuntimeError):
        ri.sync(db, now=NOW)
    db.rollback()
    ri.sync_failed(db, "超时")
    status = ri.index_status(db)
    db.commit()
    assert status["built"] is True
    assert status["lagging"] is True
    assert status["reason"]
    assert ri.window_report(db, now=NOW)["lines"]["一线"]["n"] == 5


def test_retry_after_recovery_reconciles_without_double_count(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    generation = ri.build(db, now=NOW)
    before = generation.entry_count

    # Simulate a committed record that never reached the index (crash between commit and push).
    add_samples(2, sampled_at=NOW - timedelta(hours=1), start=70, code_prefix="GAP")
    ri.sync(db, now=NOW, reconcile=True)
    ri.sync(db, now=NOW, reconcile=True)
    db.refresh(generation)
    assert generation.entry_count == before + 2
    assert db.scalar(select(func.count()).select_from(YieldIndexEntry).where(
        YieldIndexEntry.generation_id == generation.id)) == before + 2


def test_diagnosis_freezes_watermark_and_samples(db, add_samples):
    from src.services.business import submit_indexed

    class User:
        email = "analyst@demo.local"

    add_samples(6, sampled_at=NOW - timedelta(hours=2))
    generation = ri.build(db, now=NOW)
    capture = ri.diagnosis_capture(db, "一线", now=NOW)
    assert capture["watermark"]["generation_id"] == generation.id
    assert len(capture["rows"]) == 6

    review = Review(
        title="冻结窗口诊断", line="一线", status="completed", source="indexed",
        snapshot=capture["rows"], created_by="analyst@demo.local",
        index_generation_id=generation.id,
        index_watermark=capture["watermark"]["applied_through_id"],
        index_entry_count=6, window_hours=24,
        window_from=capture["window_from"], window_to=capture["window_to"],
        legacy_count=capture["legacy_count"], result=diagnose(capture["rows"]),
    )
    db.add(review)
    db.commit()
    frozen = diagnose(review.snapshot)

    # 30 hours later the window expired and a rebuild happened.
    ri.sync(db, now=NOW + timedelta(hours=30))
    ri.build(db, now=NOW + timedelta(hours=30))
    db.refresh(review)
    assert review.index_watermark == capture["watermark"]["applied_through_id"]
    assert len(review.snapshot) == 6
    assert review.result == frozen
    assert review.result["n"] == 6


def test_rebuild_retires_previous_generation(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    first = ri.build(db, now=NOW)
    second = ri.build(db, now=NOW + timedelta(minutes=5))
    db.refresh(first)
    assert second.status == "active"
    assert first.status == "retired"
    box = ri.index_status(db)
    db.commit()
    assert box["active"]["generation_id"] == second.id
    assert box["status"] == "active"


def test_begin_build_rejects_concurrent_builds(db):
    ri.begin_build(db)
    with pytest.raises(ri.IndexBusy):
        ri.begin_build(db)


def test_recover_marks_interrupted_build_failed_and_keeps_active(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    first = ri.build(db, now=NOW)
    generation = ri.begin_build(db)
    generation.started_at = ri.utcnow() - timedelta(hours=2)
    db.commit()

    ri.recover(db)
    db.refresh(generation)
    assert generation.status == "failed"
    status = ri.index_status(db)
    db.commit()
    assert status["active"]["generation_id"] == first.id
    assert status["lagging"] is True


def test_window_report_has_no_raw_inspection_scan(db, add_samples, engine):
    """Window and diagnosis queries must not fetch the raw inspections table."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session as SASession

    add_samples(12, sampled_at=NOW - timedelta(hours=1))
    generation = ri.build(db, now=NOW)

    captured = []
    with engine.connect() as conn:
        def _capture(c, cursor, statement, parameters, context, executemany):
            captured.append(statement)

        event.listen(conn, "before_cursor_execute", _capture)
        with SASession(bind=conn, expire_on_commit=False) as s:
            ri.window_report(s, now=NOW)
            ri.diagnosis_capture(s, "一线", now=NOW)
        event.remove(conn, "before_cursor_execute", _capture)

    assert captured
    assert all("inspections" not in sql.lower() for sql in captured), captured


def test_incremental_scan_is_bounded_by_id_watermark(db, add_samples):
    """New/expired updates must not scan the full history."""
    from sqlalchemy import event

    add_samples(30, sampled_at=NOW - timedelta(hours=1))
    generation = ri.build(db, now=NOW)
    add_samples(5, sampled_at=NOW - timedelta(minutes=30), start=100, code_prefix="Z")

    executed = []
    bind = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        executed.append((statement, parameters))

    event.listen(bind, "before_cursor_execute", _capture)
    ri.sync(db, now=NOW)
    event.remove(bind, "before_cursor_execute", _capture)

    inserts = [p for s, p in executed if "INSERT INTO yield_index_entries" in s]
    assert inserts
    # The incremental read is anchored on the id watermark, not on a full table walk.
    sources = [s for s, _ in executed if "FROM inspections" in s]
    assert sources
    # The only unbounded lookups allowed are the ORDER BY id DESC LIMIT 1 watermark
    # probe and the "id > watermark" pending count; the copy itself is id-bounded.
    copy = [s for s in sources if "INSERT INTO yield_index_entries" in s]
    assert copy
    for s in copy:
        # Either the id-watermark increment or the 24h window reconcile; never a full scan.
        bounded = "inspections.id >" in s or "inspections.sampled_at >=" in s
        assert bounded, s
    for s in sources:
        if s.strip().startswith("SELECT") and "LIMIT" not in s.upper():
            assert "inspections.id >" in s, s
    # Expiry deletes only rows older than the cutoff for this generation.
    deletes = [s for s, _ in executed if s.startswith("DELETE FROM yield_index_entries")]
    assert deletes and all("sampled_at <" in s for s in deletes)


def test_retired_generations_are_pruned_but_latest_kept(db, add_samples):
    add_samples(5, sampled_at=NOW - timedelta(hours=1))
    gens = [ri.build(db, now=NOW + timedelta(minutes=i)) for i in range(4)]
    status = ri.index_status(db)
    db.commit()
    assert status["active"]["generation_id"] == gens[-1].id
    remaining = set(db.scalars(select(YieldIndexEntry.generation_id)))
    assert gens[-1].id in remaining
    assert gens[-2].id in remaining  # 最近一代退休索引保留用于核对
    assert gens[0].id not in remaining and gens[1].id not in remaining
