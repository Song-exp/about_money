"""Capture a real Hana disclosure as pending evidence, never automatically approve."""
import json
import argparse
from pathlib import Path
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.card import Card, CardBenefit
from app.services.map_sync import sync_sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--persist', action='store_true')
    args = parser.parse_args()
    with SessionLocal() as db:
        item = db.scalar(select(CardBenefit).join(Card).where(Card.card_name == '네이버페이 머니 하나 체크카드'))
        if item is None: raise RuntimeError('matching_card_not_found')
        source = {'kind': 'card', 'item_id': item.id, 'url': 'https://m.hanacard.co.kr/leaflet/13/13502_20220722.pdf'}
        settings.map_official_sources_json = json.dumps([source])
        result = sync_sources(db, force=True)
    if args.persist:
        path = Path('.env')
        text = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        lines = text.splitlines()
        prefix = 'MAP_OFFICIAL_SOURCES_JSON='
        existing = next((line[len(prefix):] for line in lines if line.startswith(prefix)), '[]')
        sources = json.loads(existing.strip().strip("'"))
        if source not in sources: sources.append(source)
        lines = [line for line in lines if not line.startswith(prefix)]
        lines.append(prefix + json.dumps(sources, ensure_ascii=False, separators=(',', ':')))
        if not any(line.startswith('KAKAO_REST_API_KEY=') for line in lines): lines.append('KAKAO_REST_API_KEY=')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    path = Path('reports/map-improvement-20261013/official-source-probe.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'source': source, 'sync': result, 'automatic_approval': False}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
