"""Observe completed scheduled card runs without triggering or accelerating collection."""
import argparse
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.core.config import settings
from app.models.card import Card, CardBenefit
from app.models.card_collection import (
    CardCollectionRun, CardCollectionSource, CardProductSource, CardSourceSnapshot,
)
from app.services.card_collection import utc


def measure():
    with SessionLocal() as db:
        rows = list(db.scalars(select(CardCollectionRun).where(
            CardCollectionRun.trigger == 'scheduled', CardCollectionRun.finished_at.is_not(None))
            .order_by(CardCollectionRun.started_at.desc()).limit(2)))
        runs = [{'id': row.id, 'started_at': utc(row.started_at).isoformat(),
            'finished_at': utc(row.finished_at).isoformat(), 'status': row.status, 'counts': row.counts}
            for row in reversed(rows)]
        result = {'configured_interval_sec': settings.card_collection_interval_sec,
            'scheduled_runs': runs,
            'counts': {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                for model in (CardCollectionSource, CardProductSource, CardSourceSnapshot, Card, CardBenefit)},
            'candidates_by_namespace': dict(db.execute(select(CardProductSource.source_namespace,
                func.count()).group_by(CardProductSource.source_namespace)).all()),
            'source_errors': [{'url': row.canonical_url, 'error': row.last_error}
                for row in db.scalars(select(CardCollectionSource).where(CardCollectionSource.last_error.is_not(None)))]}
        if len(rows) == 2:
            result['run_start_interval_sec'] = (utc(rows[0].started_at) - utc(rows[1].started_at)).total_seconds()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + max(0, args.timeout)
    while True:
        result = measure()
        if len(result['scheduled_runs']) >= 2 or time.monotonic() >= deadline:
            break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
