import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.config.database import Base, engine, Session
from src.config.migrations import ensure_schema
from src.models import entities
from src.routes.api import router
from src.middlewares.audit import AuditMiddleware
from src.middlewares.rate_limit import RateLimitMiddleware
from src.services import rolling_index
from src.services.business import queue


@asynccontextmanager
async def lifespan(app):
    ensure_schema(engine)
    with Session() as db:
        # 启动时只清理中断的构建记录；已切换的旧代继续提供上一个完整水位。
        rolling_index.recover(db)
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:
            logging.exception('demo seed failed')
    yield


app = FastAPI(title='青禾磨坊 · 出粉率复核', lifespan=lifespan)
app.add_middleware(AuditMiddleware)
app.add_middleware(RateLimitMiddleware)
app.include_router(router)


@app.get('/health')
def health():
    with Session() as db:
        db.execute(text('SELECT 1'))
    queue.ping()
    return {'status': 'ok'}


@app.exception_handler(Exception)
async def error(request, exc):
    logging.exception('request failed')
    return JSONResponse({'detail': '处理失败，请重试；若持续发生请联系负责人'}, 500)
