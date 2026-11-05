"""사용자·장학금·양식 버전별 신청서 작성 대화."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import GUIDType, JSONType


class WritingSession(Base):
    __tablename__ = "writing_sessions"
    id: Mapped[uuid.UUID] = mapped_column(GUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUIDType, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scholarship_id: Mapped[int] = mapped_column(ForeignKey("scholarships.id", ondelete="RESTRICT"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("scholarship_revisions.id", ondelete="RESTRICT"))
    form_fingerprint: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSONType, default=dict)
    fields: Mapped[list] = mapped_column(JSONType, default=list)
    messages: Mapped[list] = mapped_column(JSONType, default=list)
    answers: Mapped[dict] = mapped_column(JSONType, default=dict)
    workflow: Mapped[dict] = mapped_column(JSONType, default=dict)
    application_id: Mapped[uuid.UUID | None] = mapped_column(GUIDType, ForeignKey("user_applications.id", ondelete="SET NULL"), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="collecting")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("user_id", "scholarship_id", "form_fingerprint", name="uq_writing_user_scholarship_form"),)
