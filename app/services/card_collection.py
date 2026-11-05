"""Official source registration, conservative collection and content-addressed archive.

This stage does not create approved CardBenefit rows or infer benefit periods.
"""
import hashlib
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select, or_, func

from app.core.config import settings
from app.models.card_collection import (
    CardIssuer, CardCollectionSource, CardProductSource, CardSourceSnapshot,
    CardCollectionRun, CardCollectionAttempt,
)

ARCHIVE_ROOT = Path(settings.card_source_archive_dir).resolve()
# Host and query approval is explicit. Payment networks are not inferred issuers.
OFFICIAL_HOSTS = {
    'www.kakaobank.com': set(), 'kakaobank.com': set(),
    'www.tossbank.com': set(), 'tossbank.com': set(),
    'api.tossbank.com': set(),
    'card.kbcard.com': {'cooperationcode', 'mainCC', 'categoryCode', 'pageNo', 'searchwrd'},
    'www.shinhancard.com': set(),
    'www.hanacard.co.kr': {'CD_PD_SEQ', 'cateType'},
    'm.hanacard.co.kr': {'CD_PD_SEQ', 'cateType'},
    'www.bccard.com': {'gdsno'},
    'www.samsungcard.com': {'active'},
    'www.wooricard.com': {'CARD_CDOE', 'title', '_frame'},
}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def canonical_url(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in OFFICIAL_HOSTS or parts.username
            or parts.password or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('unapproved_card_source')
    query = parse_qsl(parts.query, keep_blank_values=True)
    if parts.hostname=='api.tossbank.com':
        from app.services.card_linked_documents import DOCUMENT_URLS
        if urlunsplit(('https',parts.hostname,parts.path,'','')) not in DOCUMENT_URLS:
            raise ValueError('unapproved_card_api_path')
    if len(query) != len({key for key, _ in query}) or not {key for key, _ in query} <= OFFICIAL_HOSTS[parts.hostname]:
        raise ValueError('unapproved_card_query')
    if any(len(value) > 200 or re.search(r'[\x00-\x20]', value) for _, value in query):
        raise ValueError('invalid_card_query')
    if re.search(r'[\x00-\x20\\]', parts.path):
        raise ValueError('invalid_card_path')
    return urlunsplit(('https', parts.hostname, parts.path or '/', urlencode(sorted(query)), ''))


def register_source(db, url, *, provider_key, kind='detail', issuer=None, now=None):
    now = now or datetime.now(timezone.utc)
    url = canonical_url(url)
    if kind not in {'listing', 'detail', 'benefits', 'notice', 'leaflet', 'event'}:
        raise ValueError('invalid_card_source_kind')
    issuer_row = None
    if issuer:
        key, name = issuer
        issuer_row = db.scalar(select(CardIssuer).where(CardIssuer.issuer_key == key))
        if issuer_row is None:
            issuer_row = CardIssuer(issuer_key=key, name=name)
            db.add(issuer_row)
            db.flush()
    source_key = hashlib.sha256(url.encode()).hexdigest()
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.source_key == source_key))
    if source is None:
        source = CardCollectionSource(source_key=source_key, canonical_url=url, source_kind=kind,
            provider_key=provider_key, issuer_id=issuer_row.id if issuer_row else None,
            next_check_at=now, schedule_policy={'interval_hours': 24})
        db.add(source)
        db.flush()
    elif issuer_row and source.issuer_id not in (None, issuer_row.id):
        raise ValueError('card_source_issuer_conflict')
    elif issuer_row:
        source.issuer_id = issuer_row.id
    return source


def bootstrap_supporting_documents(db, now=None):
    """Audited product-specific leaflet; no new product or approval is created."""
    source = register_source(db, 'https://m.hanacard.co.kr/leaflet/15/15439_20250626.pdf',
        provider_key='hanacard', kind='leaflet', issuer=('hana', '하나카드'), now=now)
    if not source.last_attempt_at:
        source.schedule_policy = {**(source.schedule_policy or {}), 'interval_hours': 168}
    from app.services.shinhan_disclosure import DEEP_OIL_DISCLOSURE_URL
    shinhan = register_source(db, DEEP_OIL_DISCLOSURE_URL, provider_key='shinhan', kind='leaflet',
        issuer=('shinhan', '신한카드'), now=now)
    if not shinhan.last_attempt_at:
        shinhan.schedule_policy = {**(shinhan.schedule_policy or {}), 'interval_hours': 168}
    from app.services.card_linked_documents import DOCUMENTS
    linked=[]
    for code,spec in DOCUMENTS.items():
        document=register_source(db,spec['url'],provider_key='tossbank',kind='leaflet',
            issuer=('tossbank','토스뱅크'),now=now)
        if not document.last_attempt_at:
            document.schedule_policy={**(document.schedule_policy or {}),'interval_hours':168,
                'parent_source_url':TOSS_KPASS_URL,'document_identifier':code,'document_role':spec['role']}
        linked.append(document.id)
    return {'registered_documents': 4, 'source_ids': [source.id, shinhan.id,*linked], 'auto_approved': 0}


