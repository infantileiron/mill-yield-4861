from sqlalchemy import String, Float, Integer, BigInteger, ForeignKey, JSON, Text, DateTime, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from src.config.database import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    line: Mapped[str] = mapped_column(String(20))
    supplier: Mapped[str] = mapped_column(String(80))
    tonnes: Mapped[float] = mapped_column(Float)
    received_date: Mapped[str] = mapped_column(String(10))


class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_code: Mapped[str] = mapped_column(String(70), unique=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    flour_yield: Mapped[float] = mapped_column(Float)
    moisture: Mapped[float] = mapped_column(Float)
    inspector: Mapped[str] = mapped_column(String(50))
    # Actual sampling instant in UTC. Rows imported from old spreadsheets have no
    # reliable time and stay NULL; they are reported separately, never in a window.
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Entry instant, used to explain late arrivals. NULL for migrated legacy rows.
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=lambda: datetime.now(timezone.utc)
    )
    __table_args__ = (Index("ix_inspections_sampled_at", "sampled_at"),)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    line: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100))
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # legacy = original full-inspection scan; indexed = frozen rolling-index window.
    source: Mapped[str] = mapped_column(String(20), default="legacy")
    index_generation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    index_entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_lagging: Mapped[bool] = mapped_column(Boolean, default=False)
    window_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legacy_count: Mapped[int] = mapped_column(Integer, default=0)


class Audit(Base):
    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(200))
    status: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class YieldIndexState(Base):
    """Single-row control record for the rolling yield index (id is always 1)."""

    __tablename__ = "yield_index_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_generation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_generation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="empty")
    last_error: Mapped[str] = mapped_column(Text, default="")
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class YieldIndexGeneration(Base):
    __tablename__ = "yield_index_generations"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="building")
    window_hours: Mapped[int] = mapped_column(Integer, default=24)
    snapshot_max_id: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_through_id: Mapped[int] = mapped_column(BigInteger, default=0)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    copy_done: Mapped[bool] = mapped_column(Boolean, default=False)
    # 切换后的补齐尚未成功。任何一次追平都会补做窗口反连接，确保并发提交不漏。
    needs_reconcile: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class YieldIndexEntry(Base):
    """Materialised sample row. Queries and diagnosis never read `inspections`."""

    __tablename__ = "yield_index_entries"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    generation_id: Mapped[int] = mapped_column(Integer, index=True)
    inspection_id: Mapped[int] = mapped_column(BigInteger)
    line: Mapped[str] = mapped_column(String(20))
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flour_yield: Mapped[float] = mapped_column(Float)
    moisture: Mapped[float] = mapped_column(Float)
    sample_code: Mapped[str] = mapped_column(String(70))
    batch_code: Mapped[str] = mapped_column(String(50))
    inspector: Mapped[str] = mapped_column(String(50))
    # Entry instant copied alongside the sample so queries never touch `inspections`.
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("generation_id", "inspection_id", name="uq_yield_index_generation_inspection"),
        Index("ix_yield_index_gen_line_time", "generation_id", "line", "sampled_at"),
        Index("ix_yield_index_gen_time", "generation_id", "sampled_at"),
        Index("ix_yield_index_gen_inspection", "generation_id", "inspection_id"),
    )
