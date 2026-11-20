#!/usr/bin/env bash
# 复现滚动索引行为的本地验证脚本（无需 Docker / PostgreSQL）。
set -euo pipefail
cd "$(dirname "$0")/../../backend"
python3 -m pytest tests -q
python3 - <<'PY'
"""10 万条检验下的增量/过期耗时与查询计划。"""
import os, random, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

db_path = Path("/tmp/mill-index-bench.db")
db_path.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
sys.path.insert(0, ".")

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.models.entities import Batch, Inspection
from src.services import rolling_index as ri

engine = create_engine(f"sqlite:///{db_path}")
Base.metadata.create_all(engine)
Session = sessionmaker(engine, expire_on_commit=False)
now = datetime.now(timezone.utc).replace(tzinfo=None)

with Session() as db:
    batch = Batch(code="BENCH", line="一线", supplier="s", tonnes=1, received_date="2026-11-20")
    db.add(batch)
    db.flush()
    rows = []
    for i in range(100_000):
        offset = timedelta(minutes=int(random.Random(i * 7919 + 13).random() * 24 * 60))
        sampled = now - offset
        rows.append(dict(sample_code=f"B{i:07}", batch_id=batch.id, flour_yield=65 + i % 20,
                         moisture=12.0, inspector="x", sampled_at=sampled, recorded_at=sampled))
        if len(rows) == 5000:
            db.bulk_insert_mappings(Inspection, rows)
            rows = []
    if rows:
        db.bulk_insert_mappings(Inspection, rows)
    db.commit()

start = time.perf_counter()
with Session() as db:
    generation = ri.build(db, now=now)
print(f"首次建索引: {time.perf_counter() - start:.2f}s, 窗口条目 {generation.entry_count}")

with Session() as db:
    plan = db.execute(text(
        "EXPLAIN QUERY PLAN SELECT * FROM yield_index_entries "
        "WHERE generation_id=1 AND sampled_at >= :c AND sampled_at <= :n AND line=:l"),
        {"c": now - timedelta(hours=24), "n": now, "l": "一线"})
    print("窗口查询计划:", plan.fetchall())

with Session() as db:
    b = db.scalar(select(Batch))
    for i in range(3):
        db.add(Inspection(sample_code=f"NEW{i}", batch_id=b.id, flour_yield=71.0, moisture=12.0,
                          inspector="x", sampled_at=now - timedelta(minutes=5), recorded_at=now))
    db.commit()

start = time.perf_counter()
with Session() as db:
    result = ri.sync(db, now=now)
print(f"新增 3 条追平: {time.perf_counter() - start:.3f}s {result}")

start = time.perf_counter()
with Session() as db:
    result = ri.sync(db, now=now + timedelta(hours=25))
print(f"25 小时后过期清理: {time.perf_counter() - start:.3f}s 过期 {result['expired']} 条")
PY