def register_product(db, source, *, namespace, code, name, product_type, now=None, card_id=None):
    now = now or datetime.now(timezone.utc)
    row = db.scalar(select(CardProductSource).where(CardProductSource.source_namespace == namespace,
                                                  CardProductSource.external_product_code == code))
    if row is None:
        row = CardProductSource(source_id=source.id, issuer_id=source.issuer_id,
            source_namespace=namespace, external_product_code=code, product_name=name,
            product_type=product_type, card_id=card_id, first_seen_at=now, last_seen_at=now)
        db.add(row)
    else:
        if row.source_id != source.id or (card_id is not None and row.card_id not in (None, card_id)):
            raise ValueError('card_product_identity_conflict')
        if card_id is not None:
            row.card_id = card_id
        row.last_seen_at = now
    db.flush()
    return row


TOSS_KPASS_URL = 'https://www.tossbank.com/card/k-pass'


def collection_error_code(exc):
    """Persist safe cause categories, never exception URLs or proxy credentials."""
    if isinstance(exc,httpx.HTTPStatusError):
        stage='robots' if exc.request.url.path=='/robots.txt' else 'source'
        if exc.response.is_redirect:
            return stage+'_redirect_requires_configuration'
        return stage+'_http_'+str(exc.response.status_code)
    cause=exc
    seen=set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause,OSError) and (getattr(cause,'winerror',None)==10013 or cause.errno in (13,10013)):
            return 'network_access_denied'
        cause=cause.__cause__ or cause.__context__
    if isinstance(exc,httpx.ProxyError): return 'proxy_connection_failed'
    if isinstance(exc,httpx.TimeoutException): return 'network_timeout'
    if isinstance(exc,httpx.ConnectError): return 'network_connection_failed'
    if isinstance(exc,ValueError) and re.fullmatch('[a-z_]{1,100}',str(exc)): return str(exc)
    return type(exc).__name__


def extract_document(content, *, source_url=None):
    if not content or len(content) > settings.map_source_max_bytes:
        raise ValueError('invalid_card_document_size')
    if content.startswith(b'%PDF'):
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content))
        if len(reader.pages) > 100:
            raise ValueError('card_document_too_long')
        text = ' '.join(page.extract_text() or '' for page in reader.pages)
        mime = 'application/pdf'
    else:
        soup = BeautifulSoup(content, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'noscript']):
            tag.decompose()
        if source_url == TOSS_KPASS_URL:
            # This audited page puts most product sections beside, rather than
            # inside, its 77-character hero <main>. Do not generalize to all sites.
            main = soup.select_one('#root')
            if not main or '토스뱅크 K-패스 체크카드' not in main.get_text(' ',strip=True):
                raise ValueError('toss_kpass_layout_changed')
        else:
            main = soup.select_one('main, #main_contents, #contents, article')
        text = (main or soup).get_text(' ', strip=True)
        mime = 'text/html'
    text = re.sub(r'\s+', ' ', text).strip()
    # A readable document is still only a candidate, never an approved benefit.
    if len(text) < 100 or len(text) > 500000:
        raise ValueError('incomplete_card_document')
    if any(marker in text.lower() for marker in ('access denied', 'captcha', '접근이 차단')):
        raise ValueError('blocked_card_document')
    return text, mime


def capture_document(db, source, content, *, now=None, archive_root=None):
    now = now or datetime.now(timezone.utc)
    text, mime = extract_document(content,source_url=source.canonical_url)
    raw_hash = hashlib.sha256(content).hexdigest()
    snapshot = db.scalar(select(CardSourceSnapshot).where(CardSourceSnapshot.source_id == source.id,
                                                        CardSourceSnapshot.raw_sha256 == raw_hash))
    # Object paths depend only on SHA-256, never on an external file name or URL.
    root = Path(archive_root or ARCHIVE_ROOT)
    relative = Path(raw_hash[:2]) / raw_hash
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != raw_hash:
            raise ValueError('card_archive_integrity_failure')
    else:
        # Readers must never see a partially written object after a crash.
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temp_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink()
    if snapshot is None:
        snapshot = CardSourceSnapshot(source_id=source.id, fetched_at=now, raw_sha256=raw_hash,
            semantic_sha256=hashlib.sha256(text.encode()).hexdigest(),
            raw_object_ref=relative.as_posix(), extracted_text=text, mime_type=mime,
            parser_version='toss_kpass_root_v1' if source.canonical_url==TOSS_KPASS_URL else 'card_archive_v1', completeness='text_available')
        db.add(snapshot)
        db.flush()
    source.last_full_fetch_at = now
    return snapshot


