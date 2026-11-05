"""Provider identifiers only; no card numbers, passwords, or certificates."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import GUIDType
import uuid


class CardConnection(Base):
    __tablename__ = "card_connections"
    user_id: Mapped[uuid.UUID] = mapped_column(GUIDType, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    organization: Mapped[str] = mapped_column(String(4), primary_key=True)
    environment: Mapped[str] = mapped_column(String(12), primary_key=True)
    encrypted_connected_id: Mapped[str] = mapped_column(Text, nullable=False)
    login_type: Mapped[str] = mapped_column(String(1), nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_queried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
