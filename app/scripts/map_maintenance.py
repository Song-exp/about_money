"""Dedicated map worker and aggregate operational alerts; no credentials in output."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.db import SessionLocal
from app.services.map_sync import sync_sources, source_health
from app.services.map_verification import MODELS, evidence_status, verified_location
from app.models.map_verification import MapEvidence


def audit(db):
    counts = {}
    for kind, model in MODELS.items():
        statuses = {}
        for item in db.scalars(select(model)).all():
            status = evidence_status(db.get(MapEvidence, (kind, item.id)), item)
            statuses[status] = statuses.get(status, 0) + 1
        counts[kind] = statuses
    health = source_health(db)
    from app.models.store import Store
    locations = {}
    for kind, model in [('store', Store), ('local', MODELS['local'])]:
        items = db.scalars(select(model)).all()
        verified = sum(verified_location(db, kind, item) is not None for item in items)
        locations[kind] = {'total': len(items), 'verified': verified, 'pending': len(items)-verified}
    alerts = [{'source': s['key'], 'reason': s['error'] or 'stale'} for s in health['sources'] if s['stale'] or s['error']]
    if not health['promotion_sources_configured']: alerts.append({'reason': 'no_live_promotion_feed'})
    for kind, statuses in counts.items():
        for status in ('pending', 'changed', 'recheck_required', 'expired'):
            if statuses.get(status): alerts.append({'kind': kind, 'reason': status, 'count': statuses[status]})
    return {'measured_at': datetime.now(timezone.utc).isoformat(), 'verification': counts, 'locations': locations, 'sources': health, 'alerts': alerts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--output', default='reports/map-improvement-20261013/measurement.json')
    args = parser.parse_args()
    with SessionLocal() as db:
        sync = sync_sources(db, force=args.force) if args.sync else None
        result = audit(db)
        result['sync'] = sync
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'verification': result['verification'], 'alerts': result['alerts'], 'sync': sync}, ensure_ascii=False))


if __name__ == '__main__': main()
