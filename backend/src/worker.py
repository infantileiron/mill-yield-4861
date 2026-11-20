import json
import logging
import os
import time
import traceback
from pathlib import Path

from sqlalchemy import select

from src.config.database import Session, engine
from src.config.migrations import ensure_schema
from src.models.entities import Review
from src.services import rolling_index
from src.services.business import queue
from src.services.statistics import diagnose

ensure_schema(engine)
log = logging.getLogger(__name__)

SYNC_INTERVAL = float(os.getenv("YIELD_INDEX_SYNC_SECONDS", "15"))
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "/artifacts"))
BUILD_QUEUE = "mill:index-jobs"
JOB_QUEUE = "mill:jobs"


def recover_on_start() -> None:
    """重启恢复：中断的索引构建标记失败（旧水位继续服务），未完成复核重新排队。"""
    with Session() as db:
        rolling_index.recover(db)
        for obj in db.scalars(select(Review).where(Review.status.in_(['queued', 'running']))):
            queue.rpush(JOB_QUEUE, obj.id)


def run_review(review_id: int) -> None:
    with Session() as db:
        obj = db.get(Review, review_id)
        if not obj or obj.status not in ('queued', 'running'):
            return
        obj.status = 'running'
        db.commit()
        try:
            obj.result = diagnose(obj.snapshot)
            obj.status = 'completed'
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            (ARTIFACT_DIR / f'review-{obj.id}.json').write_text(
                json.dumps(obj.result, ensure_ascii=False)
            )
        except Exception as e:
            obj.status = 'failed'
            obj.note = str(e)
            traceback.print_exc()
        db.commit()


def run_index_build(generation_id: int) -> None:
    with Session() as db:
        try:
            rolling_index.run_build(db, generation_id)
        except Exception:  # 旧代继续服务，状态已标为滞后
            log.exception('rolling yield index build failed')


def run_index_sync() -> None:
    with Session() as db:
        try:
            rolling_index.sync(db, reconcile=False)
        except Exception as exc:  # 保留上一个完整水位并标记滞后；下轮重试自动追平
            db.rollback()
            log.exception('rolling index sync failed')
            rolling_index.sync_failed(db, exc)


def handle(job) -> None:
    if not job:
        return
    kind, value = job[0], int(job[1])
    if kind == BUILD_QUEUE:
        run_index_build(value)
    else:
        run_review(value)


def main() -> None:
    recover_on_start()
    next_sync = 0.0
    while True:
        Path('/tmp/worker-heartbeat').touch()
        now = time.monotonic()
        if now >= next_sync:
            next_sync = now + SYNC_INTERVAL
            run_index_sync()
        handle(queue.blpop([BUILD_QUEUE, JOB_QUEUE], timeout=2))


if __name__ == '__main__':
    main()
