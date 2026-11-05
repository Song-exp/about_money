"""Version-bound place checks for acknowledged official-directory changes."""
import hashlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select, func, update, delete

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent, MapJobLease
from app.services.map_sync import acquire_lease
from app.services.map_verification import normalize_location_text, location_address_key
from app.services.store_directory_review import aware, event_is_current

ACTION = 'store_directory_place_check'
BRANDS = {'starbucks': '스타벅스', 'mega': '메가MGC커피',
          'paris': '파리바게뜨', 'outback': '아웃백스테이크하우스'}


def reviewed_change(db, change_id, expected_hash):
    db.expire_all()
    event = db.get(MapReviewEvent, change_id)
    if not event or event.action != 'store_directory_changed':
        raise ValueError('store_directory_change_not_found')
    if event.payload['after_identity_hash'] != expected_hash:
        raise ValueError('store_directory_review_version_mismatch')
    state = db.get(MapSourceState, event.payload['source_key'])
    latest = db.scalar(select(func.max(MapReviewEvent.id)).where(
        MapReviewEvent.action == 'store_directory_changed',
        MapReviewEvent.payload['source_key'].as_string() == event.payload['source_key']))
    if not event_is_current(event, state, latest):
        raise ValueError('store_directory_review_stale')
    review = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_reviewed',
        MapReviewEvent.payload['change_id'].as_integer() == change_id).order_by(MapReviewEvent.id))
    if not review or review.payload['identity_hash'] != expected_hash:
        raise ValueError('store_directory_review_required')
    try:
        evidence = Path(event.payload['archive']).read_bytes()
    except OSError:
        raise ValueError('store_directory_change_evidence_unavailable') from None
    if hashlib.sha256(evidence).hexdigest() != event.payload['after_content_hash']:
        raise ValueError('store_directory_change_evidence_invalid')
    if (not state.last_success_at or not timedelta(0) <= datetime.now(timezone.utc)
            - aware(state.last_success_at) <= timedelta(days=7)):
        raise ValueError('store_directory_observation_not_recent')
    return event, state, review


def candidate_for(event, official_id):
    changes = [c for c in event.payload['changes'] if c['official_id'] == official_id]
    if len(changes) != 1:
        raise ValueError('store_directory_identity_not_changed')
    row = changes[0]['after']
    if row is None:
        raise ValueError('store_directory_missing_not_place_evidence')
    provider = event.payload['provider']
    if provider not in BRANDS:
        raise ValueError('unsupported_store_provider')
    if (provider == 'outback' and row.get('delivery_only') not in (False, 'N')) or row.get('delivery_only') in (True, 'Y'):
        raise ValueError('store_directory_not_confirmed_walk_in')
    branch = row['official_name'].strip()
    if not branch:
        raise ValueError('invalid_official_name')
    if provider in ('starbucks', 'paris') and not branch.endswith('점'):
        branch += '점'
    address = row['address']
    if not address.startswith(('서울특별시 동대문구 ', '서울시 동대문구 ', '서울 동대문구 ')):
        raise ValueError('outside_review_region')
    lat, lng = float(row['lat']), float(row['lng'])
    if not (math.isfinite(lat) and math.isfinite(lng) and 37 <= lat <= 38 and 126 <= lng <= 128):
        raise ValueError('invalid_official_coordinates')
    return {'official_id': official_id, 'name': f'{BRANDS[provider]} {branch}',
            'address': address, 'lat': lat, 'lng': lng}


def validate_place(candidate, documents):
    for doc in documents:
        if (not isinstance(doc, dict) or any(not isinstance(doc.get(key), str)
                for key in ('place_name', 'id', 'place_url', 'x', 'y'))
                or not isinstance(doc.get('road_address_name', ''), str)
                or not isinstance(doc.get('address_name', ''), str)):
            raise ValueError('invalid_place_response')
    matches = [d for d in documents if normalize_location_text(d['place_name']) == normalize_location_text(candidate['name'])
        and location_address_key(d.get('road_address_name') or d.get('address_name', '')) == location_address_key(candidate['address'])]
    if len(matches) != 1:
        raise ValueError('no_unique_exact_place')
    match = matches[0]
    code = str(match['id'])
    url = match['place_url'].replace('http://', 'https://', 1)
    if not code.isdigit() or url != f'https://place.map.kakao.com/{code}':
        raise ValueError('invalid_place_source')
    lat, lng = float(match['y']), float(match['x'])
    if not (math.isfinite(lat) and math.isfinite(lng) and 37 <= lat <= 38 and 126 <= lng <= 128):
        raise ValueError('invalid_place_coordinates')
    if abs(lat - candidate['lat']) > .002 or abs(lng - candidate['lng']) > .002:
        raise ValueError('official_place_coordinates_disagree')
    return {'place_id': code, 'source_url': url, 'matched_name': match['place_name'],
        'matched_address': match.get('road_address_name') or match['address_name'], 'lat': lat, 'lng': lng}


