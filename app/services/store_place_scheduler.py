"""Bounded scheduled place checks for reviewed current directory changes."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, delete, update

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent, MapJobLease
from app.services.map_sync import acquire_lease
from app.services.store_directory_collection import configured_directories
from app.services.store_directory_revalidation import ACTION, reviewed_change, candidate_for, revalidate_change
from app.services.store_directory_review import aware

KEY = 'store_place_revalidation'
TRANSIENT = {'place_lookup_failed', 'invalid_place_response', 'place_response_too_large', 'place_results_incomplete'}


def require_lease(db, owner):
    if not db.execute(update(MapJobLease).where(MapJobLease.key == KEY, MapJobLease.owner == owner,
            MapJobLease.expires_at > datetime.now(timezone.utc)).values(owner=owner)).rowcount:
        raise RuntimeError('store_place_worker_lease_lost')


def targets(db, blocked=None):
    result = []
    for source in configured_directories():
        event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_changed',
            MapReviewEvent.payload['source_key'].as_string() == source['source_key']).order_by(MapReviewEvent.id.desc()))
        if not event:
            continue
        review = db.scalar(select(MapReviewEvent.id).where(MapReviewEvent.action == 'store_directory_reviewed',
            MapReviewEvent.payload['change_id'].as_integer() == event.id))
        if not review:
            continue
        for delta in event.payload['changes']:
            change_id, official_id, fingerprint = event.id, delta['official_id'], event.payload['after_identity_hash']
            applied = db.scalar(select(MapReviewEvent.id).where(MapReviewEvent.action == 'store_directory_applied',
                MapReviewEvent.payload['change_id'].as_integer() == change_id,
                MapReviewEvent.payload['official_id'].as_string() == official_id))
            if applied:
                continue
            item = {'change_id': change_id, 'official_id': official_id, 'identity_hash': fingerprint,
                'source_key': source['source_key'], 'key': f'place-recheck:{change_id}:{official_id}'}
            try:
                current, _, _ = reviewed_change(db, change_id, fingerprint)
                candidate_for(current, official_id)
            except (ValueError, KeyError, TypeError) as exc:
                # The manual review queue remains the source for resolving invalid/missing identities.
                if blocked is not None:
                    reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith((
                        'store_directory_', 'invalid_official_', 'outside_review_', 'unsupported_store_')) else 'invalid_official_identity'
                    blocked.append({'change_id': change_id, 'official_id': official_id, 'reason': reason})
                continue
            state = db.get(MapSourceState, item['key'])
            next_at = aware(state.next_retry_at) if state and state.next_retry_at else datetime(1970, 1, 1, tzinfo=timezone.utc)
            latest = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == ACTION,
                MapReviewEvent.payload['change_id'].as_integer() == change_id,
                MapReviewEvent.payload['official_id'].as_string() == official_id).order_by(MapReviewEvent.id.desc()))
            if latest and (not state or not state.last_attempt_at or aware(latest.at) > aware(state.last_attempt_at)):
                delay = 300 if latest.payload.get('reason') in TRANSIENT else 86400
                next_at = aware(latest.at) + timedelta(seconds=delay)
            item['next_attempt_at'] = next_at
            result.append(item)
    return sorted(result, key=lambda row: (row['next_attempt_at'], row['change_id'], row['official_id']))


def run_due(db, client=None):
    if not settings.store_place_revalidation_enabled or not settings.store_collection_enabled:
        return {'status': 'disabled', 'map_publication': False}
    owner = acquire_lease(db, KEY)
    if not owner:
        return {'status': 'busy', 'map_publication': False}
    started = datetime.now(timezone.utc)
    try:
        monitor = db.get(MapSourceState, KEY) or MapSourceState(source_key=KEY,
            source_kind='place_revalidation_job', source_url='internal:store-place-revalidation', failures=0, records=[])
        monitor.last_attempt_at = started
        monitor.status = 'running'
        db.add(monitor)
        db.commit()
        report = {'status': 'complete', 'results': [], 'map_publication': False}
        if not settings.kakao_rest_api_key:
            report.update(status='blocked', error='kakao_rest_api_key_required')
        else:
            blocked = []
            pending = targets(db, blocked)
            report['blocked_targets'] = blocked
            due = [row for row in pending if row['next_attempt_at'] <= started]
            report.update(eligible=len(pending), due=len(due), deferred=max(0, len(due) - settings.store_place_revalidation_batch_size))
            for item in due[:settings.store_place_revalidation_batch_size]:
                require_lease(db, owner)
                db.commit()
                try:
                    checked = revalidate_change(db, item['change_id'], item['official_id'], item['identity_hash'], client=client)
                except ValueError:
                    db.rollback()
                    report['results'].append({'change_id': item['change_id'], 'official_id': item['official_id'],
                        'status': 'skipped', 'reason': 'prerequisite_changed_or_busy'})
                    continue
                require_lease(db, owner)
                state = db.get(MapSourceState, item['key']) or MapSourceState(source_key=item['key'],
                    source_kind='place_revalidation', source_url='internal:store-directory-change', records=[], failures=0)
                completed = datetime.now(timezone.utc)
                state.last_attempt_at = completed
                state.status = checked['status']
                state.error = checked.get('reason')
                state.failures = 0 if checked['status'] == 'matched' else (state.failures or 0) + 1
                if checked['status'] == 'matched':
                    state.last_success_at = completed
                delay = min(86400, 300 * 2 ** min(state.failures - 1, 8)) if state.error in TRANSIENT else 86400
                state.next_retry_at = completed + timedelta(seconds=delay)
                db.add(state)
                db.commit()
                report['results'].append({key: checked[key] for key in ('check_id', 'change_id', 'official_id', 'status', 'reason')})
        require_lease(db, owner)
        monitor = db.get(MapSourceState, KEY)
        monitor.status = report['status']
        monitor.error = report.get('error')
        monitor.next_retry_at = started + timedelta(seconds=settings.store_collection_interval_sec)
        monitor.records = [report]
        if report['status'] == 'complete':
            monitor.last_success_at = datetime.now(timezone.utc)
        db.commit()
        return report
    except Exception:
        db.rollback()
        if db.scalar(select(MapJobLease.owner).where(MapJobLease.key == KEY,
                MapJobLease.owner == owner, MapJobLease.expires_at > datetime.now(timezone.utc))) == owner:
            monitor = db.get(MapSourceState, KEY)
            if monitor:
                monitor.status, monitor.error = 'error', 'place_revalidation_job_failed'
                db.commit()
        raise
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == KEY, MapJobLease.owner == owner))
        db.commit()


def job_status(db):
    monitor = db.get(MapSourceState, KEY)
    last_tick = aware(monitor.last_attempt_at) if monitor and monitor.last_attempt_at else None
    enabled = settings.store_place_revalidation_enabled and settings.store_collection_enabled
    states = db.scalars(select(MapSourceState).where(MapSourceState.source_kind == 'place_revalidation')
        .order_by(MapSourceState.next_retry_at, MapSourceState.source_key).limit(101)).all()
    return {'enabled': enabled, 'kakao_rest_api_configured': bool(settings.kakao_rest_api_key),
        'poll_seconds': settings.store_collection_interval_sec, 'batch_size': settings.store_place_revalidation_batch_size,
        'last_tick_at': last_tick, 'last_success_at': aware(monitor.last_success_at) if monitor and monitor.last_success_at else None,
        'stale': bool(enabled and (not last_tick or datetime.now(timezone.utc) - last_tick >
            timedelta(seconds=settings.store_collection_interval_sec * 3))),
        'status': monitor.status if monitor else 'never', 'error': monitor.error if monitor else None,
        'latest_run': monitor.records[0] if monitor and monitor.records else None,
        'retry_states': [{'key': row.source_key, 'status': row.status, 'error': row.error, 'failures': row.failures,
            'next_attempt_at': aware(row.next_retry_at) if row.next_retry_at else None} for row in states[:100]],
        'retry_states_truncated': len(states) > 100, 'map_publication': False}
