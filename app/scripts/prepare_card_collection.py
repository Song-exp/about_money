"""Back up local SQLite and add archive tables without changing existing benefits."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.core.db import engine, SessionLocal
from app.models.card import Card, CardBenefit
from app.models.card_collection import COLLECTION_MODELS
from app.models.card_benefit_version import BENEFIT_VERSION_MODELS
from app.models.card_publication import CardProgramPublication
from app.models.card_document_text import CardDocumentText
from app.models.card_document_import import CardDocumentImport
from app.models.issuer_merchant_capture import IssuerMerchantCapture
from app.models.store import StoreOffer


def main():
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('Use alembic upgrade head for non-SQLite deployment')
    database = Path(engine.url.database).resolve()
    if not database.is_file():
        raise RuntimeError('Local database must already exist')
    folder = Path('_backup/card-collection')
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / f'before-archive-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    def counts():
        with SessionLocal() as db:
            return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                    for model in (Card, CardBenefit, StoreOffer)}
    before = counts()
    models = (*COLLECTION_MODELS, *BENEFIT_VERSION_MODELS, CardProgramPublication, CardDocumentText, CardDocumentImport, IssuerMerchantCapture)
    for model in models:
        model.__table__.create(engine, checkfirst=True)
    after = counts()
    if before != after:
        raise RuntimeError('legacy_counts_changed')
    print(json.dumps({'backup': str(backup), 'before': before, 'after': after,
                      'ensured_tables': [model.__tablename__ for model in models]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
