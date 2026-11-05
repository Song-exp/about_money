"""공고 수명과 독립적인 원본 버전 및 수집 시도 기록."""
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.types import JSONType, PKType


class SourceDocument(Base):
    __tablename__ = "source_documents"

    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceFetchAttempt(Base):
    __tablename__ = "source_fetch_attempts"

    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    content_key: Mapped[str] = mapped_column(String(64), index=True)
    source_platform: Mapped[str] = mapped_column(String(100), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    # 항목별 URL, filename, kind, status, error, document_id, sha256, extracted_text.
    documents: Mapped[list] = mapped_column(JSONType, default=list)
    listing_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
