"""Validate cursor chains and immutable page bytes before retaining review evidence."""
import hashlib
import json
import re
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select
from app.models.issuer_merchant_capture import IssuerMerchantCapture
from app.services.issuer_merchant_matching import DIRECTORY_URL, LOOKUP_URL, parse_shinhan_directory, merchant_match
from app.services.map_verification import aware


def merchant_archive_root():
    from app.services.card_collection import ARCHIVE_ROOT
    return Path(ARCHIVE_ROOT)/'issuer-merchants'


def load_capture(manifest_path, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    path = Path(manifest_path).resolve()
    if path.stat().st_size > 100000:
        raise ValueError('issuer_manifest_too_large')
    try:
        manifest = json.loads(path.read_bytes())
        if (type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['issuer_key'] != 'shinhan'
                or manifest['source_url'] != DIRECTORY_URL or manifest['lookup_url'] != LOOKUP_URL
                or manifest['region'] != '서울 동대문구'
                or manifest['capture_method'] != 'anonymous_public_browser'
                or not isinstance(manifest['keyword'],str) or not 2 <= len(manifest['keyword']) <= 30
                or type(manifest['complete']) is not bool):
            raise ValueError('issuer_manifest_identity_unresolved')
        pages = manifest['pages']
        if not isinstance(pages,list) or not 1 <= len(pages) <= 10:
            raise ValueError('issuer_manifest_page_limit')
        expected_cursor, previous_time, query, done = '', None, None, False
        records, seen, raw_pages, cursor_history = [], {}, [], set()
        for index, page in enumerate(pages):
            if done or not re.fullmatch(r'page-\d{3}\.json',page['raw_ref']):
                raise ValueError('issuer_manifest_page_order_invalid')
            received = datetime.fromisoformat(page['received_at'])
            if (received.tzinfo is None or not now-timedelta(days=7) <= received <= now
                    or previous_time and received < previous_time or page['http_status'] != 200):
                raise ValueError('issuer_manifest_observation_invalid')
            request = page['request']
            public_query = {key:request[key] for key in ('mchtNm','siDo','siGunGu','areaCd','pntMctYn')}
            if (request['NXT_QY_KEY'] != expected_cursor or request['mchtNm'] != manifest['keyword']
                    or request['pntMctYn'] != 'N' or any(not isinstance(v,str) or not v for v in public_query.values())
                    or query is not None and query != public_query):
                raise ValueError('issuer_manifest_query_changed')
            source = (path.parent/page['raw_ref']).resolve()
            if not source.is_relative_to(path.parent) or source.stat().st_size > 2_000_000:
                raise ValueError('issuer_manifest_archive_invalid')
            raw = source.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != page['raw_sha256']:
                raise ValueError('issuer_manifest_integrity_failure')
            parsed = parse_shinhan_directory(raw)
            if not parsed['complete'] and not parsed['records']:
                raise ValueError('issuer_manifest_empty_continuation')
            for record in parsed['records']:
                old = seen.get(record['merchant_id'])
                if old and any(old[k] != record[k] for k in ('name','address','issuer_category')):
                    raise ValueError('issuer_manifest_merchant_conflict')
                if not old:
                    record = {**record, 'page_index':index, 'page_sha256':digest}
                    seen[record['merchant_id']] = record
                    records.append(record)
            done = parsed['complete']
            expected_cursor = json.loads(raw)['mbw_json'].get('nxtQyKey') or ''
            if not done and expected_cursor in cursor_history:
                raise ValueError('issuer_manifest_cursor_repeated')
            cursor_history.add(expected_cursor)
            previous_time, query = received, public_query
            raw_pages.append((raw, {'page_index':index, 'raw_sha256':digest,
                'received_at':received.isoformat(), 'request':{**public_query,'NXT_QY_KEY':request['NXT_QY_KEY']}}))
        if manifest['complete'] != done:
            raise ValueError('issuer_manifest_completeness_conflict')
    except (KeyError,TypeError,AttributeError,json.JSONDecodeError,UnicodeDecodeError):
        raise ValueError('issuer_manifest_schema_changed') from None
    return manifest, records, raw_pages, previous_time


def import_capture(db, manifest_path, *, archive_root, now=None):
    now = aware(now or datetime.now(timezone.utc))
    manifest, records, pages, received = load_capture(manifest_path,now=now)
    binding = {'issuer_key':'shinhan','keyword':manifest['keyword'],'region':manifest['region'],
        'complete':manifest['complete'],'pages':[metadata for _,metadata in pages]}
    digest = hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    existing = db.scalar(select(IssuerMerchantCapture).where(IssuerMerchantCapture.capture_hash==digest))
    root = Path(archive_root).resolve()
    stored_pages = []
    for raw, metadata in pages:
        ref = Path(metadata['raw_sha256'][:2])/metadata['raw_sha256']
        target = (root/ref).resolve()
        if not target.is_relative_to(root):
            raise ValueError('issuer_capture_archive_escape')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary,target)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        if hashlib.sha256(target.read_bytes()).hexdigest() != metadata['raw_sha256']:
            raise ValueError('issuer_capture_archive_integrity_failure')
        stored_pages.append({**metadata, 'raw_ref':ref.as_posix()})
    if existing:
        return existing
    row = IssuerMerchantCapture(capture_hash=digest,issuer_key='shinhan',keyword=manifest['keyword'],
        region=manifest['region'],received_at=received,imported_at=now,complete=manifest['complete'],
        records=records,pages=stored_pages)
    db.add(row)
    db.flush()
    return row


