"""并发场景：建索引与录入交错、切换后不漏不重、恢复追平不重复计数。

SQLite 单写者限制使真实线程难以复现 PostgreSQL 的并发；这里用显式事务边界模拟
「提交落在快照之后、切换之前」的时序，并对每次补写重复执行以证明幂等。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.models.entities import Batch, Inspection, YieldIndexEntry
from src.services import rolling_index as ri

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _inspection(db, batch_id, code, sampled, recorded=None):
    obj = Inspection(sample_code=code, batch_id=batch_id, flour_yield=73.0, moisture=12.0,
                     inspector="林洁", sampled_at=sampled, recorded_at=recorded or sampled)
    db.add(obj)
    db.commit()
    return obj


def test_commit_between_snapshot_and_switch_is_caught_by_reconcile(db, batch):
    for i in range(5):
        _inspection(db, batch.id, f"PRE-{i}", NOW - timedelta(hours=2))
    generation = ri.begin_build(db, window_hours=24)
    snapshot_max = generation.snapshot_max_id
    # Commit after the snapshot point but before activation (no direct index write).
    _inspection(db, batch.id, "IN-FLIGHT", NOW - timedelta(hours=1))
    assert db.scalar(select(func.max(Inspection.id))) > snapshot_max

    ri.run_build(db, generation.id, now=NOW)

    codes = set(db.scalars(select(YieldIndexEntry.sample_code)
                           .where(YieldIndexEntry.generation_id == generation.id)))
    assert "IN-FLIGHT" in codes
    assert len(codes) == 6
    db.refresh(generation)
    assert generation.applied_through_id >= db.scalar(select(func.max(Inspection.id)))


def test_reconcile_is_idempotent_across_retries(db, batch):
    for i in range(5):
        _inspection(db, batch.id, f"A-{i}", NOW - timedelta(hours=2))
    generation = ri.build(db, now=NOW)
    _inspection(db, batch.id, "GAP-1", NOW - timedelta(minutes=30))

    # Simulate a crash before the incremental copy, then repeated recovery attempts.
    for _ in range(3):
        ri.reconcile_window(db, generation, now=NOW)
        db.commit()

    rows = db.scalars(select(YieldIndexEntry).where(
        YieldIndexEntry.generation_id == generation.id)).all()
    assert len(rows) == len({r.inspection_id for r in rows}) == 6


def test_retried_build_after_failure_does_not_double_count(db, batch, monkeypatch):
    for i in range(5):
        _inspection(db, batch.id, f"B-{i}", NOW - timedelta(hours=2))
    first = ri.begin_build(db)
    original = ri.reconcile_window
    calls = {"n": 0}

    def flaky(db_, generation, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("切换前故障")
        return original(db_, generation, now)

    monkeypatch.setattr(ri, "reconcile_window", flaky)
    try:
        ri.run_build(db, first.id, now=NOW)
    except RuntimeError:
        pass
    # No active generation exists, so the failed build is reported, not served.
    status = ri.index_status(db)
    db.commit()
    assert status["built"] is False

    monkeypatch.setattr(ri, "reconcile_window", original)
    second = ri.build(db, now=NOW)
    rows = db.scalars(select(YieldIndexEntry).where(
        YieldIndexEntry.generation_id == second.id)).all()
    assert len(rows) == len({r.inspection_id for r in rows}) == 5
    db.refresh(second)
    assert second.entry_count == 5


def test_new_commit_during_build_lands_in_old_then_switches(db, batch):
    """旧代继续服务期间提交的检验不会丢；切换后的新代通过补齐得到全集。"""
    for i in range(5):
        _inspection(db, batch.id, f"C-{i}", NOW - timedelta(hours=2))
    old = ri.build(db, now=NOW)

    building = ri.begin_build(db)
    # During the build, an API write goes to the still-active old generation.
    new_row = _inspection(db, batch.id, "DURING-BUILD", NOW - timedelta(minutes=20))
    ri.apply_inspection(db, new_row, batch, now=NOW)
    db.commit()
    assert db.scalar(select(func.count()).select_from(YieldIndexEntry)
                     .where(YieldIndexEntry.generation_id == old.id)) == 6

    ri.run_build(db, building.id, now=NOW)
    new_gen_rows = db.scalars(select(YieldIndexEntry).where(
        YieldIndexEntry.generation_id == building.id)).all()
    assert len(new_gen_rows) == len({r.inspection_id for r in new_gen_rows}) == 6
    assert {r.sample_code for r in new_gen_rows} == {f"C-{i}" for i in range(5)} | {"DURING-BUILD"}


def test_duplicate_apply_inspection_is_ignored(db, batch):
    for i in range(5):
        _inspection(db, batch.id, f"E-{i}", NOW - timedelta(hours=2))
    generation = ri.build(db, now=NOW)
    row = _inspection(db, batch.id, "E-DUP", NOW - timedelta(minutes=5))
    assert ri.apply_inspection(db, row, batch, now=NOW) == "inserted"
    assert ri.apply_inspection(db, row, batch, now=NOW) == "duplicate"
    db.commit()
    assert db.scalar(select(func.count()).select_from(YieldIndexEntry).where(
        YieldIndexEntry.generation_id == generation.id,
        YieldIndexEntry.inspection_id == row.id)) == 1
