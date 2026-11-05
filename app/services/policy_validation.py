"""Policy API ingestion queue, bounded validation batches and recommendation gate."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json

from sqlalchemy import select

from app.crawlers.base import RawPosting
from app.models.scholarship import Scholarship, PostingStatus
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services.posting_validation import POLICY_VERSION, candidate_fingerprint, validate_posting
from app.services.scholarship_history import record_revision


def snapshot_fingerprint(row):
    value = {'candidate': candidate_fingerprint(row), 'title': row.title, 'organization': row.organization,
             'url': row.source_url, 'period': row.eligibility.get('application_period'),
             'semantic_source': row.eligibility.get('policy_source', {}).get('semantic_sha256')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def is_verified(row):
    review = (row.eligibility or {}).get('policy_validation') or {}
    return bool(review.get('version') == POLICY_VERSION and review.get('status') == 'passed' and
                not review.get('issues') and review.get('requirements') and
                row.eligibility.get('policy_source', {}).get('semantic_sha256') and
                review.get('snapshot_fingerprint') == snapshot_fingerprint(row))


def queue(raw, existing=None):
    digest = snapshot_fingerprint(raw)
    old = (existing.eligibility or {}).get('policy_validation') if existing else None
    review = deepcopy(old) if old and old.get('version') == POLICY_VERSION and old.get('snapshot_fingerprint') == digest else {
        'version': POLICY_VERSION, 'status': 'pending', 'snapshot_fingerprint': digest,
        'scope': 'official_api_text', 'requirements': [], 'issues': [{'code': 'policy_validation_pending'}]}
    raw.eligibility = {**raw.eligibility, 'policy_validation': review}
    return review


def validate_policy(raw, previous=None):
    scoped = deepcopy(raw)
    scoped.source_documents = [d for d in scoped.source_documents if d.get('kind') == 'policy_api_text']
    source = raw.eligibility.get('policy_source') or {}
    if not scoped.source_documents or not source.get('semantic_sha256'):
        review = {'version': POLICY_VERSION, 'status': 'needs_review', 'requirements': [],
                  'issues': [{'code': 'policy_api_snapshot_missing'}]}
    elif source.get('unresolved_codes'):
        review = {'version': POLICY_VERSION, 'status': 'needs_review', 'requirements': [],
                  'issues': [{'code': 'unresolved_condition_codes', 'codes': source['unresolved_codes']}]}
    elif any(hashlib.sha256(d.get('content', b'')).hexdigest() != source['semantic_sha256'] or
             d.get('extracted_text') != raw.body_text for d in scoped.source_documents):
        review = {'version': POLICY_VERSION, 'status': 'needs_review', 'requirements': [],
                  'issues': [{'code': 'policy_source_integrity_mismatch'}]}
    else:
        from app.services.policy_sources import text_units
        from app.services.source_evidence import find_quote
        try:
            for doc in scoped.source_documents:
                normalized = json.loads(doc['content'])
                blocks, covered = [], set()
                for unit in text_units(normalized):
                    match = find_quote(raw.body_text, unit['text'])
                    if match is None:
                        raise ValueError('policy_field_text_mismatch')
                    start, end, _ = match
                    blocks.append({**unit, 'text': raw.body_text[start:end], 'start': start, 'end': end,
                                   'location': unit['unit_id'], 'method': 'api_text'})
                    covered.update(range(start, end))
                if any(not char.isspace() and i not in covered for i, char in enumerate(raw.body_text)):
                    raise ValueError('unmapped_policy_text')
                doc['blocks'] = blocks
            review = deepcopy(validate_posting(scoped, previous, consultation='youth_policy'))
        except (ValueError, KeyError, TypeError):
            review = {'version': POLICY_VERSION, 'status': 'needs_review', 'requirements': [],
                      'issues': [{'code': 'policy_source_integrity_mismatch'}]}
    review.update(scope='official_api_text', snapshot_fingerprint=snapshot_fingerprint(raw),
                  checked_at=datetime.now(timezone.utc).isoformat())
    return review


def rebuild_archived_snapshots(db):
    """Re-decode preserved API data, retaining its original collection time."""
    from app.crawlers.public import OnjungchoungnyeonCrawler
    from app.crawlers.registry import upsert_posting
    from app.services.source_archive import archive_attempt
    rows = db.scalars(select(Scholarship).where(Scholarship.source_platform == 'onjungchoungnyeon')).all()
    report = {'rebuilt': 0, 'unchanged': 0, 'missing_response': 0}
    for row in rows:
        attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == row.content_key)
                            .order_by(SourceFetchAttempt.id.desc()).limit(1))
        item = next((d for d in attempt.documents if d.get('kind') == 'policy_api_response'), None) if attempt else None
        stored = db.get(SourceDocument, item.get('document_id')) if item and item.get('document_id') else None
        if not stored:
            report['missing_response'] += 1
            continue
        raw = OnjungchoungnyeonCrawler()._to_raw(json.loads(stored.content))
        if raw is None:
            report['missing_response'] += 1
            continue
        old_capture = row.crawled_at
        captured_at = (row.eligibility.get('policy_source') or {}).get('response_captured_at') or (old_capture.isoformat() if old_capture else None)
        raw.eligibility['policy_source']['response_captured_at'] = captured_at
        if snapshot_fingerprint(raw) == snapshot_fingerprint(row):
            report['unchanged'] += 1
            continue
        review = queue(raw, row)
        archive_attempt(db, raw)
        deadline = raw.deadline_at
        status = PostingStatus.CLOSED if deadline and deadline < datetime.now(timezone.utc) else PostingStatus.NEEDS_REVIEW
        if review['status'] == 'passed' and status != PostingStatus.CLOSED:
            status = PostingStatus.OPEN
        upsert_posting(db, raw, status=status)
        row.crawled_at = old_capture
        db.commit()
        report['rebuilt'] += 1
    return report


def run_batch(db, limit=20, *, ids=None, retry=False):
    if not 1 <= limit <= 1000:
        raise ValueError('policy validation batch limit must be 1..1000')
    query = select(Scholarship).where(Scholarship.source_platform == 'onjungchoungnyeon')
    if ids is not None:
        query = query.where(Scholarship.id.in_(ids))
    else:
        query = query.where(Scholarship.status != PostingStatus.CLOSED)
    rows = db.scalars(query.order_by(Scholarship.id)).all()
    report = {'processed': 0, 'passed': 0, 'needs_review': 0, 'stale': 0, 'rows': []}
    for row in rows:
        previous = row.eligibility.get('policy_validation') or {}
        if is_verified(row) or previous.get('version') == POLICY_VERSION and previous.get('status') == 'needs_review' and not retry:
            continue
        if report['processed'] >= limit:
            break
        attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == row.content_key)
                            .order_by(SourceFetchAttempt.id.desc()).limit(1))
        docs = []
        for item in attempt.documents if attempt else []:
            if item.get('kind') != 'policy_api_text':
                continue
            stored = db.get(SourceDocument, item.get('document_id')) if item.get('document_id') else None
            if stored:
                docs.append({**item, 'content': stored.content})
        raw = RawPosting(title=row.title, source_url=row.source_url, source_platform=row.source_platform,
            category=row.category, source_type=row.source_type, organization=row.organization,
            deadline_at=row.deadline_at, body_text=row.body_text, eligibility=deepcopy(row.eligibility),
            benefit=deepcopy(row.benefit), required_documents=deepcopy(row.required_documents), source_documents=docs)
        initial_snapshot = snapshot_fingerprint(row)
        review = validate_policy(raw, previous)
        db.refresh(row)
        if snapshot_fingerprint(row) != initial_snapshot:
            report['processed'] += 1
            report['stale'] += 1
            report['rows'].append({'id': row.id, 'status': 'stale', 'issues': [{'code': 'policy_changed_during_validation'}]})
            continue
        record_revision(db, row)
        row.eligibility = {**row.eligibility, 'policy_validation': review}
        deadline = row.deadline_at
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone(timedelta(hours=9)))
        row.status = (PostingStatus.CLOSED if deadline and deadline < datetime.now(timezone.utc) else
                      PostingStatus.OPEN if review['status'] == 'passed' else PostingStatus.NEEDS_REVIEW)
        record_revision(db, row)
        db.commit()
        report['processed'] += 1
        report[review['status']] += 1
        report['rows'].append({'id': row.id, 'status': review['status'], 'issues': review['issues'],
                               'requirements': len(review.get('requirements', []))})
    report['pending'] = sum((r.eligibility.get('policy_validation') or {}).get('status') == 'pending' for r in rows)
    return report