def next_check(source, now):
    policy = source.schedule_policy or {}
    hours = int(policy.get('interval_hours', 24))
    if not 1 <= hours <= 168:
        raise ValueError('invalid_card_schedule')
    # End time must come from reviewed period evidence, not guesses from page dates.
    end = policy.get('reviewed_end_at')
    if end:
        end = datetime.fromisoformat(end)
        if end.tzinfo is None:
            raise ValueError('card_schedule_timezone_required')
        if timedelta(0) <= end - now <= timedelta(days=7):
            hours = min(hours, 6)
    if policy.get('early_end_possible'):
        hours = min(hours, 6)
    return now + timedelta(hours=hours)


def capture_existing_official(db, source, item, content, *, archive_root=None):
    """Archive an already configured card source without changing its approval."""
    registered = register_source(db, source['url'], provider_key=urlsplit(source['url']).hostname)
    register_product(db, registered, namespace='legacy_card_key', code=item.card.card_key,
                     name=item.card.card_name, product_type='unclassified', card_id=item.card_id)
    snapshot = capture_document(db, registered, content, archive_root=archive_root)
    now = datetime.now(timezone.utc)
    registered.last_attempt_at = registered.last_success_at = now
    registered.next_check_at = next_check(registered, now)
    registered.failure_count, registered.next_retry_at, registered.last_error = 0, None, None
    db.add(CardCollectionAttempt(source_id=registered.id, snapshot_id=snapshot.id,
                                attempted_at=now, status='captured', http_status=200))
    return snapshot


# Audited initial candidates; this is not issuer-wide automatic discovery.
CANDIDATES = (
    ('kakaobank', 'checkcard', '프렌즈 체크카드', 'check', 'https://www.kakaobank.com/products/checkcard', ('kakaobank', '카카오뱅크')),
    ('kakaobank', 'thecheckcard', '더(The) 체크카드', 'check', 'https://www.kakaobank.com/products/thecheckcard', ('kakaobank', '카카오뱅크')),
    ('kakaobank', 'moimcheckcard', '모임 체크카드', 'check', 'https://www.kakaobank.com/products/moimcheckcard', ('kakaobank', '카카오뱅크')),
    ('kakaobank', 'chakboot', '착붙 신한카드', 'credit', 'https://kakaobank.com/products/chakboot', ('shinhan', '신한카드')),
    ('kakaobank', 'plcc', '줍줍 신한카드', 'credit', 'https://kakaobank.com/products/plcc', ('shinhan', '신한카드')),
    ('tossbank', 'check-card', '토스뱅크 체크카드', 'check', 'https://www.tossbank.com/product-service/card/check-card', ('tossbank', '토스뱅크')),
    ('tossbank', 'k-pass', '토스뱅크 K-패스 체크카드', 'check', 'https://www.tossbank.com/card/k-pass', ('tossbank', '토스뱅크')),
    ('hanacard', '18669', '토스뱅크 하나카드 Day', 'credit', 'https://www.hanacard.co.kr/OPI41000000D.web?CD_PD_SEQ=18669', ('hana', '하나카드')),
)


def bootstrap_candidates(db):
    for namespace, code, name, kind, url, issuer in CANDIDATES:
        source = register_source(db, url, provider_key=namespace, issuer=issuer)
        register_product(db, source, namespace=namespace, code=code, name=name, product_type=kind)
    register_source(db, 'https://www.tossbank.com/card/benefits', provider_key='tossbank', kind='benefits',
                    issuer=('tossbank', '토스뱅크'))
    db.flush()
    return {'registered_candidates': len(CANDIDATES), 'auto_approved': 0}


