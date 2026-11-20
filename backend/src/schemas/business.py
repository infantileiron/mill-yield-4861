from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Login(BaseModel):
    email: str
    password: str


class BatchInput(BaseModel):
    code: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    line: Literal["一线", "二线", "三线"]
    supplier: str = Field(min_length=1, max_length=80)
    tonnes: float = Field(gt=0, le=10000, allow_inf_nan=False)
    received_date: str

    @field_validator("received_date")
    @classmethod
    def valid_date(cls, v):
        from datetime import date

        date.fromisoformat(v)
        return v


class InspectionInput(BaseModel):
    sample_code: str = Field(min_length=3, max_length=70, pattern=r"^[A-Za-z0-9_-]+$")
    batch_id: int = Field(gt=0)
    flour_yield: float = Field(gt=0, le=100, allow_inf_nan=False)
    moisture: float = Field(ge=0, le=30, allow_inf_nan=False)
    inspector: str = Field(min_length=1, max_length=50)
    # 实际采样时刻（带时区）。旧记录无可靠时间，可留空并单列。
    sampled_at: datetime | None = None


class ReviewInput(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    line: Literal["全部", "一线", "二线", "三线"] = "全部"


class IndexedReviewInput(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    line: Literal["一线", "二线", "三线"] = "一线"
    window_hours: int = Field(default=24, ge=1, le=24)


class ApprovalInput(BaseModel):
    note: str = Field(min_length=5, max_length=1500)


class IndexBuildInput(BaseModel):
    window_hours: int = Field(default=24, ge=1, le=24)
