"""Public source polling and authenticated feed pushes; never invent promotions."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from threading import Lock
from urllib.parse import urlsplit, parse_qs
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy import update, delete
from sqlalchemy.exc import IntegrityError
import uuid

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.local_benefit import LocalBenefitMerchant
from app.models.store import Store, StoreCategory, StoreOffer, PayMethod

GEOJI_HOST = 'xn--v69ak0xskm.com'
USER_AGENT = 'PayPickMapSync/1.0'
sync_lock = Lock()


def safe_url(url, host=None):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
        raise ValueError('invalid_source_url')
    if host and p.hostname != host:
        raise ValueError('source_host_mismatch')
    return url


def configured_sources(db=None):
    sources = [{'key': 'geojimap:dongdaemun', 'url': safe_url(settings.map_geojimap_listing_url, GEOJI_HOST), 'kind': 'menu_price'}]
    feeds = json.loads(settings.map_promotion_sources_json)
    if not isinstance(feeds, list) or len(feeds) > 30:
        raise ValueError('invalid_feed_configuration')
    for f in feeds:
        if not isinstance(f, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', f.get('key', '')):
            raise ValueError('invalid_feed_configuration')
        sources.append({'key': 'promotion:' + f['key'], 'url': safe_url(f['url']), 'kind': 'simple_pay_promotion'})
    if len({s['key'] for s in sources}) != len(sources):
        raise ValueError('duplicate_source_configuration')
    official = json.loads(settings.map_official_sources_json)
    if db is not None:
        from app.services.map_card_pipeline import policy_sources
        existing = {(row['kind'], row['item_id']): row['url'] for row in official}
        for row in policy_sources(db):
            key = (row['kind'], row['item_id'])
            if key in existing and existing[key] != row['url']:
                raise ValueError('official_adapter_source_conflict')
            if key not in existing:
                official.append(row)
    if not isinstance(official, list) or len(official) > 100:
        raise ValueError('invalid_official_sources')
    allowed = {'www.shinhancard.com', 'card.kbcard.com', 'www.hanacard.co.kr', 'm.hanacard.co.kr', 'www.samsungcard.com', 'www.wooricard.com'}
    for row in official:
        parsed = urlsplit(row['url'])
        safe_url(parsed._replace(query='').geturl())
        if not set(parse_qs(parsed.query)) <= {'cooperationcode', 'mainCC', 'categoryCode', 'CD_PD_SEQ', 'CARD_CDOE', 'title', '_frame'}:
            raise ValueError('invalid_official_query')
        url = row['url']
        if urlsplit(url).hostname not in allowed or row.get('kind') not in ('card', 'offer', 'local') or type(row.get('item_id')) is not int:
            raise ValueError('invalid_official_source')
        sources.append({'key': f"official:{row['kind']}:{row['item_id']}", 'url': url,
                        'kind': 'official_evidence', 'target_kind': row['kind'], 'item_id': row['item_id']})
    if len({s['key'] for s in sources}) != len(sources):
        raise ValueError('duplicate_source_configuration')
    return sources


def capture_official(db, source, content):
    from app.models.map_verification import MapEvidence
    from app.services.map_verification import MODELS, item_hash, item_snapshot
    item = db.get(MODELS[source['target_kind']], source['item_id'])
    if not item: raise ValueError('official_target_missing')
    if content.startswith(b'%PDF'):
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content))
        if len(reader.pages) > 100: raise ValueError('official_document_too_long')
        text = ' '.join(page.extract_text() or '' for page in reader.pages)
    else:
        soup = BeautifulSoup(content, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer']): tag.decompose()
        text = soup.get_text(' ', strip=True)
    if len(text) < 100 or len(text) > 500000: raise ValueError('incomplete_official_document')
    if source['target_kind'] == 'card':
        from app.services.card_collection import capture_existing_official
        capture_existing_official(db, source, item, content)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    row = db.get(MapEvidence, (source['target_kind'], item.id))
    now = datetime.now(timezone.utc)
    if row is None:
        row = MapEvidence(kind=source['target_kind'], item_id=item.id)
        db.add(row)
    changed = row.content_hash != fingerprint or row.item_hash != item_hash(item) or row.source_url != source['url']
    if changed:
        row.status = 'revoked' if row.status == 'revoked' else 'pending'
        row.verified_at, row.reviewer = None, None
        row.reason = 'official_source_changed_or_first_capture'
        row.recheck_at = now
    row.source_url, row.provider, row.source_text = source['url'], urlsplit(source['url']).hostname, text
    row.content_hash, row.item_hash, row.fetched_at = fingerprint, item_hash(item), now
    if changed:
        from app.models.map_verification import MapReviewEvent
        db.add(MapReviewEvent(kind=row.kind, item_id=row.item_id, action='capture', at=now,
               payload={'content_hash': fingerprint, 'item_hash': row.item_hash, 'source_url': row.source_url,
                        'source_text': text, 'item': item_snapshot(item)}))
    return [{'kind': row.kind, 'id': row.item_id, 'content_hash': fingerprint}]


def acquire_lease(db, key='map_sync'):
    from app.models.map_verification import MapJobLease
    owner, now = uuid.uuid4().hex, datetime.now(timezone.utc)
    expires = now + timedelta(minutes=60)
    try:
        row = db.get(MapJobLease, key)
        if row is None:
            db.add(MapJobLease(key=key, owner=owner, expires_at=expires))
            db.commit()
            return owner
        changed = db.execute(update(MapJobLease).where(MapJobLease.key == key, MapJobLease.expires_at < now).values(owner=owner, expires_at=expires).execution_options(synchronize_session=False)).rowcount
        db.commit()
        return owner if changed else None
    except IntegrityError:
        db.rollback()
        return None


def geojimap_records(html, source_url):
    soup = BeautifulSoup(html, 'html.parser')
    collections = []
    for tag in soup.find_all('script', type='application/ld+json'):
        value = json.loads(tag.get_text())
        for item in value if isinstance(value, list) else [value]:
            if item.get('@type') == 'CollectionPage' and item.get('mainEntity', {}).get('@type') == 'ItemList':
                collections.append(item['mainEntity'])
    if len(collections) != 1:
        raise ValueError('listing_structure_changed')
    listing = collections[0]
    items = listing.get('itemListElement')
    if not isinstance(items, list) or not 0 < len(items) <= 200 or listing.get('numberOfItems') != len(items):
        raise ValueError('incomplete_listing')
    links = {a.get('href'): a.get_text(' ', strip=True) for a in soup.find_all('a', href=True)}
    result = []
    for row in items:
        item = row['item']
        url = safe_url(item['url'], GEOJI_HOST)
        path = urlsplit(url).path
        match = re.search(r'([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})/?$', path)
        name = item.get('name', '').strip()
        address = item.get('address', {}).get('streetAddress', '').strip()
        price = re.fullmatch(r'([0-9][0-9,]*)원', item.get('priceRange', ''))
        text = links.get(path) or links.get(url)
        parts = text.split(' · ') if text else []
        if item.get('@type') != 'Restaurant' or not match or not name or len(name) > 200 or not address or len(address) > 500 or not price or len(parts) != 3 or parts[0] != name:
            raise ValueError('unverified_restaurant_record')
        amount = int(price[1].replace(',', ''))
        if amount <= 0 or amount > 1_000_000 or parts[-1] != item['priceRange']:
            raise ValueError('invalid_menu_price')
        geo = item.get('geo') or {}
        lat, lng = geo.get('latitude'), geo.get('longitude')
        if (lat is None) != (lng is None) or (lat is not None and (not math.isfinite(float(lat)) or not math.isfinite(float(lng)) or not -90 <= float(lat) <= 90 or not -180 <= float(lng) <= 180)):
            raise ValueError('invalid_coordinates')
        result.append({'key': 'geojimap:' + match[1], 'name': name, 'address': address, 'menu': parts[1],
                       'price': amount, 'url': url, 'lat': float(lat) if lat is not None else None,
                       'lng': float(lng) if lng is not None else None})
    if len({r['key'] for r in result}) != len(result):
        raise ValueError('duplicate_restaurant')
    return result


class Promotion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    brand: str = Field(min_length=1, max_length=100)
    branch: str = Field(min_length=1, max_length=150)
    address: str = Field(min_length=1, max_length=300)
    category: StoreCategory
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    pay_method: PayMethod
    discount_rate: int = Field(ge=1, le=100, strict=True)
    condition_text: str = Field(min_length=1, max_length=300)
    valid_from: date
    valid_to: date
    source_url: str

    @model_validator(mode='after')
    def period(self):
        if self.valid_to < self.valid_from:
            raise ValueError('invalid_promotion_period')
        safe_url(self.source_url)
        return self


def promotion_records(body, source_url):
    data = json.loads(body)
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != 1 or type(data.get('complete')) is not bool or not isinstance(data.get('items'), list) or len(data['items']) > 5000:
        raise ValueError('invalid_promotion_feed')
    rows = [Promotion.model_validate(r).model_dump(mode='json') for r in data['items']]
    host = urlsplit(source_url).hostname
    for r in rows:
        safe_url(r['source_url'], host)
    if len({r['id'] for r in rows}) != len(rows) or len({(r['brand'], r['branch'], r['pay_method']) for r in rows}) != len(rows):
        raise ValueError('duplicate_promotion')
    return rows, data['complete']


def apply_records(db, state, records, complete):
    previous_count = len(state.records or [])
    if complete and previous_count >= 10 and len(records) < previous_count / 2:
        raise ValueError('source_record_count_dropped_requires_review')
    if state.source_kind == 'menu_price':
        seen = set()
        for r in records:
            row = db.scalar(select(LocalBenefitMerchant).where(LocalBenefitMerchant.source_key == r['key']))
            if row is None:
                row = LocalBenefitMerchant(source_key=r['key'])
                db.add(row)
            values = {'merchant_name': r['name'], 'address': r['address'], 'search_query': r['name'] + ' ' + r['address'],
                      'category': StoreCategory.RESTAURANT, 'category_label': '가성비 식당', 'source_type': 'geojimap',
                      'programs': ['geojimap'], 'credentials': [], 'requires': '등록 가격이며 실제 매장 가격을 확인해 주세요.',
                      'benefit_text': r['menu'] + ' · ' + format(r['price'], ',') + '원', 'discount_type': 'menu_price',
                      'discount_value': r['price'], 'conditions': '거지맵 제보 가격 · 할인 프로모션 여부는 별도 확인',
                      'source_url': r['url'], 'confidence': 'community_reported', 'is_active': True}
            # Do not discard separately verified coordinates when source has none.
            if r['lat'] is not None:
                values.update(lat=r['lat'], lng=r['lng'])
            for k, v in values.items():
                setattr(row, k, v)
            seen.add(r['key'])
            db.flush()
        if complete:
            previous = {r['key'] for r in state.records or []}
            for row in db.scalars(select(LocalBenefitMerchant).where(LocalBenefitMerchant.source_key.in_(previous-seen))).all():
                row.is_active = False
    else:
        previous = {r['id']: r for r in state.records or []}
        ownership = {r.get('offer_id') for other in db.scalars(select(MapSourceState).where(MapSourceState.source_key != state.source_key)).all() for r in other.records or []}
        seen = set()
        for r in records:
            store = db.scalar(select(Store).where(Store.brand == r['brand'], Store.branch == r['branch']))
            if store is None:
                store = Store(brand=r['brand'], branch=r['branch'], address=r['address'], category=StoreCategory(r['category']), lat=r['lat'], lng=r['lng'])
                db.add(store)
                db.flush()
            offer = db.scalar(select(StoreOffer).where(StoreOffer.store_id == store.id, StoreOffer.pay_method == PayMethod(r['pay_method'])))
            if offer is not None and (offer.id in ownership or (not offer.is_sample and offer.id not in {v.get('offer_id') for v in previous.values()})):
                raise ValueError('promotion_source_conflict')
            if offer is None:
                offer = StoreOffer(store_id=store.id, pay_method=PayMethod(r['pay_method']))
                db.add(offer)
            offer.discount_rate, offer.condition_text = r['discount_rate'], r['condition_text']
            offer.valid_from, offer.valid_to = date.fromisoformat(r['valid_from']), date.fromisoformat(r['valid_to'])
            offer.is_active, offer.is_sample = True, False
            db.flush()
            r['offer_id'] = offer.id
            seen.add(offer.id)
        if complete:
            for offer in db.scalars(select(StoreOffer).where(StoreOffer.id.in_({r.get('offer_id') for r in previous.values()}-seen))).all():
                offer.is_active = False
        else:
            records.extend(r for key, r in previous.items() if key not in {v['id'] for v in records})
    state.records = records


def utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def fetch_source(client, source, state, force=False):
    url = source['url']
    root = urlsplit(url)
    robots_url = f'{root.scheme}://{root.netloc}/robots.txt'
    robots = client.get(robots_url)
    if robots.status_code != 404:
        robots.raise_for_status()
        policy = RobotFileParser()
        policy.parse(robots.text.splitlines())
        if not policy.can_fetch(USER_AGENT, url):
            raise ValueError('source_disallows_fetch')
    headers = {}
    if state.etag and not force:
        headers['If-None-Match'] = state.etag
    if state.last_modified and not force:
        headers['If-Modified-Since'] = state.last_modified
    # Redirects are deliberately not followed into unconfigured hosts.
    with client.stream('GET', url, headers=headers) as response:
        if response.status_code == 304:
            if not state.content_hash:
                raise ValueError('invalid_not_modified_without_cache')
            return None, response.headers
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError('source_redirect_requires_configuration')
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > settings.map_source_max_bytes:
                raise ValueError('source_too_large')
        return bytes(data), response.headers


def sync_sources(db, *, client=None, force=False, trigger='manual'):
    if not sync_lock.acquire(blocking=False):
        return {'status': 'busy', 'sources': []}
    owned = client is None
    results = []
    fetched_documents = {}
    lease = None
    started = None
    outcome = 'failed'
    try:
        lease = acquire_lease(db)
        if not lease: return {'status': 'busy', 'sources': []}
        from app.models.map_verification import MapReviewEvent
        started = datetime.now(timezone.utc)
        db.add(MapReviewEvent(kind='pipeline', item_id=0, action='pipeline_started', at=started,
            payload={'run_id': lease, 'trigger': trigger}))
        db.commit()
        client = client or httpx.Client(timeout=settings.map_source_timeout_sec, follow_redirects=False, headers={'User-Agent': USER_AGENT})
        for source in configured_sources(db):
            now = datetime.now(timezone.utc)
            from app.models.map_verification import MapJobLease
            renewed = db.execute(update(MapJobLease).where(MapJobLease.key == 'map_sync', MapJobLease.owner == lease).values(expires_at=now + timedelta(minutes=60))).rowcount
            db.commit()
            if not renewed: raise RuntimeError('map_worker_lease_lost')
            state = db.get(MapSourceState, source['key'])
            if state is not None and not force and state.next_retry_at and utc(state.next_retry_at) > now:
                results.append({'key': source['key'], 'status': 'backoff'})
                continue
            if state is None:
                state = MapSourceState(source_key=source['key'], source_url=source['url'], source_kind=source['kind'])
                db.add(state)
                db.flush()
            state.last_attempt_at = now
            # Release DB writes while waiting for the external source.
            db.commit()
            content = None
            try:
                from app.services.map_card_pipeline import needs_full_validation, publish_card
                validation = None
                must_validate = needs_full_validation(db, source)
                # URL changes must not reuse another endpoint's cache validators.
                if state.source_url != source['url']:
                    state.etag = state.last_modified = state.content_hash = None
                cached = fetched_documents.get(source['url'])
                if cached is not None:
                    content, headers = cached
                else:
                    content, headers = fetch_source(client, source, state, force=force or must_validate)
                    if content is not None:
                        fetched_documents[source['url']] = (content, headers)
                if content is None and must_validate:
                    raise ValueError('full_document_required_for_revalidation')
                if content is not None:
                    if source['kind'] == 'official_evidence':
                        state.records = capture_official(db, source, content)
                        validation = publish_card(db, source, content)
                    elif source['kind'] == 'menu_price':
                        rows, complete = geojimap_records(content, source['url']), True
                        apply_records(db, state, rows, complete)
                    else:
                        rows, complete = promotion_records(content, source['url'])
                        apply_records(db, state, rows, complete)
                    state.content_hash = hashlib.sha256(content).hexdigest()
                    state.etag, state.last_modified = headers.get('etag'), headers.get('last-modified')
                state.source_url = source['url']
                state.last_success_at = datetime.now(timezone.utc)
                state.status, state.error, state.failures, state.next_retry_at = ('unchanged' if content is None else 'success'), None, 0, None
                if source['kind'] == 'official_evidence' and validation is None:
                    from app.models.map_verification import MapEvidence
                    from app.services.map_verification import MODELS, evidence_status
                    evidence = db.get(MapEvidence, (source['target_kind'], source['item_id']))
                    item = db.get(MODELS[source['target_kind']], source['item_id'])
                    validation = {'status': evidence_status(evidence, item), 'reason': 'cached_source_no_renewal'}
                db.commit()
            except Exception as exc:
                db.rollback()
                if content is not None and source['kind'] == 'official_evidence':
                    # A newly observed document that failed validation must not
                    # leave the previous published version eligible.
                    from app.models.map_verification import MapEvidence
                    evidence = db.get(MapEvidence, (source['target_kind'], source['item_id']))
                    if evidence and evidence.status != 'revoked':
                        evidence.status, evidence.verified_at = 'pending', None
                        evidence.reason = 'new_document_validation_failed'
                state = db.get(MapSourceState, source['key'])
                state.failures += 1
                state.status, state.error = 'error', type(exc).__name__
                if type(exc) is ValueError and re.fullmatch(r'[a-z_]{1,100}', str(exc)):
                    state.error = str(exc)
                state.next_retry_at = now + timedelta(seconds=min(86400, 60 * 2**min(state.failures, 10)))
                db.commit()
            results.append({'key': source['key'], 'status': state.status, 'records': len(state.records or []),
                            'error': state.error, 'validation': validation})
        if any(r['status'] in ('error', 'backoff') for r in results):
            outcome = 'degraded'
        elif any((r.get('validation') or {}).get('status') in ('pending', 'manual_review', 'changed', 'recheck_required', 'unverified') for r in results):
            outcome = 'needs_review'
        else:
            outcome = 'success'
        return {'status': 'complete', 'outcome': outcome, 'run_id': lease, 'sources': results}
    finally:
        if lease:
            # Durable execution history includes failures; no automatic external messages.
            db.rollback()
            finished = datetime.now(timezone.utc)
            db.add(MapReviewEvent(kind='pipeline', item_id=0, action='pipeline_finished', at=finished,
                payload={'run_id': lease, 'trigger': trigger, 'started_at': started.isoformat(),
                         'finished_at': finished.isoformat(), 'outcome': outcome, 'sources': results}))
            from app.models.map_verification import MapJobLease
            db.execute(delete(MapJobLease).where(MapJobLease.key == 'map_sync', MapJobLease.owner == lease))
            db.commit()
        if owned and client is not None:
            client.close()
        sync_lock.release()


def push_promotions(db, key, payload):
    if not sync_lock.acquire(blocking=False):
        raise ValueError('sync_busy')
    lease = None
    try:
        lease = acquire_lease(db)
        if not lease: raise ValueError('sync_busy')
        source = next((s for s in configured_sources() if s['key'] == key and s['kind'] == 'simple_pay_promotion'), None)
        if source is None:
            raise ValueError('source_not_configured')
        rows, complete = promotion_records(json.dumps(payload), source['url'])
        state = db.get(MapSourceState, key)
        if state is None:
            state = MapSourceState(source_key=key, source_url=source['url'], source_kind=source['kind'])
            db.add(state)
        state.source_url = source['url']
        apply_records(db, state, rows, complete)
        state.last_attempt_at = state.last_success_at = datetime.now(timezone.utc)
        state.content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        state.status, state.error, state.failures, state.next_retry_at = 'success', None, 0, None
        state.etag = state.last_modified = None
        db.commit()
        return {'key': key, 'status': state.status, 'records': len(state.records)}
    except Exception:
        db.rollback()
        raise
    finally:
        if lease:
            from app.models.map_verification import MapJobLease
            db.execute(delete(MapJobLease).where(MapJobLease.key == 'map_sync', MapJobLease.owner == lease))
            db.commit()
        sync_lock.release()


def source_health(db):
    from sqlalchemy import func
    from app.models.map_verification import MapReviewEvent, MapLocation
    states = {s.source_key: s for s in db.scalars(select(MapSourceState)).all()}
    now = datetime.now(timezone.utc)
    result = []
    for source in configured_sources(db):
        state = states.get(source['key'])
        age = (now-utc(state.last_success_at)).total_seconds() if state and state.last_success_at else None
        result.append({'key': source['key'], 'source_url': source['url'], 'kind': source['kind'],
                       'status': state.status if state else 'never', 'last_success_at': utc(state.last_success_at).isoformat() if state and state.last_success_at else None,
                       'stale': age is None or age > max(60, settings.map_sync_interval_sec)*2 or bool(state and state.status == 'error'),
                       'records': len(state.records or []) if state else 0, 'error': state.error if state else None})
    from app.models.map_verification import MapEvidence
    from app.services.map_verification import today_kst, aware
    overdue = sum(aware(row.recheck_at) <= now for row in db.scalars(select(MapEvidence)).all())
    revision = f"{db.scalar(select(func.max(MapReviewEvent.id))) or 0}:{db.scalar(select(func.max(MapLocation.checked_at))) or ''}:{today_kst()}:{overdue}"
    latest = db.scalar(select(MapReviewEvent).where(MapReviewEvent.kind == 'pipeline',
        MapReviewEvent.action == 'pipeline_finished').order_by(MapReviewEvent.id.desc()).limit(1))
    return {'enabled': settings.map_sync_enabled, 'revision': revision, 'poll_interval_seconds': max(60, settings.map_sync_interval_sec),
            'automatic_validation_enabled': settings.map_auto_validate_enabled,
            'latest_pipeline_run': latest.payload if latest else None,
            'promotion_sources_configured': sum(s['kind'] == 'simple_pay_promotion' for s in result), 'sources': result}
