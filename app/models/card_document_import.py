"""Operator-supplied documents are evidence, never successful HTTP collections."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import PKType


class CardDocumentImport(Base):
    __tablename__ = 'card_document_imports'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey('card_source_snapshots.id'), index=True)
    document_identifier: Mapped[str] = mapped_column(String(30))
    declared_title: Mapped[str] = mapped_column(String(200))
    declared_revision: Mapped[str] = mapped_column(String(200))
    obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    operator: Mapped[str] = mapped_column(String(100))
    origin_url: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('snapshot_id', name='uq_card_document_import_snapshot'),)
