"""Normalize legacy youth-API classifications; retain all IDs, bodies and history."""
import argparse
import json
from datetime import datetime
from pathlib import Path
import sqlite3

from sqlalchemy import select, update

from app.core.db import SessionLocal, engine
from app.models.scholarship import Category, Scholarship
from app.services.consultation_scope import POLICY_PLATFORM


def run(apply=False):
    condition = (Scholarship.source_platform == POLICY_PLATFORM) & (Scholarship.category == Category.SCHOLARSHIP)
    with SessionLocal() as db:
        ids = list(db.scalars(select(Scholarship.id).where(condition)).all())
        result = {'mode': 'apply' if apply else 'dry_run', 'policy_record_ids': ids, 'count': len(ids)}
        if apply and ids:
            if engine.dialect.name == 'sqlite':
                root = Path('data/backups')
                root.mkdir(parents=True, exist_ok=True)
                path = root / ('before-consultation-split-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
                with sqlite3.connect(engine.url.database) as source, sqlite3.connect(path) as backup:
                    source.backup(backup)
                result['backup'] = str(path)
            db.execute(update(Scholarship).where(condition).values(category=Category.GOV_BENEFIT))
            db.commit()
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), ensure_ascii=False, indent=2))
