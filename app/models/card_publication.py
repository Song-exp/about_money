"""Current approved program pointer; immutable payload and audit stay elsewhere."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base


class CardProgramPublication(Base):
    __tablename__ = 'card_program_publications'
    program_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey('card_benefit_versions.id'), unique=True)
    card_id: Mapped[int] = mapped_column(ForeignKey('cards.id'))
    status: Mapped[str] = mapped_column(String(30))
    payload_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recheck_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
