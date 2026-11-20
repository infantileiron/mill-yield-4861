from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.database import Base, Session, engine
from src.models.entities import Batch, Inspection, User
from src.services import rolling_index as ri
from src.services.business import queue as app_queue
from src.utils.security import password_hash


@pytest.fixture()
def api(monkeypatch):
    """Real FastAPI app, temp SQLite schema, in-memory Redis queue."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.Redis.from_url", classmethod(lambda cls, *a, **k: fake))
    monkeypatch.setattr("src.services.business.queue", fake)
    monkeypatch.setattr("src.main.queue", fake, raising=False)
    assert app_queue is not None

    from src.main import app

    with TestClient(app) as client:
        with Session() as db:
            for role in ("admin", "analyst", "viewer"):
                db.add(User(email=f"{role}@demo.local",
                            password_hash=password_hash("Demo123!"), role=role))
            db.commit()
        yield {"client": client, "fake": fake}
    Base.metadata.drop_all(engine)


def _token(client, email):
    res = client.post("/auth/login", json={"email": email, "password": "Demo123!"})
    assert res.status_code == 200, res.text
    return {"Authorization": "Bearer " + res.json()["access_token"]}


def _seed(count=6, sampled_offset_hours=-1, line="一线"):
    now = datetime.now(timezone.utc)
    with Session() as db:
        batch = Batch(code="QH-API-A", line=line, supplier="丰垦粮业", tonnes=25, received_date="2026-11-20")
        db.add(batch)
        db.flush()
        for i in range(count):
            db.add(Inspection(sample_code=f"API-S{i:02}", batch_id=batch.id,
                              flour_yield=73.0 + i * 0.1, moisture=12.5, inspector="林洁",
                              sampled_at=now + timedelta(hours=sampled_offset_hours),
                              recorded_at=now + timedelta(hours=sampled_offset_hours)))
        db.commit()
    return now


def _build(client, headers):
    res = client.post("/index/build", json={"window_hours": 24}, headers=headers)
    assert res.status_code == 202, res.text
    generation_id = res.json()["generation_id"]
    ri.run_build_in_session(generation_id)
    return generation_id


def test_index_build_status_and_window(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()

    res = client.get("/index/status", headers=headers)
    assert res.status_code == 200
    assert res.json()["built"] is False and res.json()["status"] == "empty"

    generation_id = _build(client, headers)
    body = client.get("/index/status", headers=headers).json()
    assert body["built"] is True and body["lagging"] is False
    assert body["active"]["generation_id"] == generation_id
    assert body["active"]["entry_count"] >= 6

    window = client.get("/index/window", params={"line": "一线"}, headers=headers).json()
    summary = window["lines"]["一线"]
    assert window["built"] is True
    assert summary["n"] >= 6 and summary["q1"] is not None
    assert window["watermark"]["generation_id"] == generation_id
    # The window endpoint returns aggregates and flagged samples, not raw inspection rows.
    assert "inspection_id" not in summary
    assert "checksum" not in summary
    assert all("moisture" in s for s in summary["outliers"]["samples"])


def test_viewer_cannot_build_index(api):
    headers = _token(api["client"], "viewer@demo.local")
    assert api["client"].post("/index/build", json={}, headers=headers).status_code == 403


def test_indexed_and_legacy_reviews_coexist(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    generation_id = _build(client, headers)

    res = client.post("/reviews/indexed",
                      json={"title": "一线最近24小时复核", "line": "一线", "window_hours": 24},
                      headers=headers)
    assert res.status_code == 202, res.text
    review = res.json()
    assert review["source"] == "indexed"
    assert review["index_generation_id"] == generation_id
    assert review["index_watermark"] is not None
    assert review["index_entry_count"] >= 6
    assert review["window_hours"] == 24
    assert "snapshot" not in review
    frozen = client.get(f"/reviews/{review['id']}", headers=headers).json()
    assert len(frozen["snapshot"]) == review["index_entry_count"]

    res = client.post("/reviews", json={"title": "全线历史复核", "line": "全部"}, headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["source"] == "legacy"

    listed = client.get("/reviews", headers=headers).json()
    assert {r["source"] for r in listed} == {"indexed", "legacy"}
    # Legacy review still freezes every historical sample.
    legacy = next(r for r in listed if r["source"] == "legacy")
    detail = client.get(f"/reviews/{legacy['id']}", headers=headers).json()
    assert len(detail["snapshot"]) >= 6


def test_indexed_review_requires_index(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    res = client.post("/reviews/indexed", json={"title": "无索引诊断", "line": "一线"}, headers=headers)
    assert res.status_code == 409
    assert "索引" in res.json()["detail"]


def test_new_inspection_is_indexed_and_window_updates(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    now = _seed()
    _build(client, headers)
    batch_id = client.get("/batches", headers=headers).json()[0]["id"]

    res = client.post("/inspections", json={
        "sample_code": "API-NEW-1", "batch_id": batch_id, "flour_yield": 74.5,
        "moisture": 12.7, "inspector": "林洁",
        "sampled_at": (now - timedelta(minutes=10)).isoformat(),
    }, headers=headers)
    assert res.status_code == 201, res.text
    assert res.json()["indexed"] == "inserted"
    assert res.json()["late"] is False

    window = client.get("/index/window", params={"line": "一线"}, headers=headers).json()
    assert window["lines"]["一线"]["n"] >= 7

    # Late arrival: sampled 20h ago but entered now, inside the window -> indexed + flagged.
    res = client.post("/inspections", json={
        "sample_code": "API-LATE-1", "batch_id": batch_id, "flour_yield": 74.1,
        "moisture": 12.7, "inspector": "林洁",
        "sampled_at": (now - timedelta(hours=20)).isoformat(),
    }, headers=headers)
    assert res.status_code == 201, res.text
    assert res.json()["indexed"] == "inserted"
    assert res.json()["late"] is True
    window = client.get("/index/window", headers=headers).json()
    assert window["lines"]["一线"]["late_count"] == 1

    # Outside the window: kept as a record, not indexed, reported as such.
    res = client.post("/inspections", json={
        "sample_code": "API-OLD-1", "batch_id": batch_id, "flour_yield": 74.1,
        "moisture": 12.7, "inspector": "林洁",
        "sampled_at": (now - timedelta(hours=40)).isoformat(),
    }, headers=headers)
    assert res.status_code == 201
    assert res.json()["indexed"] == "out-of-window"

    # Unknown sampling time: separate legacy bucket, never mixed into the window.
    res = client.post("/inspections", json={
        "sample_code": "API-LEGACY-1", "batch_id": batch_id, "flour_yield": 73.9,
        "moisture": 12.9, "inspector": "林洁",
    }, headers=headers)
    assert res.status_code == 201
    assert res.json()["sampled_at"] is None
    window = client.get("/index/window", headers=headers).json()
    assert window["legacy"]["count"] == 1
    assert window["legacy"]["samples"][0]["sample_code"] == "API-LEGACY-1"
    assert window["legacy"]["samples"][0]["sampled_at"] is None


def test_future_sampling_time_rejected(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    batch_id = client.get("/batches", headers=headers).json()[0]["id"]
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    res = client.post("/inspections", json={
        "sample_code": "API-FUTURE", "batch_id": batch_id, "flour_yield": 74,
        "moisture": 12, "inspector": "林洁", "sampled_at": future,
    }, headers=headers)
    assert res.status_code == 422


def test_export_includes_sampling_time_and_window_metadata(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    generation_id = _build(client, headers)
    review = client.post("/reviews/indexed",
                         json={"title": "导出窗口复核", "line": "一线"}, headers=headers).json()
    # Simulate worker completion.
    from src.services.statistics import diagnose
    with Session() as db:
        from src.models.entities import Review
        obj = db.get(Review, review["id"])
        obj.result = diagnose(obj.snapshot)
        obj.status = "completed"
        db.commit()
    res = client.get(f"/reviews/{review['id']}/export", headers=headers)
    assert res.status_code == 200
    text = res.text
    assert "滚动出粉率索引" in text
    assert str(generation_id) in text
    assert "采样时刻" in text
    assert "API-S00" in text


def test_index_sync_endpoint_guards_failures(api, monkeypatch):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    _build(client, headers)

    def boom(*args, **kwargs):
        raise RuntimeError("追平失败")

    monkeypatch.setattr(ri, "_source_select", boom)
    res = client.post("/index/sync", headers=headers)
    assert res.status_code == 503
    status = client.get("/index/status", headers=headers).json()
    assert status["built"] is True
    assert status["lagging"] is True
    assert status["active"]["generation_id"]


def test_index_window_rejects_longer_window(api):
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    _seed()
    _build(client, headers)
    assert client.get("/index/window", params={"hours": 48}, headers=headers).status_code == 422
    assert client.get("/index/window", params={"hours": 12}, headers=headers).status_code == 200


def test_inspection_survives_index_write_failure(api, monkeypatch):
    """索引写入异常不能吞掉已提交的检验留痕；追平稍后补齐。"""
    client = api["client"]
    headers = _token(client, "analyst@demo.local")
    now = _seed()
    _build(client, headers)
    batch_id = client.get("/batches", headers=headers).json()[0]["id"]

    import src.services.rolling_index as real_ri

    original = real_ri.apply_inspection

    def boom(*args, **kwargs):
        raise RuntimeError("索引写入故障")

    monkeypatch.setattr(real_ri, "apply_inspection", boom)
    res = client.post("/inspections", json={
        "sample_code": "API-RESILIENT", "batch_id": batch_id, "flour_yield": 74,
        "moisture": 12, "inspector": "林洁",
        "sampled_at": (now - timedelta(minutes=5)).isoformat(),
    }, headers=headers)
    assert res.status_code == 201, res.text
    assert res.json()["indexed"] == "deferred"
    assert client.get("/index/status", headers=headers).json()["lagging"] is True

    listed = client.get("/inspections", headers=headers).json()
    assert any(x["sample_code"] == "API-RESILIENT" for x in listed)

    # Once writing works again, the watermark sync fills the gap without duplicates.
    monkeypatch.setattr(real_ri, "apply_inspection", original)
    from src.config.database import Session
    with Session() as db:
        real_ri.sync(db, reconcile=True)
    window = client.get("/index/window", headers=headers).json()
    assert window["lines"]["一线"]["n"] >= 7
