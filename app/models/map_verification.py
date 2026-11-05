"""Review evidence and location identity independent of legacy confidence labels."""
from datetime import date, datetime
from sqlalchemy import Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import JSONType
from app.core.types import PKType


class MapEvidence(Base):
    __tablename__ = 'map_evidence'
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    source_url: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(100))
    source_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    item_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recheck_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    indefinite: Mapped[bool] = mapped_column(default=False)
    rules: Mapped[dict] = mapped_column(JSONType, default=dict)


class MapLocation(Base):
    __tablename__ = 'map_locations'
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64))
    place_id: Mapped[str] = mapped_column(String(100), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    source_url: Mapped[str] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str] = mapped_column(String(100))


class MapJobLease(Base):
    __tablename__ = 'map_job_leases'
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MapReviewEvent(Base):
    __tablename__ = 'map_review_events'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(30))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONType)
