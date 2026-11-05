"""Card discovery and immutable official-source archive; independent of publication."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.types import JSONType, PKType


class CardIssuer(Base):
    __tablename__ = 'card_issuers'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    issuer_key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))


class CardCollectionSource(Base):
    __tablename__ = 'card_collection_sources'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey('card_issuers.id'))
    provider_key: Mapped[str] = mapped_column(String(100))
    source_kind: Mapped[str] = mapped_column(String(30))
    canonical_url: Mapped[str] = mapped_column(Text)
    adapter_key: Mapped[str] = mapped_column(String(100), default='archive_v1')
    enabled: Mapped[bool] = mapped_column(default=True)
    schedule_policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (Index('ix_card_sources_due', 'enabled', 'next_check_at'),)


class CardProductSource(Base):
    __tablename__ = 'card_product_sources'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey('cards.id'), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('card_collection_sources.id'), index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey('card_issuers.id'))
    source_namespace: Mapped[str] = mapped_column(String(100))
    external_product_code: Mapped[str] = mapped_column(String(150))
    product_name: Mapped[str] = mapped_column(String(200))
    product_type: Mapped[str] = mapped_column(String(30))
    review_state: Mapped[str] = mapped_column(String(30), default='pending')
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('source_namespace', 'external_product_code', name='uq_card_external_product'),)


class CardSourceSnapshot(Base):
    __tablename__ = 'card_source_snapshots'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('card_collection_sources.id'), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    semantic_sha256: Mapped[str] = mapped_column(String(64))
    raw_object_ref: Mapped[str] = mapped_column(Text)
    extracted_text: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(80))
    parser_version: Mapped[str] = mapped_column(String(80))
    completeness: Mapped[str] = mapped_column(String(30))
    __table_args__ = (UniqueConstraint('source_id', 'raw_sha256', name='uq_card_source_snapshot'),)


class CardCollectionRun(Base):
    __tablename__ = 'card_collection_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    counts: Mapped[dict] = mapped_column(JSONType, default=dict)


class CardCollectionAttempt(Base):
    __tablename__ = 'card_collection_attempts'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey('card_collection_runs.id'), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('card_collection_sources.id'), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey('card_source_snapshots.id'))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))


COLLECTION_MODELS = (CardIssuer, CardCollectionSource, CardProductSource, CardSourceSnapshot,
                     CardCollectionRun, CardCollectionAttempt)
