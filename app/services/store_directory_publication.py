"""Explicit, version-bound application of reviewed changes to existing stores."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import IntegrityError

from app.models.map_source import MapSourceState
from app.models.map_verification import MapLocation, MapReviewEvent, MapJobLease
from app.models.store import Store
from app.services.map_sync import acquire_lease
from app.services.map_verification import digest, location_hash, location_address_key, normalize_location_text
from app.services.store_directory_review import aware
from app.services.store_directory_revalidation import ACTION, BRANDS, reviewed_change, candidate_for, validate_place

APPLY_ACTION = 'store_directory_applied'
LOCK = 'store_directory_apply'
STORE_BRANDS = {**BRANDS, 'outback': '아웃백'}


def snapshot(store, location):
    return {'brand': store.brand, 'branch': store.branch, 'address': store.address,
        'lat': store.lat, 'lng': store.lng, 'is_active': store.is_active,
        'location': {key: (aware(location.checked_at).isoformat() if key == 'checked_at' else getattr(location, key)) for key in
            ('identity_hash', 'place_id', 'lat', 'lng', 'source_url', 'checked_at', 'reviewer')} if location else None}


def branch_for(provider, row):
    branch = row['official_name'].strip()
    if provider in ('starbucks', 'paris') and not branch.endswith('점'):
        branch += '점'
    return branch


def check_binding(db, store, event, official_id):
    provider = event.payload['provider']
    if store.brand != STORE_BRANDS[provider] or not store.is_active:
        raise ValueError('store_directory_target_ineligible')
    bindings = db.scalars(select(MapReviewEvent).where(MapReviewEvent.kind == 'store',
        MapReviewEvent.action.in_(['official_store_discovery', APPLY_ACTION]))).all()
    bound_here = False
    for binding in bindings:
        data = binding.payload
        if data.get('provider') == provider and str(data.get('official_id')) == official_id:
            if binding.item_id != store.id:
                raise ValueError('official_id_already_bound')
            bound_here = True
        elif binding.item_id == store.id and data.get('provider') == provider:
            raise ValueError('store_bound_to_another_official_id')
    if bound_here:
        return
    # Legacy rows need an exact old official identity, never a fuzzy branch guess.
    delta = next(c for c in event.payload['changes'] if c['official_id'] == official_id)
    before = delta['before']
    if (not before or normalize_location_text(store.branch) != normalize_location_text(branch_for(provider, before))
            or location_address_key(store.address) != location_address_key(before['address'])):
        raise ValueError('store_directory_official_binding_required')


def publication_preview(db, change_id, check_id, store_id, expected_hash):
    event, state, review = reviewed_change(db, change_id, expected_hash)
    check = db.get(MapReviewEvent, check_id)
    if not check or check.action != ACTION or check.payload.get('change_id') != change_id:
        raise ValueError('store_directory_place_check_not_found')
    data = check.payload
    official_id = data['official_id']
    latest = db.scalar(select(func.max(MapReviewEvent.id)).where(MapReviewEvent.action == ACTION,
        MapReviewEvent.payload['change_id'].as_integer() == change_id,
        MapReviewEvent.payload['official_id'].as_string() == official_id))
    if (latest != check_id or data.get('status') != 'matched' or data.get('review_id') != review.id
            or data.get('identity_hash') != expected_hash or data.get('source_key') != state.source_key):
        raise ValueError('store_directory_place_check_not_current')
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(data['lookup_started_at'])
    if (started.tzinfo is None or not timedelta(0) <= now - started <= timedelta(hours=24)
            or not started <= aware(check.at) <= now):
        raise ValueError('store_directory_place_check_expired')
    candidate = candidate_for(event, official_id)
    if data.get('candidate') != candidate or validate_place(candidate, data['documents']) != data.get('place'):
        raise ValueError('store_directory_place_evidence_invalid')
    store = db.get(Store, store_id)
    if not store:
        raise ValueError('store_directory_target_not_found')
    check_binding(db, store, event, official_id)
    place = data['place']
    conflict = db.scalar(select(MapLocation).where(MapLocation.kind == 'store',
        MapLocation.place_id == place['place_id'], MapLocation.item_id != store_id))
    if conflict:
        raise ValueError('place_already_bound')
    branch = branch_for(event.payload['provider'], next(c['after'] for c in event.payload['changes']
        if c['official_id'] == official_id))
    if db.scalar(select(Store.id).where(Store.brand == store.brand, Store.branch == branch, Store.id != store_id)):
        raise ValueError('store_directory_branch_conflict')
    before = snapshot(store, db.get(MapLocation, ('store', store_id)))
    return {'change_id': change_id, 'check_id': check_id, 'store_id': store_id,
        'official_id': official_id, 'provider': event.payload['provider'], 'identity_hash': expected_hash,
        'expected_store_version': digest(before), 'before': before,
        'after': {'brand': store.brand, 'branch': branch, 'address': candidate['address'],
            'lat': place['lat'], 'lng': place['lng'], 'place': place},
        'map_publication': False, 'benefit_approval': False}


def apply_publication(db, change_id, check_id, store_id, expected_hash, expected_store_version, reviewer, note):
    reviewer, note = reviewer.strip(), note.strip()
    if not reviewer or len(reviewer) > 100 or not note or len(note) > 2000:
        raise ValueError('store_directory_publication_note_required')
    owner = acquire_lease(db, LOCK)
    if not owner:
        raise ValueError('store_directory_publication_busy')
    try:
        previous = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == APPLY_ACTION,
            MapReviewEvent.item_id == store_id, MapReviewEvent.payload['check_id'].as_integer() == check_id,
            MapReviewEvent.payload['change_id'].as_integer() == change_id))
        if previous:
            if (previous.payload['expected_store_version'] != expected_store_version
                    or previous.payload['identity_hash'] != expected_hash):
                raise ValueError('store_directory_target_version_mismatch')
            return {'status': 'already_applied', 'event_id': previous.id, 'store_id': store_id,
                'benefit_approval': False}
        if not db.execute(update(MapJobLease).where(MapJobLease.key == LOCK, MapJobLease.owner == owner,
                MapJobLease.expires_at > datetime.now(timezone.utc)).values(owner=owner)).rowcount:
            raise ValueError('store_directory_publication_busy')
        preview = publication_preview(db, change_id, check_id, store_id, expected_hash)
        if preview['expected_store_version'] != expected_store_version:
            raise ValueError('store_directory_target_version_mismatch')
        state = db.get(MapSourceState, db.get(MapReviewEvent, change_id).payload['source_key'])
        if not db.execute(update(MapSourceState).where(MapSourceState.source_key == state.source_key,
                MapSourceState.content_hash == state.content_hash, MapSourceState.last_attempt_at == state.last_attempt_at)
                .values(content_hash=state.content_hash)).rowcount:
            raise ValueError('store_directory_review_stale')
        # Lock and reread the target and pin so another editor cannot be overwritten.
        db.scalar(select(Store).where(Store.id == store_id).with_for_update())
        db.scalar(select(MapLocation).where(MapLocation.kind == 'store', MapLocation.item_id == store_id).with_for_update())
        db.expire_all()
        # Recheck latest lookup, source version, binding and duplicate-place guards after locks.
        preview = publication_preview(db, change_id, check_id, store_id, expected_hash)
        if preview['expected_store_version'] != expected_store_version:
            raise ValueError('store_directory_target_version_mismatch')
        store = db.get(Store, store_id)
        after = preview['after']
        for field in ('branch', 'address', 'lat', 'lng'):
            setattr(store, field, after[field])
        location = db.get(MapLocation, ('store', store_id)) or MapLocation(kind='store', item_id=store_id)
        for field in ('place_id', 'source_url', 'lat', 'lng'):
            setattr(location, field, after['place'][field])
        check = db.get(MapReviewEvent, check_id)
        location.identity_hash = location_hash(store)
        location.checked_at = aware(check.at)  # Applying evidence does not renew its lookup time.
        location.reviewer = reviewer
        db.add(location)
        event = MapReviewEvent(kind='store', item_id=store_id, action=APPLY_ACTION,
            at=datetime.now(timezone.utc), payload={**preview, 'after': snapshot(store, location),
                'reviewer': reviewer, 'note': note, 'map_publication': True, 'benefit_approval': False})
        db.add(event)
        db.commit()
        return {'status': 'applied', 'event_id': event.id, 'store_id': store_id,
            'map_publication': True, 'benefit_approval': False}
    except IntegrityError:
        raise ValueError('store_directory_concurrent_conflict') from None
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == LOCK, MapJobLease.owner == owner))
        db.commit()
