"""Periodic official store observations, separate from map/benefit publication."""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapJobLease, MapReviewEvent
from app.scripts.probe_paris_stores import read_bounded_response, capture_paris
from app.services.paris_store_identity import paris_capture_rows
from app.scripts.probe_mega_stores import capture_mega
from app.services.mega_store_identity import mega_candidate
from app.scripts.probe_starbucks_stores import capture_starbucks
from app.scripts.discover_starbucks_store import official_candidate as starbucks_candidate
from playwright.sync_api import TimeoutError as BrowserTimeoutError, Error as BrowserError
from app.services.map_sync import acquire_lease
from app.services.outback_store_identity import SEARCH_SOURCE, parse_outback_search
from app.services.robots_policy import prefix_robots_allowed
from app.services.store_directory_review import make_change_event, repeated_retained_change

LEASE_KEY = 'store_directory_collection'
USER_AGENT = 'PayPickMapSync/1.0'


def configured_directories():
    configured = json.loads(settings.store_collection_sources_json)
    if not isinstance(configured, list) or len(configured) > 20:
        raise ValueError('invalid_store_directory_configuration')
    sources = []
    for item in configured:
        if (not isinstance(item, dict) or not isinstance(item.get('key'), str)
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', item['key'])):
            raise ValueError('invalid_store_directory_configuration')
        if item.get('provider') == 'outback':
            if (set(item) != {'key', 'provider', 'query'} or not isinstance(item['query'], str)
                    or not item['query'].strip() or len(item['query']) > 50
                    or not re.search(r'[가-힣A-Za-z0-9]', item['query'])):
                raise ValueError('invalid_store_directory_configuration')
            params = {'sdCd': '', 'sggCd': '', 'searchKeyword': item['query']}
            url, query = SEARCH_SOURCE + '?' + urlencode(params), item['query']
        elif item.get('provider') == 'paris':
            if (set(item) != {'key', 'provider', 'province', 'district'}
                    or item['province'] != '서울' or item['district'] != '동대문구'):
                raise ValueError('unsupported_store_directory_region')
            url, query = 'https://www.paris.co.kr/store', '서울 동대문구'
        elif item.get('provider') == 'mega':
            reviewed = {('경희', '경희대점', '732'), ('외대', '외대앞역점', '1409')}
            if (set(item) != {'key', 'provider', 'query', 'branch', 'official_id'}
                    or not all(isinstance(item.get(k), str) for k in ('query', 'branch', 'official_id'))
                    or (item['query'], item['branch'], item['official_id']) not in reviewed):
                raise ValueError('unsupported_store_directory_branch')
            query = item['query']
            url = 'https://www.mega-mgccoffee.com/store/find/store_search.php?' + urlencode({'store_search': query})
        elif item.get('provider') == 'starbucks':
            reviewed = {('경희대', '경희대', '9551'), ('회기', '회기역사거리', '9967')}
            if (set(item) != {'key', 'provider', 'query', 'official_name', 'official_id'}
                    or not all(isinstance(item.get(k), str) for k in ('query', 'official_name', 'official_id'))
                    or (item['query'], item['official_name'], item['official_id']) not in reviewed):
                raise ValueError('unsupported_store_directory_branch')
            query = item['query']
            url = 'https://www.starbucks.co.kr/store/store_map.do?in_biz_cd=' + item['official_id']
        else:
            raise ValueError('invalid_store_directory_configuration')
        sources.append({**item, 'query': query, 'source_key': 'store-directory:' + item['key'], 'url': url})
    if len({s['source_key'] for s in sources}) != len(sources):
        raise ValueError('duplicate_store_directory_key')
    return sources


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def fetch_directory(client, source):
    if source['provider'] == 'starbucks':
        base = 'https://www.starbucks.co.kr'
        robots = client.get(base + '/robots.txt')
        if robots.status_code == 404:
            text = ''
        else:
            robots.raise_for_status()
            text = robots.text
        if not all(prefix_robots_allowed(text, USER_AGENT, base + path)
                for path in ('/store/store_map.do', '/store/getStore.do')):
            raise ValueError('source_disallows_fetch')
        time.sleep(2)
        try:
            raw = capture_starbucks(source['query'], source['url'])
        except BrowserTimeoutError:
            raise ValueError('official_browser_timeout') from None
        except BrowserError:
            raise ValueError('official_browser_unavailable') from None
        observed = datetime.now(timezone.utc).isoformat()
        receipt = {'source_url': base + '/store/store_map.do', 'locator_url': source['url'],
            'query': source['query'], 'response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'captured_at': observed}
        candidate = starbucks_candidate(raw, receipt, source['official_id'], source['official_name'], datetime.now(timezone.utc))
        receipt.pop('captured_at')
        bundle = {'schema_version': 1, 'provider': 'starbucks', 'receipt': receipt, 'stores_json': raw}
        row = {field: candidate[field] for field in ('official_id', 'official_name', 'address', 'lat', 'lng')}
        return json.dumps(bundle, ensure_ascii=False, sort_keys=True), [row]
    if source['provider'] == 'mega':
        raw, search_raw, receipt = capture_mega(source['query'], source['branch'], client=client)
        candidate = mega_candidate(raw, search_raw, receipt, source['official_id'], source['branch'], datetime.now(timezone.utc))
        receipt.pop('captured_at')
        bundle = {'schema_version': 1, 'provider': 'mega', 'receipt': receipt,
            'positions_json': raw, 'search_html': search_raw}
        row = {field: candidate[field] for field in ('official_id', 'official_name', 'address', 'lat', 'lng')}
        return json.dumps(bundle, ensure_ascii=False, sort_keys=True), [row]
    if source['provider'] == 'paris':
        raw, receipt = capture_paris('', client=client, region=(source['province'], source['district']))
        area_raw = receipt.pop('area_response')
        rows = paris_capture_rows(raw, receipt, datetime.now(timezone.utc), area_raw)
        if any(not re.match(r'^(?:서울특별시|서울시|서울)\s+동대문구\s+', row['address']) for row in rows):
            raise ValueError('store_directory_region_response_mismatch')
        # Content-addressed evidence remains stable when only observation time
        # changes. The actual observation timestamp is stored in the DB event.
        receipt.pop('captured_at')
        bundle = {'schema_version': 1, 'provider': 'paris', 'receipt': receipt,
            'search_html': raw, 'areas_json': area_raw}
        return json.dumps(bundle, ensure_ascii=False, sort_keys=True), rows
    robots = client.get('https://m.outback.co.kr/robots.txt')
    if robots.status_code == 404:
        text = ''
    else:
        robots.raise_for_status()
        text = robots.text
    if not prefix_robots_allowed(text, USER_AGENT, source['url']):
        raise ValueError('source_disallows_fetch')
    time.sleep(2)
    with client.stream('GET', source['url']) as response:
        raw = read_bounded_response(response)
    return raw, parse_outback_search(raw)


def archive_response(raw):
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    root = Path(settings.card_source_archive_dir) / 'store-directories'
    root.mkdir(parents=True, exist_ok=True)
    target = root / (fingerprint + '.json')
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != fingerprint:
            raise ValueError('store_directory_archive_corrupt')
    else:
        import uuid
        temporary = root / (fingerprint + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(raw.encode())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return fingerprint, str(target)


def collect_store_directories(db, *, client=None, trigger='manual', force=False):
    sources = configured_directories()
    if not sources:
        return {'status': 'not_configured', 'sources': []}
    owner = acquire_lease(db, LEASE_KEY)
    if not owner:
        return {'status': 'busy', 'sources': []}
    own_client = client is None
    results = []
    try:
        if own_client:
            client = httpx.Client(trust_env=False, timeout=30, follow_redirects=False, headers={'User-Agent': USER_AGENT})
        for source in sources:
            now = datetime.now(timezone.utc)
            state = db.get(MapSourceState, source['source_key'])
            if state and not force and state.next_retry_at and aware(state.next_retry_at) > now:
                results.append({'key': source['source_key'], 'status': 'not_due'})
                continue
            renewed = db.execute(update(MapJobLease).where(MapJobLease.key == LEASE_KEY,
                MapJobLease.owner == owner).values(expires_at=now + timedelta(minutes=60))).rowcount
            if not renewed:
                raise RuntimeError('store_collection_lease_lost')
            if state is None:
                state = MapSourceState(source_key=source['source_key'], source_url=source['url'],
                    source_kind='store_directory', status='never', failures=0, records=[])
                db.add(state)
            state.last_attempt_at = now
            db.commit()  # Do not hold database writes during external requests.
            pending_change = None
            try:
                if state.source_url != source['url']:
                    raise ValueError('source_configuration_changed_requires_review')
                raw, rows = fetch_directory(client, source)
                fingerprint, archive = archive_response(raw)
                completed = datetime.now(timezone.utc)
                if db.scalar(select(MapJobLease.owner).where(MapJobLease.key == LEASE_KEY,
                        MapJobLease.owner == owner, MapJobLease.expires_at > completed)) != owner:
                    raise RuntimeError('store_collection_lease_lost')
                retained = bool(state.records and not rows)
                pending_change = make_change_event(state, source, rows, fingerprint, archive, completed, retained=retained)
                if repeated_retained_change(db, pending_change, state):
                    pending_change = None
                if retained:
                    raise ValueError('store_directory_disappeared_requires_review')
                if pending_change:
                    db.add(pending_change)
                unchanged = state.content_hash == fingerprint
                state.status, state.error, state.failures = 'unchanged' if unchanged else 'success', None, 0
                state.content_hash, state.records, state.last_success_at = fingerprint, rows, completed
                state.next_retry_at = completed + timedelta(hours=settings.store_collection_refresh_hours)
                db.add(MapReviewEvent(kind='pipeline', item_id=0, action='store_directory_observed', at=completed,
                    payload={'source_key': source['source_key'], 'source_url': source['url'], 'trigger': trigger,
                        'content_hash': fingerprint, 'archive': archive, 'records': len(rows),
                        'complete': False, 'map_publication': False, 'benefit_approval': False}))
                db.commit()
                results.append({'key': source['source_key'], 'status': state.status, 'records': len(rows)})
            except Exception as exc:
                db.rollback()
                if isinstance(exc, RuntimeError) and str(exc) == 'store_collection_lease_lost':
                    raise
                if db.scalar(select(MapJobLease.owner).where(MapJobLease.key == LEASE_KEY,
                        MapJobLease.owner == owner, MapJobLease.expires_at > datetime.now(timezone.utc))) != owner:
                    raise RuntimeError('store_collection_lease_lost') from None
                state = db.get(MapSourceState, source['source_key'])
                state.status, state.failures = 'error', (state.failures or 0) + 1
                known = {'source_disallows_fetch', 'store_directory_disappeared_requires_review',
                    'source_configuration_changed_requires_review', 'store_directory_archive_corrupt',
                    'official_response_too_large', 'official_search_unsuccessful',
                    'official_search_structure_changed', 'official_store_data_invalid',
                    'official_store_duplicate_identity', 'invalid_official_coordinates',
                    'store_directory_region_response_mismatch', 'official_search_incomplete_or_ambiguous',
                    'official_area_evidence_missing_or_changed', 'capture_region_mismatch',
                    'official_province_missing_or_ambiguous', 'official_district_missing_or_ambiguous'}
                known.update({'official_store_identity_mismatch', 'official_search_map_address_disagrees',
                    'official_search_address_disagrees', 'official_search_duplicate_branch',
                    'official_search_identity_missing_or_ambiguous', 'outside_review_region'})
                known.update({'official_browser_timeout', 'official_browser_unavailable'})
                state.error = str(exc) if isinstance(exc, ValueError) and str(exc) in known else 'store_directory_fetch_failed'
                delay = 86400 if state.error == 'source_disallows_fetch' else min(86400, 300 * 2 ** min(state.failures - 1, 8))
                state.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                if pending_change is not None and pending_change.payload.get('state_retained'):
                    db.add(pending_change)
                db.add(MapReviewEvent(kind='pipeline', item_id=0, action='store_directory_failed',
                    at=datetime.now(timezone.utc), payload={'source_key': source['source_key'], 'trigger': trigger,
                        'error': state.error, 'retained_records': len(state.records or []), 'map_publication': False}))
                db.commit()
                results.append({'key': source['source_key'], 'status': 'error', 'error': state.error})
        return {'status': 'complete', 'sources': results, 'map_publication': False}
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == LEASE_KEY, MapJobLease.owner == owner))
        db.commit()
        if own_client and client is not None:
            client.close()


def directory_status(db):
    result = []
    for source in configured_directories():
        state = db.get(MapSourceState, source['source_key'])
        result.append({'key': source['source_key'], 'provider': source['provider'], 'query': source['query'],
            'status': state.status if state else 'never',
            'last_attempt_at': aware(state.last_attempt_at) if state and state.last_attempt_at else None,
            'last_success_at': aware(state.last_success_at) if state and state.last_success_at else None,
            'next_attempt_at': aware(state.next_retry_at) if state and state.next_retry_at else None,
            'content_hash': state.content_hash if state else None,
            'records': state.records if state else [], 'error': state.error if state else None,
            'map_publication': False})
    return {'enabled': settings.store_collection_enabled, 'poll_seconds': settings.store_collection_interval_sec,
        'refresh_hours': settings.store_collection_refresh_hours, 'sources': result}
