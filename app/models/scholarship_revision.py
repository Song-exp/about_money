"""추천/작성 근거의 구조화 스냅샷. 공고 ID와 원문 버전을 보존한다."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.types import JSONType, PKType


class ScholarshipRevision(Base):
    __tablename__ = "scholarship_revisions"
    __table_args__ = (UniqueConstraint("scholarship_id", "sha256", name="uq_scholarship_revision_hash"),)

    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    scholarship_id: Mapped[int] = mapped_column(ForeignKey("scholarships.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