def lookup_place(candidate, client):
    query = f"{candidate['name']} {candidate['address']}"
    # One bounded request; never fall back to fuzzy matching or follow redirects.
    with client.stream('GET', 'https://dapi.kakao.com/v2/local/search/keyword.json',
            params={'query': query, 'size': 15, 'page': 1},
            headers={'Authorization': f'KakaoAK {settings.kakao_rest_api_key}'},
            follow_redirects=False, timeout=15) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise ValueError('place_response_too_large')
    import json
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('documents'), list):
        raise ValueError('invalid_place_response')
    if not isinstance(data.get('meta'), dict):
        raise ValueError('invalid_place_response')
    if data['meta'].get('is_end') is not True:
        raise ValueError('place_results_incomplete')
    documents = data['documents']
    if len(documents) > 15 or any(not isinstance(d, dict) for d in documents):
        raise ValueError('invalid_place_response')
    return query, documents, hashlib.sha256(raw).hexdigest()


def revalidate_change(db, change_id, official_id, expected_hash, client=None):
    lock_key = f'directory_place:{change_id}'
    owner = acquire_lease(db, lock_key)
    if not owner:
        raise ValueError('store_directory_place_check_busy')
    try:
        event, state, review = reviewed_change(db, change_id, expected_hash)
        candidate = candidate_for(event, official_id)
        if not settings.kakao_rest_api_key:
            raise ValueError('kakao_rest_api_key_required')
        # Release the read transaction during network I/O; check the version again afterwards.
        state_hash, attempted_at, review_id = state.content_hash, state.last_attempt_at, review.id
        db.rollback()
        result = {'status': 'pending', 'reason': None, 'candidate': candidate,
            'change_id': change_id, 'review_id': review_id, 'official_id': official_id,
            'identity_hash': expected_hash, 'map_publication': False, 'benefit_approval': False}
        started_at = datetime.now(timezone.utc)
        def perform(active_client):
            query, documents, fingerprint = lookup_place(candidate, active_client)
            result.update(query=query, documents=documents, response_sha256=fingerprint)
            result['place'] = validate_place(candidate, documents)
            result['status'] = 'matched'
        try:
            if client is None:
                with httpx.Client() as active_client:
                    perform(active_client)
            else:
                perform(client)
        except httpx.HTTPError:
            result['reason'] = 'place_lookup_failed'
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            result['reason'] = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                'no_unique_exact_place', 'invalid_place_source', 'invalid_place_coordinates',
                'official_place_coordinates_disagree', 'place_response_too_large',
                'invalid_place_response', 'place_results_incomplete'} else 'invalid_place_response'
        event, state, review = reviewed_change(db, change_id, expected_hash)
        held = db.execute(update(MapJobLease).where(MapJobLease.key == lock_key, MapJobLease.owner == owner,
            MapJobLease.expires_at > datetime.now(timezone.utc)).values(owner=owner)).rowcount
        if not held:
            raise ValueError('store_directory_place_check_busy')
        if state.content_hash != state_hash or state.last_attempt_at != attempted_at:
            raise ValueError('store_directory_review_stale')
        changed = db.execute(update(MapSourceState).where(MapSourceState.source_key == state.source_key,
            MapSourceState.content_hash == state_hash, MapSourceState.last_attempt_at == attempted_at)
            .values(content_hash=state_hash)).rowcount
        if not changed:
            raise ValueError('store_directory_review_stale')
        result.update(source_key=state.source_key, lookup_started_at=started_at.isoformat())
        check = MapReviewEvent(kind='pipeline', item_id=0, action=ACTION,
            at=datetime.now(timezone.utc), payload=result)
        db.add(check)
        db.commit()
        return {'check_id': check.id, **result}
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == lock_key, MapJobLease.owner == owner))
        db.commit()


def list_checks(db, change_id, limit=50, offset=0):
    return [{'check_id': r.id, 'at': aware(r.at), **r.payload} for r in db.scalars(
        select(MapReviewEvent).where(MapReviewEvent.action == ACTION,
            MapReviewEvent.payload['change_id'].as_integer() == change_id)
        .order_by(MapReviewEvent.id.desc()).offset(offset).limit(limit))]
