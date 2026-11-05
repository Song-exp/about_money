"""Complete short roads using an existing, still-bound exact-place observation."""
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.models.map_verification import MapLocation, MapReviewEvent
from app.services.map_verification import aware, location_hash, normalize_location_text, digest


def address_completion(db, store, *, now=None, apply=False):
    now = aware(now or datetime.now(timezone.utc))
    result = {'store_id':store.id,'name':f'{store.brand} {store.branch}','before':store.address,
        'status':'pending','reason':None,'after':None}
    old = store.address or ''
    if not store.is_active:
        result['reason']='inactive_store'
        return result
    if re.match(r'^(?:서울특별시|서울시|서울)\s+[가-힣]+구\s+',old):
        result.update(status='unchanged',reason='address_already_qualified')
        return result
    if not re.fullmatch(r'[가-힣A-Za-z0-9·]+(?:로|길)\s+\d+(?:-\d+)?',old):
        result['reason']='short_road_identity_missing'
        return result
    location = db.get(MapLocation,('store',store.id))
    if (not location or location.identity_hash != location_hash(store)
            or location.reviewer != 'exact_name_address_match'
            or not re.fullmatch(r'\d{1,30}',location.place_id or '')
            or location.source_url != f'https://place.map.kakao.com/{location.place_id}'):
        result['reason']='current_exact_place_evidence_missing'
        return result
    if not now-timedelta(days=7) <= aware(location.checked_at) <= now:
        result['reason']='place_observation_stale'
        return result
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.kind=='store',
        MapReviewEvent.item_id==store.id,MapReviewEvent.action.in_(['location_match','location_review']))
        .order_by(MapReviewEvent.id.desc()).limit(1))
    payload = event.payload if event else {}
    expected = f"{payload.get('region','')} {old}".strip()
    address = payload.get('address')
    if (not event or event.action!='location_match' or aware(event.at)!=aware(location.checked_at)
            or payload.get('place_id')!=location.place_id or payload.get('source_address')!=old
            or normalize_location_text(payload.get('name'))!=normalize_location_text(result['name'])
            or not re.fullmatch(r'서울\s+[가-힣]+구',payload.get('region') or '')
            or not address or normalize_location_text(expected)!=normalize_location_text(address)
            or normalize_location_text(payload.get('qualified_address'))!=normalize_location_text(address)):
        result['reason']='place_observation_identity_mismatch'
        return result
    result.update(status='ready',reason=None,after=address,source_event_id=event.id,
        source_url=location.source_url,original_checked_at=aware(location.checked_at).isoformat())
    if apply:
        before_hash = location_hash(store)
        store.address = address
        # Only the address representation changes. The observation date and
        # coordinates remain bound to the original exact-place evidence.
        location.identity_hash = location_hash(store)
        db.add(MapReviewEvent(kind='store',item_id=store.id,action='address_review',at=now,
            payload={'before':{'address':old,'identity_hash':before_hash},
                'after':{'address':address,'identity_hash':location.identity_hash},
                'source_url':location.source_url,'source_event_id':event.id,
                'source_hash':digest(payload),'original_checked_at':aware(location.checked_at).isoformat(),
                'reviewer':'existing_exact_place_address_completion','official_name':payload['name'],
                'official_address':address,'new_external_observation':False}))
        result['status']='address_completed'
    return result