def collect_due(db, *, client=None, now=None, archive_root=None, request_interval=2,
                trigger='manual', max_sources=20, only_source_ids=None):
    """Collect due documents and listing candidates, sharing the map worker lease."""
    from app.services.map_sync import acquire_lease, fetch_source
    from app.models.map_verification import MapJobLease
    from sqlalchemy import delete, update
    from types import SimpleNamespace

    at = now or datetime.now(timezone.utc)
    if trigger not in {'manual', 'scheduled', 'test'} or not 1 <= max_sources <= 100:
        raise ValueError('invalid_card_collection_run')
    if only_source_ids is not None and (not only_source_ids or len(only_source_ids)>100
            or any(type(value) is not int or value<=0 for value in only_source_ids)):
        raise ValueError('invalid_card_source_filter')
    # Query due work first: empty five-minute ticks must not create empty run history.
    due = select(CardCollectionSource.id).where(CardCollectionSource.enabled.is_(True),
        or_(CardCollectionSource.next_check_at.is_(None), CardCollectionSource.next_check_at <= at),
        or_(CardCollectionSource.next_retry_at.is_(None), CardCollectionSource.next_retry_at <= at))
    if only_source_ids is not None:
        due = due.where(CardCollectionSource.id.in_(only_source_ids))
    source_ids = list(db.scalars(due.order_by(CardCollectionSource.next_check_at, CardCollectionSource.id)
        .limit(max_sources)))
    total = db.scalar(select(func.count()).select_from(CardCollectionSource).where(CardCollectionSource.enabled.is_(True)))
    if not source_ids:
        return {'status': 'idle', 'captured': 0, 'unchanged': 0, 'failed': 0, 'skipped': total,
                'new_products': 0, 'observed_products': 0, 'auto_approved': 0}
    lease = acquire_lease(db)
    if not lease:
        return {'status': 'busy'}
    owned = client is None
    last_requests = {}
    def throttle(request):
        host = request.url.host
        delay = request_interval - (time.monotonic() - last_requests.get(host, 0))
        if delay > 0:
            time.sleep(delay)
        last_requests[host] = time.monotonic()
    client = client or httpx.Client(timeout=settings.map_source_timeout_sec, follow_redirects=False,
        headers={'User-Agent': 'PayPickMapSync/1.0'}, event_hooks={'request': [throttle]})
    run_id = str(uuid.uuid4())
    counts = {'captured': 0, 'unchanged': 0, 'failed': 0, 'skipped': total - len(source_ids),
              'new_products': 0, 'observed_products': 0, 'auto_approved': 0,
              'parsed_versions': 0, 'extraction_failures': 0}
    try:
        at = now or datetime.now(timezone.utc)
        db.add(CardCollectionRun(id=run_id, trigger=trigger, started_at=at, status='running'))
        db.commit()
        for source_id in source_ids:
            at = now or datetime.now(timezone.utc)
            source = db.get(CardCollectionSource, source_id)
            if any(value and utc(value) > at for value in (source.next_check_at, source.next_retry_at)):
                counts['skipped'] += 1
                continue
            renewed = db.execute(update(MapJobLease).where(MapJobLease.key == 'map_sync', MapJobLease.owner == lease,
                MapJobLease.expires_at > datetime.now(timezone.utc)).values(expires_at=datetime.now(timezone.utc) + timedelta(minutes=60)))
            if not renewed.rowcount:
                raise RuntimeError('card_collection_lease_lost')
            source.last_attempt_at = at
            db.commit()
            http_status = None
            snapshot_id = None
            try:
                url = canonical_url(source.canonical_url)
                # Immutable object IDs do not describe the latest HTTP observation:
                # a source can revert, and manual imports share this archive.
                from app.models.card_document_import import CardDocumentImport
                observed = db.scalar(select(CardCollectionAttempt).where(
                    CardCollectionAttempt.source_id == source.id,
                    CardCollectionAttempt.snapshot_id.is_not(None),
                    CardCollectionAttempt.status.in_(['captured', 'unchanged']),
                    CardCollectionAttempt.http_status.in_([200, 304]))
                    .order_by(CardCollectionAttempt.id.desc()).limit(1))
                previous = db.get(CardSourceSnapshot, observed.snapshot_id) if observed else db.scalar(
                    select(CardSourceSnapshot).where(CardSourceSnapshot.source_id == source.id,
                        ~select(CardDocumentImport.id).where(
                            CardDocumentImport.snapshot_id == CardSourceSnapshot.id).exists())
                    .order_by(CardSourceSnapshot.id.desc()).limit(1))
                if previous is not None and previous.source_id != source.id:
                    raise ValueError('card_snapshot_identity_failure')
                cache = SimpleNamespace(etag=source.etag, last_modified=source.last_modified,
                                        content_hash=previous.raw_sha256 if previous else None)
                # Daily full body fetch; a 304 is never evidence of fresh parsing.
                force_full = previous is None or bool(source.last_error) or not source.last_full_fetch_at or at - utc(source.last_full_fetch_at) >= timedelta(hours=24)
                from app.services.shinhan_disclosure import DEEP_OIL_DISCLOSURE_URL, fetch_deep_oil_leaflet
                if url == DEEP_OIL_DISCLOSURE_URL and source.source_kind == 'leaflet':
                    body, headers = fetch_deep_oil_leaflet(client)
                else:
                    body, headers = fetch_source(client, {'url': url}, cache, force=force_full)
                http_status = 304 if body is None else 200
                if body is None and force_full:
                    raise ValueError('full_card_document_required')
                snapshot = capture_document(db, source, body, now=at, archive_root=archive_root) if body is not None else previous
                snapshot_id = snapshot.id
                discovery = None
                if source.source_kind == 'listing':
                    # Preserve the observed document even if its listing parser fails.
                    # Candidate mutations below still roll back together on failure.
                    db.commit()
                    # A cached response may reuse the original object, never invented markup.
                    if body is None:
                        body = (Path(archive_root or ARCHIVE_ROOT) / snapshot.raw_object_ref).read_bytes()
                        if hashlib.sha256(body).hexdigest() != snapshot.raw_sha256:
                            raise ValueError('card_archive_integrity_failure')
                    from app.services.card_discovery import apply_discovery
                    discovery = apply_discovery(db, source, snapshot, body, at)
                from app.services.card_benefit_extraction import extract_snapshot
                if http_status != 304:
                    from app.services.card_document_crosscheck import prepare_document_text
                    prepare_document_text(db, source, snapshot, body, now=at)
                extraction = extract_snapshot(db, source, snapshot, now=at) if http_status != 304 else None
                status = 'unchanged' if http_status == 304 else 'captured'
                source.etag = headers.get('etag') if http_status != 304 else headers.get('etag', source.etag)
                source.last_modified = headers.get('last-modified') if http_status != 304 else headers.get('last-modified', source.last_modified)
                source.last_success_at = at
                source.failure_count, source.next_retry_at, source.last_error = 0, None, None
                source.next_check_at = next_check(source, at)
                db.add(CardCollectionAttempt(run_id=run_id, source_id=source.id, snapshot_id=snapshot.id,
                    attempted_at=at, status=status, http_status=http_status))
                db.commit()
                counts[status] += 1
                if extraction:
                    counts['parsed_versions'] += int(extraction.get('created', False))
                    counts['extraction_failures'] += int(extraction['status'] == 'failed')
                if discovery:
                    counts['new_products'] += discovery['new_products']
                    counts['observed_products'] += discovery['observed_products']
            except Exception as exc:
                db.rollback()
                source = db.get(CardCollectionSource, source_id)
                source.last_attempt_at = at
                source.failure_count += 1
                source.next_retry_at = at + timedelta(seconds=min(86400, 60 * 2 ** min(source.failure_count, 10)))
                source.next_check_at = source.next_retry_at
                source.last_error = collection_error_code(exc)
                if source.last_error == 'source_disallows_fetch':
                    source.next_retry_at = source.next_check_at = at + timedelta(days=1)
                if isinstance(exc, httpx.HTTPStatusError):
                    http_status = exc.response.status_code
                    retry = exc.response.headers.get('retry-after', '')
                    if retry.isdigit():
                        retry_at = at + timedelta(seconds=min(int(retry), 31536000))
                    else:
                        try:
                            retry_at = parsedate_to_datetime(retry)
                            if retry_at.tzinfo is None:
                                retry_at = None
                        except (ValueError, TypeError, OverflowError):
                            retry_at = None
                    if retry_at:
                        source.next_retry_at = max(utc(source.next_retry_at), retry_at)
                        source.next_check_at = source.next_retry_at
                db.add(CardCollectionAttempt(run_id=run_id, source_id=source_id, attempted_at=at,
                    snapshot_id=snapshot_id if snapshot_id and db.get(CardSourceSnapshot, snapshot_id) else None,
                    status='failed', http_status=http_status, error_code=source.last_error))
                db.commit()
                counts['failed'] += 1
        run = db.get(CardCollectionRun, run_id)
        run.status = 'degraded' if counts['failed'] or counts['extraction_failures'] else 'complete'
        run.finished_at, run.counts = now or datetime.now(timezone.utc), counts
        db.commit()
        return {'run_id': run_id, 'status': run.status, **counts}
    except BaseException:
        db.rollback()
        run = db.get(CardCollectionRun, run_id)
        if run:
            run.status, run.finished_at, run.counts = 'failed', datetime.now(timezone.utc), counts
            db.commit()
        raise
    finally:
        db.rollback()
        db.execute(delete(MapJobLease).where(MapJobLease.key == 'map_sync', MapJobLease.owner == lease))
        db.commit()
        if owned:
            client.close()
