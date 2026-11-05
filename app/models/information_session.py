"""Account-owned information conversations; runtime cache is not the durable source."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.types import GUIDType, JSONType


class InformationSession(Base):
    __tablename__ = 'information_sessions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[object] = mapped_column(GUIDType, ForeignKey('users.id', ondelete='CASCADE'), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
