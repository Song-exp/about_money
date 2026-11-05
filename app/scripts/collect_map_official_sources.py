"""Expand stable card keys to local IDs; capture evidence without approving it."""
import argparse
import json
from datetime import datetime
from pathlib import Path
import sqlite3
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.card import Card, CardBenefit, BenefitType
from app.services.map_sync import sync_sources
from app.services.map_verification import today_kst


def resolve_sources(db, catalog):
    sources, missing = [], []
    today = today_kst()
    for entry in catalog['sources']:
        stmt = select(CardBenefit).join(Card).where(Card.card_key == entry['card_key'],
            CardBenefit.is_active.is_(True), CardBenefit.benefit_type != BenefitType.EXPIRED)
        if entry.get('brands'): stmt = stmt.where(CardBenefit.brand_key.in_(entry['brands']))
        items = [item for item in db.scalars(stmt).all()
                 if not (item.valid_from and item.valid_from > today) and not (item.valid_to and item.valid_to < today)]
        if not items: missing.append(entry['card_key'])
        sources.extend({'kind': 'card', 'item_id': item.id, 'url': entry['url']} for item in items)
    return sources, missing


def persist_sources(sources):
    path = Path('.env')
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    prefix = 'MAP_OFFICIAL_SOURCES_JSON='
    previous = next((line[len(prefix):] for line in lines if line.startswith(prefix)), '[]')
    values = json.loads(previous.strip().strip("'").strip('"'))
    merged = {(row['kind'], row['item_id']): row for row in values}
    merged.update({(row['kind'], row['item_id']): row for row in sources})
    lines = [line for line in lines if not line.startswith(prefix)]
    lines.append(prefix + json.dumps(list(merged.values()), ensure_ascii=False, separators=(',', ':')))
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--persist', action='store_true')
    args = parser.parse_args()
    output = Path('reports/map-improvement-20261013-followup')
    output.mkdir(parents=True, exist_ok=True)
    if engine.dialect.name == 'sqlite':
        backup = Path('_backup/map-improvement-20261013-followup')
        backup.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(engine.url.database) as original, sqlite3.connect(backup / f'before-collect-{datetime.now():%H%M%S}.db') as copied:
            original.backup(copied)
    catalog = json.loads(Path('data/map_official_source_catalog.json').read_text(encoding='utf-8'))
    with SessionLocal() as db:
        sources, missing = resolve_sources(db, catalog)
        existing = json.loads(settings.map_official_sources_json)
        merged = {(row['kind'], row['item_id']): row for row in existing}
        merged.update({(row['kind'], row['item_id']): row for row in sources})
        settings.map_official_sources_json = json.dumps(list(merged.values()))
        result = sync_sources(db, force=True)
    if args.persist: persist_sources(sources)
    report = {'targets': sources, 'missing_card_keys': missing, 'sync': result, 'automatic_approval': False}
    (output / 'official-sources.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'targets': len(sources), 'missing_card_keys': missing, 'sync': result}, ensure_ascii=False))


if __name__ == '__main__': main()
