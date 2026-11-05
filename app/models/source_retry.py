"""공고별 영속 재시도 큐. 실행은 기존 크롤 워커가 담당한다."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.types import PKType


class SourceRetryJob(Base):
    __tablename__ = "source_retry_jobs"
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    content_key: Mapped[str] = mapped_column(String(64), unique=True)
    source_platform: Mapped[str] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
