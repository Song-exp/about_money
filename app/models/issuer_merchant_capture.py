"""Query-scoped public directory evidence, separate from benefit approval."""
from datetime import datetime
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.core.types import JSONType, PKType


class IssuerMerchantCapture(Base):
    __tablename__ = 'issuer_merchant_captures'
    id: Mapped[int] = mapped_column(PKType, primary_key=True, autoincrement=True)
    capture_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issuer_key: Mapped[str] = mapped_column(String(80))
    keyword: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    complete: Mapped[bool] = mapped_column(default=False)
    records: Mapped[list] = mapped_column(JSONType)
    pages: Mapped[list] = mapped_column(JSONType)

