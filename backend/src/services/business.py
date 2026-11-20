import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from src.config.settings import REDIS_URL
from src.models.entities import Batch, Inspection, Review
from src.services import rolling_index
from src.utils.serialize import record

queue = Redis.from_url(REDIS_URL, decode_responses=True)
log = logging.getLogger(__name__)


def batches(db):
    rows = db.execute(
        select(Batch, func.count(Inspection.id))
        .outerjoin(Inspection, Inspection.batch_id == Batch.id)
        .group_by(Batch.id)
        .order_by(Batch.id.desc())
    ).all()
    return [{**record(b), "samples": n} for b, n in rows]


def create(db, model, data):
    obj = model(**data)
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "编号已存在，请使用独立编号")
    return record(obj)


def inspections(db, batch_id=None):
    q = (
        select(Inspection, Batch.code, Batch.line)
        .join(Batch, Batch.id == Inspection.batch_id)
        .order_by(Inspection.id.desc())
    )
    if batch_id:
        q = q.where(Inspection.batch_id == batch_id)
    return [{**record(i), "batch_code": code, "line": line} for i, code, line in db.execute(q)]


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def add_inspection(db, data):
    batch = db.get(Batch, data["batch_id"])
    if not batch:
        raise HTTPException(404, "原粮批次不存在")
    now = datetime.now(timezone.utc)
    sampled = _as_utc(data.get("sampled_at"))
    if sampled is not None and sampled > now + timedelta(minutes=5):
        raise HTTPException(422, "采样时刻不能晚于当前时间，请核对后重录")
    obj = Inspection(
        sample_code=data["sample_code"],
        batch_id=data["batch_id"],
        flour_yield=data["flour_yield"],
        moisture=data["moisture"],
        inspector=data["inspector"],
        sampled_at=sampled,
        recorded_at=now,
    )
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "编号已存在，请使用独立编号")
    # 索引写入放在保存点里：索引故障不能阻断检验留痕；水位追平会稍后补齐。
    outcome = "no-index"
    index_error = None
    try:
        with db.begin_nested():
            outcome = rolling_index.apply_inspection(db, obj, batch, now)
    except Exception as exc:  # noqa: BLE001
        index_error = exc
        outcome = "deferred"
        log.exception("inline index write failed; watermark sync will catch up")
    db.commit()
    if index_error is not None:
        try:
            rolling_index.sync_failed(db, f"录入时索引写入失败：{index_error}")
        except Exception:  # noqa: BLE001 - 仅状态标记，失败不影响录入
            db.rollback()
    return {**record(obj), "indexed": outcome, "late": rolling_index.is_late(sampled, now)}


def _enqueue(obj):
    try:
        queue.rpush("mill:jobs", obj.id)
        return True
    except Exception:
        obj.status = "failed"
        obj.note = "队列不可用，请恢复后重新提交"
        return False


def submit(db, data, user):
    """原有诊断：冻结全部历史检验样本，保留不变。"""
    rows = inspections(db)
    if data["line"] != "全部":
        rows = [x for x in rows if x["line"] == data["line"]]
    if len(rows) < 5:
        raise HTTPException(422, "该范围至少需要 5 条检验记录，请先录入")
    obj = Review(**data, created_by=user.email, snapshot=rows, source="legacy")
    db.add(obj)
    db.commit()
    if not _enqueue(obj):
        db.commit()
        raise HTTPException(503, obj.note)
    return record(obj)


def submit_indexed(db, data, user):
    """索引诊断：受理时冻结索引代、水位和窗口样本；之后过期不改写结果。"""
    try:
        capture = rolling_index.diagnosis_capture(
            db, None if data["line"] == "全部" else data["line"], data.get("window_hours")
        )
    except rolling_index.IndexNotBuilt as exc:
        raise HTTPException(409, str(exc))
    except rolling_index.TooManySamples as exc:
        raise HTTPException(422, str(exc))
    rows = capture["rows"]
    if len(rows) < 5:
        raise HTTPException(422, "该产线最近窗口内不足 5 条带采样时刻的检验，请先录入")
    obj = Review(
        title=data["title"],
        line=data["line"],
        status="queued",
        source="indexed",
        snapshot=rows,
        created_by=user.email,
        index_generation_id=capture["generation"].id,
        index_watermark=capture["watermark"]["applied_through_id"],
        index_entry_count=len(rows),
        index_lagging=capture["lagging"],
        window_hours=capture["window_hours"],
        window_from=capture["window_from"],
        window_to=capture["window_to"],
        legacy_count=capture["legacy_count"],
        result={},
        note="",
    )
    db.add(obj)
    db.commit()
    if not _enqueue(obj):
        db.commit()
        raise HTTPException(503, obj.note)
    # 冻结样本可能很多，提交响应不回传整份快照；查看时再按 id 读取。
    return {k: v for k, v in record(obj).items() if k != "snapshot"}


def review(db, id):
    obj = db.get(Review, id)
    if not obj:
        raise HTTPException(404, "复核单不存在")
    return obj


def approve(db, id, note, user):
    obj = review(db, id)
    if obj.status != "completed":
        raise HTTPException(409, "只有已计算且未签核的复核单可以签核")
    obj.status = "approved"
    obj.note = note
    obj.approved_by = user.email
    db.commit()
    return record(obj)


# ---------------------------------------------------------------------------
# 索引运维
# ---------------------------------------------------------------------------


def build_index(db, window_hours=rolling_index.WINDOW_HOURS):
    try:
        generation = rolling_index.begin_build(db, window_hours)
    except rolling_index.IndexBusy as exc:
        raise HTTPException(409, str(exc))
    try:
        queue.rpush("mill:index-jobs", generation.id)
    except Exception:
        rolling_index.mark_failed(db, generation.id, "队列不可用，索引构建未启动")
        raise HTTPException(503, "队列不可用，请恢复后重试")
    return rolling_index.watermark(generation)


def sync_index(db):
    try:
        return rolling_index.sync(db, reconcile=True)
    except Exception as exc:  # noqa: BLE001 - 保留旧水位并标记滞后
        db.rollback()
        rolling_index.sync_failed(db, exc)
        raise HTTPException(503, "索引追平失败，已保留上个完整水位")


def index_status(db):
    status = rolling_index.index_status(db)
    db.commit()
    return status


def index_window(db, line=None, hours=None):
    if hours is not None:
        state = rolling_index.ensure_state(db)
        generation = rolling_index.active_generation(db, state)
        if generation is not None and hours > generation.window_hours:
            raise HTTPException(422, f"索引只保留最近 {generation.window_hours} 小时，无法查询更长窗口")
        if hours < 1:
            raise HTTPException(422, "窗口小时必须为正整数")
    report = rolling_index.window_report(db, line=line, hours=hours)
    db.commit()
    return report
