"""Semantic store-directory changes and review acknowledgements, never publication."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update, delete, func

from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent, MapJobLease
from app.services.map_sync import acquire_lease
from app.services.map_verification import normalize_location_text

FIELDS = ('official_name', 'address', 'lat', 'lng', 'delivery_only')


def normalized_row(row):
    result = {'official_id': str(row['official_id'])}
    for field in FIELDS:
        value = row.get(field)
        if field == 'official_name':
            value = normalize_location_text(value)
        elif field == 'address':
            value = ' '.join((value or '').replace('서울특별시', '서울').replace('서울시', '서울').split()).casefold()
        elif field in ('lat', 'lng') and value is not None:
            value = float(value)
        result[field] = value
    return result


def indexed(records):
    result = {}
    for row in records:
        code = str(row['official_id'])
        if not code or code in result:
            raise ValueError('store_directory_duplicate_identity')
        result[code] = row
    return result


def identity_hash(records):
    rows = indexed(records)
    canonical = [normalized_row(rows[code]) for code in sorted(rows)]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compare_records(previous, current):
    before, after = indexed(previous), indexed(current)
    changes = []
    for code in sorted(before.keys() | after.keys()):
        if code not in before:
            changes.append({'official_id': code, 'kind': 'added', 'before': None, 'after': after[code]})
        elif code not in after:
            changes.append({'official_id': code, 'kind': 'missing_from_response', 'before': before[code], 'after': None})
        else:
            old, new = normalized_row(before[code]), normalized_row(after[code])
            fields = [field for field in FIELDS if old[field] != new[field]]
            if fields:
                changes.append({'official_id': code, 'kind': 'modified', 'fields': fields,
                    'before': before[code], 'after': after[code]})
    return changes


def make_change_event(state, source, rows, fingerprint, archive, observed_at, retained=False):
    if not state.last_success_at:
        return None  # Initial observations establish a baseline.
    changes = compare_records(state.records or [], rows)
    if not changes:
        return None
    return MapReviewEvent(kind='pipeline', item_id=0, action='store_directory_changed', at=observed_at,
        payload={'source_key': source['source_key'], 'source_url': source['url'], 'provider': source['provider'],
            'before_content_hash': state.content_hash, 'after_content_hash': fingerprint,
            'before_identity_hash': identity_hash(state.records or []), 'after_identity_hash': identity_hash(rows),
            'archive': archive, 'changes': changes, 'state_retained': retained,
            'attempted_at': aware(state.last_attempt_at).isoformat(),
            'missing_is_closure_evidence': False, 'map_publication': False, 'benefit_approval': False})


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def event_is_current(event, state, latest_change_id=None):
    if state is None:
        return False
    if latest_change_id is not None and event.id != latest_change_id:
        return False
    data = event.payload
    if data.get('state_retained'):
        return (state.status == 'error' and state.error == 'store_directory_disappeared_requires_review'
            and state.content_hash == data['before_content_hash'] and state.last_success_at is not None
            and aware(state.last_success_at) <= aware(event.at))
    return identity_hash(state.records or []) == data['after_identity_hash']


def repeated_retained_change(db, event, state):
    if not event or not event.payload.get('state_retained'):
        return False
    previous = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_changed',
        MapReviewEvent.payload['source_key'].as_string() == event.payload['source_key'])
        .order_by(MapReviewEvent.id.desc()).limit(1))
    fields = ('before_content_hash', 'after_content_hash', 'before_identity_hash', 'after_identity_hash', 'state_retained')
    return bool(previous and aware(state.last_success_at) <= aware(previous.at)
        and all(previous.payload.get(field) == event.payload.get(field) for field in fields))


def list_changes(db, limit=50, offset=0):
    events = db.scalars(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_changed')
        .order_by(MapReviewEvent.id.desc()).offset(offset).limit(limit)).all()
    ids = [event.id for event in events]
    reviews = db.scalars(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_reviewed',
        MapReviewEvent.payload['change_id'].as_integer().in_(ids)).order_by(MapReviewEvent.id)).all() if ids else []
    reviewed = {r.payload['change_id']: r for r in reviews}
    source_keys = {event.payload['source_key'] for event in events}
    source_field = MapReviewEvent.payload['source_key'].as_string()
    latest = dict(db.execute(select(source_field, func.max(MapReviewEvent.id))
        .where(MapReviewEvent.action == 'store_directory_changed', source_field.in_(source_keys))
        .group_by(source_field)).all()) if source_keys else {}
    result = []
    for event in events:
        state = db.get(MapSourceState, event.payload['source_key'])
        current = event_is_current(event, state, latest.get(event.payload['source_key']))
        review = reviewed.get(event.id)
        result.append({'id': event.id, 'observed_at': aware(event.at), **event.payload,
            'status': 'reviewed' if review else 'pending', 'current': current,
            'reviewable': current and review is None,
            'review': {'id': review.id, 'at': aware(review.at), 'note': review.payload['note']} if review else None})
    return {'items': result, 'limit': limit, 'offset': offset, 'map_publication': False}


def record_review(db, change_id, expected_identity_hash, note):
    note = note.strip()
    if not note or len(note) > 2000:
        raise ValueError('review_note_required')
    lock_key = 'store_directory_review:' + str(change_id)
    owner = acquire_lease(db, lock_key)
    if not owner:
        raise ValueError('store_directory_review_busy')
    try:
        event = db.get(MapReviewEvent, change_id)
        if not event or event.action != 'store_directory_changed':
            raise ValueError('store_directory_change_not_found')
        if event.payload['after_identity_hash'] != expected_identity_hash:
            raise ValueError('store_directory_review_version_mismatch')
        existing = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_reviewed',
            MapReviewEvent.payload['change_id'].as_integer() == change_id).order_by(MapReviewEvent.id))
        if existing:
            return {'status': 'already_reviewed', 'review_id': existing.id, 'map_publication': False}
        state = db.get(MapSourceState, event.payload['source_key'])
        latest = db.scalar(select(func.max(MapReviewEvent.id)).where(MapReviewEvent.action == 'store_directory_changed',
            MapReviewEvent.payload['source_key'].as_string() == event.payload['source_key']))
        if not event_is_current(event, state, latest):
            raise ValueError('store_directory_review_stale')
        try:
            evidence = Path(event.payload['archive']).read_bytes()
        except OSError:
            raise ValueError('store_directory_change_evidence_unavailable') from None
        if hashlib.sha256(evidence).hexdigest() != event.payload['after_content_hash']:
            raise ValueError('store_directory_change_evidence_invalid')
        # Serialize the acknowledgement with a concurrent collector. A no-op CAS
        # leaves the observation and its timestamps intact while locking the row.
        changed = db.execute(update(MapSourceState).where(MapSourceState.source_key == state.source_key,
            MapSourceState.content_hash == state.content_hash, MapSourceState.last_attempt_at == state.last_attempt_at)
            .values(content_hash=state.content_hash)).rowcount
        if not changed:
            raise ValueError('store_directory_review_stale')
        review = MapReviewEvent(kind='pipeline', item_id=0, action='store_directory_reviewed',
            at=datetime.now(timezone.utc), payload={'change_id': change_id,
                'source_key': state.source_key, 'identity_hash': expected_identity_hash,
                'note': note, 'map_publication': False, 'benefit_approval': False})
        db.add(review)
        db.commit()
        return {'status': 'reviewed', 'review_id': review.id, 'map_publication': False}
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == lock_key, MapJobLease.owner == owner))
        db.commit()
