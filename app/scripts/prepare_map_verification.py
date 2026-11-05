"""Back up local SQLite before additive map tables; never relabel old data approved."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy import select, func
from app.core.db import engine, SessionLocal
from app.models.map_verification import MapEvidence, MapLocation, MapJobLease, MapReviewEvent
from app.models.card import CardBenefit
from app.models.store import StoreOffer
from app.models.local_benefit import LocalBenefitMerchant


def main():
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('Use alembic upgrade head for non-SQLite deployment')
    root = Path('reports/map-improvement-20261013')
    root.mkdir(parents=True, exist_ok=True)
    database = Path(engine.url.database).resolve()
    backup = Path('_backup/map-improvement-20261013')
    backup.mkdir(parents=True, exist_ok=True)
    target = backup / f'before-map-{datetime.now():%H%M%S}.db'
    with sqlite3.connect(database) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    with SessionLocal() as db:
        before = {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (CardBenefit, StoreOffer, LocalBenefitMerchant)}
    for model in (MapEvidence, MapLocation, MapJobLease, MapReviewEvent):
        model.__table__.create(engine, checkfirst=True)
    with SessionLocal() as db:
        after = {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (CardBenefit, StoreOffer, LocalBenefitMerchant)}
    assert before == after
    result = {'backup': str(target), 'before': before, 'after': after, 'approved_legacy_rows': 0}
    (root / 'migration.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
