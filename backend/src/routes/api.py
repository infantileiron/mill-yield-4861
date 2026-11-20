from fastapi import APIRouter, Depends
from sqlalchemy import select, func

from src.config.database import get_db
from src.controllers.auth import current, writable, admin, login
from src.controllers.export import export_review
from src.schemas.business import (
    Login,
    BatchInput,
    InspectionInput,
    ReviewInput,
    IndexedReviewInput,
    ApprovalInput,
    IndexBuildInput,
)
from src.models.entities import Batch, Inspection, Review, Audit
from src.services import business
from src.utils.serialize import record

router = APIRouter()


@router.post("/auth/login")
def auth(data: Login, db=Depends(get_db)):
    return login(db, data)


@router.get("/auth/me")
def me(u=Depends(current)):
    return {"email": u.email, "role": u.role}


@router.get("/batches")
def get_batches(db=Depends(get_db), u=Depends(current)):
    return business.batches(db)


@router.post("/batches", status_code=201)
def add_batch(data: BatchInput, db=Depends(get_db), u=Depends(writable)):
    return business.create(db, Batch, data.model_dump())


@router.get("/inspections")
def get_inspections(batch_id: int | None = None, db=Depends(get_db), u=Depends(current)):
    return business.inspections(db, batch_id)


@router.post("/inspections", status_code=201)
def add_inspection(data: InspectionInput, db=Depends(get_db), u=Depends(writable)):
    return business.add_inspection(db, data.model_dump())


# --- Rolling yield index ---------------------------------------------------
@router.get("/index/status")
def index_status(db=Depends(get_db), u=Depends(current)):
    return business.index_status(db)


@router.post("/index/build", status_code=202)
def index_build(data: IndexBuildInput | None = None, db=Depends(get_db), u=Depends(writable)):
    return business.build_index(db, (data or IndexBuildInput()).window_hours)


@router.post("/index/sync")
def index_sync(db=Depends(get_db), u=Depends(writable)):
    return business.sync_index(db)


@router.get("/index/window")
def index_window(line: str | None = None, hours: int | None = None, db=Depends(get_db), u=Depends(current)):
    return business.index_window(db, line=line, hours=hours)


# --- Reviews ---------------------------------------------------------------
@router.get("/reviews")
def get_reviews(db=Depends(get_db), u=Depends(current)):
    return [{k: v for k, v in record(x).items() if k != "snapshot"}
            for x in db.scalars(select(Review).order_by(Review.id.desc()))]


@router.post("/reviews", status_code=202)
def create_review(data: ReviewInput, db=Depends(get_db), u=Depends(writable)):
    """原有诊断：全量历史快照，口径与结果保持不变。"""
    return business.submit(db, data.model_dump(), u)


@router.post("/reviews/indexed", status_code=202)
def create_indexed_review(data: IndexedReviewInput, db=Depends(get_db), u=Depends(writable)):
    """滚动索引诊断：受理时固定索引水位与窗口样本。"""
    return business.submit_indexed(db, data.model_dump(), u)


@router.get("/reviews/{id}")
def get_review(id: int, db=Depends(get_db), u=Depends(current)):
    return record(business.review(db, id))


@router.post("/reviews/{id}/approve")
def approve(id: int, data: ApprovalInput, db=Depends(get_db), u=Depends(admin)):
    return business.approve(db, id, data.note, u)


@router.get("/reviews/{id}/export")
def export(id: int, db=Depends(get_db), u=Depends(current)):
    return export_review(business.review(db, id))


@router.get("/audits")
def audits(db=Depends(get_db), u=Depends(admin)):
    return [record(x) for x in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]


@router.get("/counts")
def counts(db=Depends(get_db), u=Depends(current)):
    return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (Batch, Inspection, Review)}
