"""Immutable PDF/embedded-HTML text derivations; preserve source snapshots."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import PKType


class CardDocumentText(Base):
    __tablename__ = 'card_document_texts'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey('card_source_snapshots.id'), index=True)
    parser_version: Mapped[str] = mapped_column(String(80))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    text_sha256: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('snapshot_id', 'parser_version', name='uq_card_document_text_parser'),)
