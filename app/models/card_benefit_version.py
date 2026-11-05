"""Extracted benefit candidates and immutable field evidence; never map publication."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import JSONType, PKType


class CardBenefitVersion(Base):
    __tablename__ = 'card_benefit_versions'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    program_key: Mapped[str] = mapped_column(String(100), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('card_collection_sources.id'), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey('card_source_snapshots.id'))
    extractor_version: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSONType)
    review_state: Mapped[str] = mapped_column(String(30), default='pending')
    publication_state: Mapped[str] = mapped_column(String(30), default='draft')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('snapshot_id', 'extractor_version', name='uq_card_benefit_extraction_version'),)


class CardBenefitFieldEvidence(Base):
    __tablename__ = 'card_benefit_field_evidence'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey('card_benefit_versions.id'), index=True)
    field_path: Mapped[str] = mapped_column(String(200))
    quote: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint('version_id', 'field_path', name='uq_card_benefit_field_evidence'),)


class CardBenefitExtraction(Base):
    __tablename__ = 'card_benefit_extractions'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('card_collection_sources.id'), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey('card_source_snapshots.id'))
    extractor_version: Mapped[str] = mapped_column(String(80))
    version_id: Mapped[int | None] = mapped_column(ForeignKey('card_benefit_versions.id'))
    status: Mapped[str] = mapped_column(String(30))
    issues: Mapped[list] = mapped_column(JSONType, default=list)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('snapshot_id', 'extractor_version', name='uq_card_benefit_extraction_result'),)


BENEFIT_VERSION_MODELS = (CardBenefitVersion, CardBenefitFieldEvidence, CardBenefitExtraction)
