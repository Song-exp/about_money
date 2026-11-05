"""Run public map sync once, backing up SQLite before the first write."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from app.core.db import SessionLocal, engine
from app.models.map_source import MapSourceState
from app.services.map_sync import sync_sources, source_health


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='reports/map-sync.json')
    args = parser.parse_args()
    if engine.dialect.name == 'sqlite' and engine.url.database != ':memory:':
        target = Path('data/backups') / ('map-sync-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.db')
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(engine.url.database) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
    MapSourceState.__table__.create(engine, checkfirst=True)
    with SessionLocal() as db:
        result = sync_sources(db, force=True)
        result['health'] = source_health(db)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(s['status'] == 'error' for s in result['sources']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