def capture_matches(db, capture, *, archive_root, now=None):
    from app.models.store import Store
    now = aware(now or datetime.now(timezone.utc))
    root = Path(archive_root).resolve()
    issue = None
    binding = {'issuer_key':capture.issuer_key,'keyword':capture.keyword,'region':capture.region,
        'complete':capture.complete,'pages':[{k:v for k,v in p.items() if k!='raw_ref'} for p in capture.pages]}
    if hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False).encode()).hexdigest() != capture.capture_hash:
        issue = 'capture_metadata_changed'
    if not now-timedelta(days=7) <= aware(capture.received_at) <= now:
        issue = 'capture_stale'
    derived, seen = [], {}
    for page in capture.pages:
        path = (root/page['raw_ref']).resolve()
        try:
            intact = path.is_relative_to(root) and hashlib.sha256(path.read_bytes()).hexdigest()==page['raw_sha256']
        except OSError:
            intact = False
        if not intact:
            issue = 'capture_archive_integrity_failure'
            break
        try:
            parsed = parse_shinhan_directory(path.read_bytes())
            for record in parsed['records']:
                if record['merchant_id'] not in seen:
                    seen[record['merchant_id']] = record
                    derived.append({**record,'page_index':page['page_index'],'page_sha256':page['raw_sha256']})
            at = datetime.fromisoformat(page['received_at'])
            if at.tzinfo is None or not now-timedelta(days=7) <= at <= now:
                issue = 'capture_stale'
        except (ValueError,KeyError,TypeError):
            issue = 'capture_derivation_invalid'
            break
    if not issue and derived != capture.records:
        issue = 'capture_records_changed'
    directory = {'issuer_key':'shinhan','scope':'general_card_merchants_not_point_merchants',
        'complete':capture.complete,'records':capture.records}
    matches = []
    for store in db.scalars(select(Store).where(Store.is_active.is_(True)).order_by(Store.id)):
        result = merchant_match(store,directory)
        if issue:
            result.update(status='pending',reason=issue,candidates=[])
        matches.append(result)
    return {'capture_id':capture.id,'capture_hash':capture.capture_hash,'keyword':capture.keyword,
        'region':capture.region,'complete_for_submitted_query_only':capture.complete,'issue':issue,
        'merchant_records':len(capture.records),'stores_checked':len(matches),
        'unique_exact_matches':sum(m['status']=='unique_exact_match' for m in matches),
        'capture_claim_verified':False,'public_approval':False,'matches':matches}
