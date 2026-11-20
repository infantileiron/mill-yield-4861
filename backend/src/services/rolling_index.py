"""滚动出粉率索引（rolling yield index）。

设计要点
--------
* ``yield_index_entries`` 是检验记录的物化副本，每条带实际采样时刻；窗口查询、
  分位数与诊断只读索引，不再回查 ``inspections`` 全表。
* 索引按「代」(generation) 构建：新一代完整构建后才原子切换为 active。构建中、
  维护故障或重建时旧代继续提供上一个完整水位的结果，并在水位里标明滞后原因。
* 增量只处理 ``inspection.id > applied_through_id`` 的新记录；迟到检验单独按
  sampling 时间判断是否进窗口。过期按 ``sampled_at < cutoff`` 精确删除。两者都
  走索引，不扫描全部历史。
* 所有写入使用 ``(generation_id, inspection_id)`` 唯一约束 + ON CONFLICT DO
  NOTHING，重试与并发切换都不会重复计数。
* 构建期间新提交的检验先写进「当前 active 代」；新代切换后做一次窗口内反连接
  补齐（只扫 24 小时窗口），保证并发提交不漏不重。
* 诊断受理时冻结代 id、水位与样本行；之后窗口过期或重建都不改写已受理结果。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, delete, func, literal, or_, select
from sqlalchemy.dialects import postgresql, sqlite

from src.models.entities import (
    Batch,
    Inspection,
    YieldIndexEntry,
    YieldIndexGeneration,
    YieldIndexState,
)

log = logging.getLogger(__name__)

WINDOW_HOURS = int(os.getenv("YIELD_INDEX_WINDOW_HOURS", "24"))
LATE_TOLERANCE_MINUTES = int(os.getenv("YIELD_INDEX_LATE_MINUTES", "30"))
STALE_BUILD_MINUTES = int(os.getenv("YIELD_INDEX_STALE_BUILD_MINUTES", "30"))
# 切换后继续做窗口反连接的时间，覆盖切换瞬间仍在提交的录入事务。
RECONCILE_GRACE_SECONDS = int(os.getenv("YIELD_INDEX_RECONCILE_GRACE_SECONDS", "120"))
# 保留最近一代已退休索引，便于核对；更早的物化行清理掉，避免无限增长。
KEEP_RETIRED_GENERATIONS = int(os.getenv("YIELD_INDEX_KEEP_RETIRED_GENERATIONS", "1"))
STATE_ID = 1
LINES = ("一线", "二线", "三线")


class IndexBusy(RuntimeError):
    def __init__(self) -> None:
        super().__init__("索引正在构建，请等待完成后再次重建")


class IndexNotBuilt(RuntimeError):
    pass


class TooManySamples(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def db_dt(db, value: datetime | None) -> datetime | None:
    """SQLite 不保存时区，统一绑定 UTC naive 值，避免窗口比较错位。"""
    value = as_utc(value)
    if value is None:
        return None
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        return value.replace(tzinfo=None)
    return value


def _iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def _insert(db, model):
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        return postgresql.insert(model)
    if dialect == "sqlite":
        return sqlite.insert(model)
    return model.__table__.insert()


def is_late(sampled_at, recorded_at) -> bool:
    sampled, recorded = as_utc(sampled_at), as_utc(recorded_at)
    if sampled is None or recorded is None:
        return False
    return recorded - sampled > timedelta(minutes=LATE_TOLERANCE_MINUTES)


# ---------------------------------------------------------------------------
# 水位 / 状态
# ---------------------------------------------------------------------------


def ensure_state(db) -> YieldIndexState:
    state = db.get(YieldIndexState, STATE_ID)
    if state is not None:
        return state
    db.execute(_insert(db, YieldIndexState).values(id=STATE_ID, status="empty").on_conflict_do_nothing())
    db.flush()
    state = db.get(YieldIndexState, STATE_ID)
    if state is None:  # pragma: no cover - 极端方言回退
        state = YieldIndexState(id=STATE_ID, status="empty")
        db.add(state)
        db.flush()
    return state


def active_generation(db, state: YieldIndexState | None = None) -> YieldIndexGeneration | None:
    state = state or ensure_state(db)
    return db.get(YieldIndexGeneration, state.active_generation_id) if state.active_generation_id else None


def building_generation(db, state: YieldIndexState | None = None) -> YieldIndexGeneration | None:
    state = state or ensure_state(db)
    return db.get(YieldIndexGeneration, state.building_generation_id) if state.building_generation_id else None


def cutoff_for(generation: YieldIndexGeneration, now: datetime | None = None) -> datetime:
    return as_utc(now or utcnow()) - timedelta(hours=generation.window_hours)


def watermark(generation: YieldIndexGeneration | None) -> dict | None:
    if generation is None:
        return None
    return {
        "generation_id": generation.id,
        "status": generation.status,
        "applied_through_id": generation.applied_through_id,
        "snapshot_max_id": generation.snapshot_max_id,
        "entry_count": generation.entry_count,
        "window_hours": generation.window_hours,
        "started_at": _iso(generation.started_at),
        "built_at": _iso(generation.built_at),
        "activated_at": _iso(generation.activated_at),
    }


def lag_state(db, state: YieldIndexState | None = None) -> dict:
    state = state or ensure_state(db)
    active = active_generation(db, state)
    building = building_generation(db, state)
    status = state.status
    if status == "building" and building is None:
        # 构建已结束但状态行尚未刷新到最新：以是否有可用代为准。
        status = "active" if active is not None else "empty"
    reason = ""
    if building is not None:
        reason = "新一代索引构建中，当前展示上一个完整水位"
    elif status == "lagging":
        reason = state.last_error or "索引维护滞后，当前展示上一个完整水位"
    elif status == "failed":
        reason = state.last_error or "索引构建失败，当前展示上一个完整水位"
    elif status == "empty":
        reason = "索引尚未建立"
    lagging = bool(active is not None and (building is not None or status != "active"))
    if active is None and building is not None:
        reason = "首次建立索引中，完成后可查询"
    return {
        "status": status,
        "built": active is not None,
        "lagging": lagging,
        "serving_generation_id": active.id if active else None,
        "reason": reason,
        "last_error": state.last_error,
        "last_synced_at": _iso(state.last_reconciled_at),
        "pending_inspections": state.pending_count,
    }


def index_status(db) -> dict:
    state = ensure_state(db)
    active, building = active_generation(db, state), building_generation(db, state)
    lag = lag_state(db, state)
    active_watermark = watermark(active)
    if active is not None:
        counts = generation_counts(db, active)
        active_watermark.update(counts)
        active_watermark["entry_count"] = counts["total"]
        lag["entries"] = counts
    return {
        **lag,
        "active": active_watermark,
        "building": watermark(building),
        "window_hours": active.window_hours if active else WINDOW_HOURS,
        "late_tolerance_minutes": LATE_TOLERANCE_MINUTES,
        "lines": list(LINES),
    }


def _max_inspection_id(db) -> int:
    """取最大 id；走主键索引的 ORDER BY ... LIMIT 1，避免聚合扫描。"""
    return db.scalar(select(Inspection.id).order_by(Inspection.id.desc()).limit(1)) or 0


def _pending_count(db, applied_through_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(Inspection).where(Inspection.id > applied_through_id)
    ) or 0


# ---------------------------------------------------------------------------
# 构建 / 切换 / 恢复
# ---------------------------------------------------------------------------


def begin_build(db, window_hours: int = WINDOW_HOURS) -> YieldIndexGeneration:
    """登记新一代构建。只锁控制行，不阻塞检验录入。"""
    now = utcnow()
    ensure_state(db)
    state = db.execute(
        select(YieldIndexState).where(YieldIndexState.id == STATE_ID).with_for_update()
    ).scalar_one()
    stale = building_generation(db, state)
    if stale is not None:
        started = as_utc(stale.started_at)
        if started and now - started < timedelta(minutes=STALE_BUILD_MINUTES):
            raise IndexBusy()
        stale.status = "failed"
        stale.error = stale.error or "构建超时"
        state.building_generation_id = None
    snapshot_max = _max_inspection_id(db)
    generation = YieldIndexGeneration(
        status="building", window_hours=int(window_hours), snapshot_max_id=snapshot_max, started_at=now
    )
    db.add(generation)
    db.flush()
    state.building_generation_id = generation.id
    state.status = "building"
    state.last_error = ""
    state.updated_at = now
    db.commit()
    return generation


def _source_select(db, generation_id: int, cutoff: datetime, now: datetime, after_id: int | None = None):
    """从 inspections 取「可进窗口」的行：采样时刻不早于 cutoff，或无采样时刻。

    刻意不设时间上界：id 水位一旦越过某条记录就不会再回头，任何上界过滤都可能
    让刚提交的记录被永久跳过。窗口展示另行用 ``sampled_at <= now`` 过滤。
    """
    cols = (
        literal(generation_id),
        Inspection.id,
        Batch.line,
        Inspection.sampled_at,
        Inspection.flour_yield,
        Inspection.moisture,
        Inspection.sample_code,
        Batch.code,
        Inspection.inspector,
        Inspection.recorded_at,
    )
    stmt = (
        select(*cols)
        .join(Batch, Batch.id == Inspection.batch_id)
        .where(
            or_(
                Inspection.sampled_at.is_(None),
                Inspection.sampled_at >= db_dt(db, cutoff),
            )
        )
    )
    if after_id is not None:
        stmt = stmt.where(Inspection.id > after_id)
    return stmt


_COPY_COLUMNS = [
    "generation_id",
    "inspection_id",
    "line",
    "sampled_at",
    "flour_yield",
    "moisture",
    "sample_code",
    "batch_code",
    "inspector",
    "recorded_at",
]


def _copy(db, generation_id: int, src) -> int:
    stmt = (
        _insert(db, YieldIndexEntry)
        .from_select(_COPY_COLUMNS, src)
        .on_conflict_do_nothing(index_elements=["generation_id", "inspection_id"])
    )
    inserted = db.execute(stmt).rowcount or 0
    db.flush()
    return inserted


def copy_snapshot(db, generation: YieldIndexGeneration, now: datetime | None = None) -> int:
    """复制快照点之前的窗口记录；幂等。"""
    now = as_utc(now or utcnow())
    src = _source_select(db, generation.id, cutoff_for(generation, now), now).where(
        Inspection.id <= generation.snapshot_max_id
    )
    inserted = _copy(db, generation.id, src)
    return inserted


def late_filter(db):
    """迟到条件：录入时刻比采样时刻晚超过容差。"""
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        return func.julianday(YieldIndexEntry.recorded_at) - func.julianday(
            YieldIndexEntry.sampled_at
        ) > (LATE_TOLERANCE_MINUTES / 1440.0)
    return (
        YieldIndexEntry.recorded_at - YieldIndexEntry.sampled_at
        > timedelta(minutes=LATE_TOLERANCE_MINUTES)
    )


def reconcile_window(db, generation: YieldIndexGeneration, now: datetime | None = None) -> int:
    """窗口内反连接补齐：只扫最近 24 小时的采样时间索引。

    用于构建并发空档与故障恢复追平；唯一约束保证重试不重复计数。
    """
    now = as_utc(now or utcnow())
    exists = (
        select(literal(1))
        .select_from(YieldIndexEntry)
        .where(
            YieldIndexEntry.generation_id == generation.id,
            YieldIndexEntry.inspection_id == Inspection.id,
        )
        .exists()
    )
    src = _source_select(db, generation.id, cutoff_for(generation, now), now).where(~exists)
    inserted = _copy(db, generation.id, src)
    return inserted


def run_build(db, generation_id: int, now: datetime | None = None) -> YieldIndexGeneration:
    """执行构建并原子切换。任何异常都保留旧 active 代并标记滞后。"""
    now = as_utc(now or utcnow())
    generation = db.get(YieldIndexGeneration, generation_id)
    if generation is None or generation.status != "building":
        raise IndexBusy()
    try:
        copy_snapshot(db, generation, now)
        reconcile_window(db, generation, now)
        generation.applied_through_id = _max_inspection_id(db)
        generation.entry_count = _count_entries(db, generation.id)
        generation.copy_done = True
        generation.built_at = utcnow()
        db.commit()

        state = db.execute(
            select(YieldIndexState).where(YieldIndexState.id == STATE_ID).with_for_update()
        ).scalar_one()
        previous = active_generation(db, state)
        generation = db.get(YieldIndexGeneration, generation_id)
        generation.status = "active"
        generation.activated_at = utcnow()
        generation.needs_reconcile = True
        state.active_generation_id = generation.id
        state.building_generation_id = None
        state.status = "active"
        state.last_error = ""
        state.last_reconciled_at = utcnow()
        state.pending_count = 0
        state.updated_at = utcnow()
        if previous is not None and previous.id != generation.id:
            previous.status = "retired"
            previous.retired_at = utcnow()
        db.commit()

        # 切换瞬间可能新增落进旧代：新代做一次窗口补齐，幂等。
        # 真实运行时 now 就是当前时刻；测试注入固定时刻时保持确定性。
        sync(db, now=now if now else utcnow(), reconcile=True)
        purge_retired(db)
        return db.get(YieldIndexGeneration, generation_id)
    except Exception as exc:  # noqa: BLE001 - 必须落库并保留旧代
        db.rollback()
        mark_failed(db, generation_id, exc)
        raise


def purge_retired(db, keep: int = KEEP_RETIRED_GENERATIONS) -> int:
    """删除较早退休代的物化行；保留最近 keep 代用于核对。诊断已冻结快照，不受影响。"""
    ids = db.scalars(
        select(YieldIndexGeneration.id)
        .where(YieldIndexGeneration.status == "retired")
        .order_by(YieldIndexGeneration.id.desc())
        .offset(keep)
    ).all()
    if not ids:
        return 0
    deleted = db.execute(
        delete(YieldIndexEntry).where(YieldIndexEntry.generation_id.in_(ids))
    ).rowcount or 0
    db.commit()
    return deleted


def mark_failed(db, generation_id: int | None, exc: Exception | str) -> None:
    generation = db.get(YieldIndexGeneration, generation_id) if generation_id else None
    if generation is not None and generation.status == "building":
        generation.status = "failed"
        generation.error = str(exc)[:2000]
    state = ensure_state(db)
    if state.building_generation_id == generation_id:
        state.building_generation_id = None
    # 已切换成功的代继续服务，只标记滞后；补齐由后续追平重试。
    state.status = "lagging" if state.active_generation_id else "failed"
    state.last_error = str(exc)[:2000]
    state.updated_at = utcnow()
    db.commit()


def sync_failed(db, exc: Exception | str) -> None:
    """追平失败：保留 active 水位与进行中的构建，仅标记滞后。"""
    state = ensure_state(db)
    state.status = "lagging" if state.active_generation_id else state.status
    state.last_error = str(exc)[:2000]
    state.updated_at = utcnow()
    db.commit()


def build(db, window_hours: int = WINDOW_HOURS, now: datetime | None = None) -> YieldIndexGeneration:
    generation = begin_build(db, window_hours)
    return run_build(db, generation.id, now)


def run_build_in_session(generation_id: int) -> None:
    """后台任务入口（独立 Session）。"""
    from src.config.database import Session

    with Session() as db:
        try:
            run_build(db, generation_id)
        except Exception:  # noqa: BLE001
            log.exception("rolling yield index build failed")


def recover(db) -> None:
    """进程启动：清理上次中断的构建；active 代继续服务。"""
    state = ensure_state(db)
    generation = building_generation(db, state)
    if generation is not None:
        started = as_utc(generation.started_at)
        if started and utcnow() - started > timedelta(minutes=STALE_BUILD_MINUTES):
            generation.status = "failed"
            generation.error = generation.error or "进程重启，构建中断"
            state.building_generation_id = None
            state.status = "lagging" if state.active_generation_id else "failed"
            state.last_error = generation.error
            db.commit()
    if active_generation(db, state) is None and state.status == "active":
        state.status = "empty"
        db.commit()


# ---------------------------------------------------------------------------
# 增量写入（提交事务内）
# ---------------------------------------------------------------------------


def apply_inspection(db, inspection: Inspection, batch: Batch, now: datetime | None = None) -> str:
    """检验提交时同步写 active 索引；迟到但在窗口内同样写入。

    返回 inserted / duplicate / out-of-window / no-index，供接口说明去向。
    """
    state = ensure_state(db)
    generation = active_generation(db, state)
    if generation is None:
        return "no-index"
    now = as_utc(now or utcnow())
    sampled = as_utc(inspection.sampled_at)
    if sampled is not None:
        cutoff = cutoff_for(generation, now)
        if sampled < cutoff or sampled > now + timedelta(minutes=5):
            return "out-of-window"
    values = dict(
        generation_id=generation.id,
        inspection_id=inspection.id,
        line=batch.line,
        sampled_at=db_dt(db, sampled),
        flour_yield=float(inspection.flour_yield),
        moisture=float(inspection.moisture),
        sample_code=inspection.sample_code,
        batch_code=batch.code,
        inspector=inspection.inspector,
        recorded_at=db_dt(db, inspection.recorded_at),
    )
    stmt = _insert(db, YieldIndexEntry).values(**values).on_conflict_do_nothing(
        index_elements=["generation_id", "inspection_id"]
    )
    inserted = db.execute(stmt).rowcount or 0
    if inserted:
        generation.entry_count = (generation.entry_count or 0) + 1
    state.updated_at = now
    return "inserted" if inserted else "duplicate"


def sync(db, now: datetime | None = None, reconcile: bool = False) -> dict:
    """增量追平 + 精确过期；只按 id 水位与采样时间索引，不扫全历史。"""
    now = as_utc(now or utcnow())
    ensure_state(db)
    # 锁住控制行再读水位：避免与构建切换的提交交错，把状态写回过期值。
    state = db.execute(
        select(YieldIndexState).where(YieldIndexState.id == STATE_ID).with_for_update()
    ).scalar_one()
    generation = active_generation(db, state)
    if generation is None:
        building = building_generation(db, state)
        state.status = "building" if building else "empty"
        state.updated_at = now
        db.commit()
        return {"inserted": 0, "expired": 0, "watermark": None, "lagging": True, "built": False}

    cutoff = cutoff_for(generation, now)
    watermark_id = generation.applied_through_id or 0
    inserted = 0
    src = _source_select(db, generation.id, cutoff, now, after_id=watermark_id)
    stmt = _insert(db, YieldIndexEntry).from_select(_COPY_COLUMNS, src).on_conflict_do_nothing(
        index_elements=["generation_id", "inspection_id"]
    )
    inserted += db.execute(stmt).rowcount or 0
    activated = as_utc(generation.activated_at)
    need = (
        reconcile
        or bool(generation.needs_reconcile)
        or (activated is not None and now - activated < timedelta(seconds=RECONCILE_GRACE_SECONDS))
    )
    if need:
        # 窗口反连接补齐并发空档；成功后再清标记，失败时下轮重试仍会补齐。
        inserted += reconcile_window(db, generation, now)
        generation.needs_reconcile = False

    expired = db.execute(
        delete(YieldIndexEntry).where(
            YieldIndexEntry.generation_id == generation.id,
            YieldIndexEntry.sampled_at.is_not(None),
            YieldIndexEntry.sampled_at < db_dt(db, cutoff),
        )
    ).rowcount or 0

    new_watermark = _max_inspection_id(db) or watermark_id
    generation.applied_through_id = max(watermark_id, new_watermark)
    # 每轮追平都以索引精确计数校准，多个写入者并发时计数也不会漂移。
    generation.entry_count = _count_entries(db, generation.id)
    state.status = "building" if state.building_generation_id else "active"
    state.last_error = ""
    state.pending_count = _pending_count(db, generation.applied_through_id)
    state.last_reconciled_at = now
    state.updated_at = now
    db.commit()
    return {
        "inserted": inserted,
        "expired": expired,
        "watermark": generation.applied_through_id,
        "entry_count": generation.entry_count,
        "lagging": False,
        "built": True,
    }


def generation_counts(db, generation, cutoff: datetime | None = None,
                      now: datetime | None = None) -> dict:
    """按索引精确统计窗口条数与旧记录条数；不读原始检验表。"""
    now = as_utc(now or utcnow())
    cutoff = as_utc(cutoff or cutoff_for(generation, now))
    base = [YieldIndexEntry.generation_id == generation.id]
    window = db.scalar(
        select(func.count()).select_from(YieldIndexEntry).where(
            *base,
            YieldIndexEntry.sampled_at.is_not(None),
            YieldIndexEntry.sampled_at >= db_dt(db, cutoff),
            YieldIndexEntry.sampled_at <= db_dt(db, now),
        )
    ) or 0
    legacy = db.scalar(
        select(func.count()).select_from(YieldIndexEntry).where(
            *base, YieldIndexEntry.sampled_at.is_(None)
        )
    ) or 0
    return {"window_count": window, "legacy_count": legacy, "total": window + legacy}


def _count_entries(db, generation_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(YieldIndexEntry).where(
            YieldIndexEntry.generation_id == generation_id
        )
    ) or 0


# ---------------------------------------------------------------------------
# 查询：只读索引
# ---------------------------------------------------------------------------


def _window_filters(db, generation_id: int, line: str | None, cutoff: datetime, now: datetime):
    filters = [
        YieldIndexEntry.generation_id == generation_id,
        YieldIndexEntry.sampled_at.is_not(None),
        YieldIndexEntry.sampled_at >= db_dt(db, cutoff),
        YieldIndexEntry.sampled_at <= db_dt(db, now),
    ]
    if line:
        filters.append(YieldIndexEntry.line == line)
    return filters


def _row_dict(row) -> dict:
    return {
        "inspection_id": row.inspection_id,
        "sample_code": row.sample_code,
        "batch_code": row.batch_code,
        "line": row.line,
        "flour_yield": float(row.flour_yield),
        "moisture": float(row.moisture),
        "inspector": row.inspector,
        "sampled_at": _iso(row.sampled_at),
        "recorded_at": _iso(row.recorded_at),
        "late": is_late(row.sampled_at, row.recorded_at),
    }


def collect_rows(db, generation, line: str | None = None, cutoff: datetime | None = None,
                 now: datetime | None = None, limit: int | None = None) -> list[dict]:
    """窗口内全部样本（诊断冻结快照用）；仅读索引。"""
    now = as_utc(now or utcnow())
    cutoff = as_utc(cutoff or cutoff_for(generation, now))
    stmt = (
        select(YieldIndexEntry)
        .where(*_window_filters(db, generation.id, line, cutoff, now))
        .order_by(YieldIndexEntry.sampled_at, YieldIndexEntry.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    return [_row_dict(row) for row in db.scalars(stmt)]


def legacy_summary(db, generation, line: str | None = None, limit: int = 100) -> dict:
    filters = [YieldIndexEntry.generation_id == generation.id, YieldIndexEntry.sampled_at.is_(None)]
    if line:
        filters.append(YieldIndexEntry.line == line)
    total = db.scalar(select(func.count()).select_from(YieldIndexEntry).where(*filters)) or 0
    by_line = {
        name: n
        for name, n in db.execute(
            select(YieldIndexEntry.line, func.count())
            .where(*filters)
            .group_by(YieldIndexEntry.line)
            .order_by(YieldIndexEntry.line)
        )
    }
    samples = [
        _row_dict(row)
        for row in db.scalars(
            select(YieldIndexEntry).where(*filters).order_by(YieldIndexEntry.id).limit(limit)
        )
    ]
    return {"count": total, "by_line": by_line, "samples": samples, "truncated": total > len(samples)}


def _summary(db, generation_id: int, line: str | None, cutoff: datetime, now: datetime) -> dict:
    filters = _window_filters(db, generation_id, line, cutoff, now)
    n = db.scalar(select(func.count()).select_from(YieldIndexEntry).where(*filters)) or 0
    if n == 0:
        return {"n": 0}
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        mean, std, low, high, q1, median, q3 = db.execute(
            select(
                func.avg(YieldIndexEntry.flour_yield),
                func.stddev_samp(YieldIndexEntry.flour_yield),
                func.min(YieldIndexEntry.flour_yield),
                func.max(YieldIndexEntry.flour_yield),
                func.percentile_cont(0.25).within_group(YieldIndexEntry.flour_yield),
                func.percentile_cont(0.5).within_group(YieldIndexEntry.flour_yield),
                func.percentile_cont(0.75).within_group(YieldIndexEntry.flour_yield),
            ).where(*filters)
        ).one()
        values = dict(
            n=n,
            mean=round(float(mean), 4),
            std=round(float(std), 4) if std is not None else 0.0,
            min=float(low),
            max=float(high),
            q1=float(q1),
            median=float(median),
            q3=float(q3),
        )
    else:  # SQLite 等方言的精确回退：只读窗口内数值，与线性插值口径一致
        import statistics as pystats

        from src.services.statistics import quantile

        vals = sorted(float(v) for v in db.scalars(select(YieldIndexEntry.flour_yield).where(*filters)))
        values = dict(
            n=n,
            mean=round(pystats.mean(vals), 4),
            std=round(pystats.stdev(vals), 4) if n >= 2 else 0.0,
            min=vals[0],
            max=vals[-1],
            q1=quantile(vals, 0.25),
            median=quantile(vals, 0.5),
            q3=quantile(vals, 0.75),
        )
    values["iqr"] = values["q3"] - values["q1"]
    values["lower"] = values["q1"] - 1.5 * values["iqr"]
    values["upper"] = values["q3"] + 1.5 * values["iqr"]
    return values


def _histogram(db, generation_id: int, line: str | None, cutoff: datetime, now: datetime) -> list[dict]:
    edges = list(range(0, 101, 5))
    filters = _window_filters(db, generation_id, line, cutoff, now)
    cols = [
        func.sum(
            case(
                (and_(YieldIndexEntry.flour_yield >= a, YieldIndexEntry.flour_yield < (b if b < 100 else 101)), 1),
                else_=0,
            )
        )
        for a, b in zip(edges, edges[1:])
    ]
    counts = db.execute(select(*cols).where(*filters)).one()
    return [
        {"from": a, "to": b, "count": int(c or 0)} for a, b, c in zip(edges, edges[1:], counts)
    ]


def _outliers(db, generation_id: int, line: str | None, cutoff: datetime, now: datetime,
              bounds: dict, limit: int = 500) -> dict:
    filters = _window_filters(db, generation_id, line, cutoff, now)
    outside = or_(
        YieldIndexEntry.flour_yield < bounds["lower"],
        YieldIndexEntry.flour_yield > bounds["upper"],
    )
    total = db.scalar(select(func.count()).select_from(YieldIndexEntry).where(*filters, outside)) or 0
    rows = db.scalars(
        select(YieldIndexEntry)
        .where(*filters, outside)
        .order_by(YieldIndexEntry.sampled_at, YieldIndexEntry.id)
        .limit(limit)
    ).all()
    return {"total": total, "truncated": total > len(rows), "samples": [_row_dict(r) for r in rows]}


def line_summary(db, generation, line: str, cutoff: datetime | None = None,
                 now: datetime | None = None) -> dict:
    now = as_utc(now or utcnow())
    cutoff = as_utc(cutoff or cutoff_for(generation, now))
    summary = _summary(db, generation.id, line, cutoff, now)
    if summary.get("n", 0) == 0:
        summary.update(histogram=[], outliers={"total": 0, "truncated": False, "samples": []},
                       late_count=0, in_window=0)
        return summary
    summary["outliers"] = _outliers(db, generation.id, line, cutoff, now, summary)
    summary["histogram"] = _histogram(db, generation.id, line, cutoff, now)
    summary["late_count"] = db.scalar(
        select(func.count())
        .select_from(YieldIndexEntry)
        .where(
            *_window_filters(db, generation.id, line, cutoff, now),
            YieldIndexEntry.recorded_at.is_not(None),
            late_filter(db),
        )
    ) or 0
    summary["in_window"] = summary["n"]
    return summary


def window_report(db, line: str | None = None, hours: int | None = None,
                  now: datetime | None = None) -> dict:
    """各产线最近 N 小时的分位数与异常样本；不返回原始检验行。"""
    state = ensure_state(db)
    generation = active_generation(db, state)
    lag = lag_state(db, state)
    if generation is None:
        return {
            "built": False, "lagging": lag["lagging"], "reason": lag["reason"],
            "status": state.status, "watermark": None, "lines": {},
            "legacy": {"count": 0, "by_line": {}, "samples": [], "truncated": False},
        }
    now = as_utc(now or utcnow())
    window_hours = int(hours or generation.window_hours)
    cutoff = now - timedelta(hours=window_hours)
    names = [line] if line else list(LINES)
    wm = watermark(generation)
    wm.update(generation_counts(db, generation, cutoff=cutoff, now=now))
    wm["entry_count"] = wm["total"]
    return {
        "built": True,
        "status": state.status,
        "lagging": lag["lagging"],
        "reason": lag["reason"],
        "watermark": wm,
        "window": {
            "hours": window_hours,
            "from": _iso(cutoff),
            "to": _iso(now),
            "late_tolerance_minutes": LATE_TOLERANCE_MINUTES,
        },
        "lines": {name: line_summary(db, generation, name, cutoff=cutoff, now=now) for name in names},
        "legacy": legacy_summary(db, generation, line),
        "as_of": _iso(now),
    }


# ---------------------------------------------------------------------------
# 诊断受理
# ---------------------------------------------------------------------------


def diagnosis_capture(db, line: str | None, hours: int | None = None,
                      now: datetime | None = None) -> dict:
    """受理诊断：冻结 active 代、水位和窗口样本；之后过期不改写结果。"""
    state = ensure_state(db)
    generation = active_generation(db, state)
    if generation is None:
        raise IndexNotBuilt("滚动索引尚未建立，请先建立索引再受理诊断")
    now = as_utc(now or utcnow())
    window_hours = int(hours or generation.window_hours)
    cutoff = now - timedelta(hours=window_hours)
    lag = lag_state(db, state)
    # 冻结快照上限：单产线单窗口 5000 条，超过则拒绝受理而不是悄悄截断。
    max_rows = int(os.getenv("YIELD_INDEX_MAX_SNAPSHOT", "5000"))
    rows = collect_rows(db, generation, line=line, cutoff=cutoff, now=now)
    if len(rows) > max_rows:
        raise TooManySamples(
            f"窗口内样本 {len(rows)} 条超过单次受理上限 {max_rows} 条，请缩短窗口或按产线拆分"
        )
    return {
        "generation": generation,
        "rows": rows,
        "legacy_count": legacy_summary(db, generation, line)["count"],
        "lagging": lag["lagging"],
        "reason": lag["reason"],
        "watermark": watermark(generation),
        "window_from": cutoff,
        "window_to": now,
        "window_hours": window_hours,
    }
