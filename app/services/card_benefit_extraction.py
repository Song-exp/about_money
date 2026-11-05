"""Persist source-specific, pending-only versions without changing public benefits."""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence, CardBenefitExtraction
from app.models.card_collection import CardCollectionSource, CardSourceSnapshot, CardCollectionAttempt
from app.services.card_benefit_parsers import PARSERS


def extract_snapshot(db, source, snapshot, *, now=None):
    supported = PARSERS.get(source.canonical_url)
    if supported is None:
        return {'status': 'unsupported', 'created': False}
    extractor, parser = supported
    if snapshot.source_id != source.id:
        raise ValueError('benefit_snapshot_source_mismatch')
    existing = db.scalar(select(CardBenefitExtraction).where(CardBenefitExtraction.snapshot_id == snapshot.id,
        CardBenefitExtraction.extractor_version == extractor))
    if existing:
        return {'status': existing.status, 'created': False, 'version_id': existing.version_id,
                'issues': existing.issues}
    now = now or datetime.now(timezone.utc)
    try:
        # No inferred facts may be used as evidence. Every locator is relative to
        # the normalized text already retained by the immutable source snapshot.
        payload = parser(snapshot.extracted_text)
        evidence = payload.pop('evidence')
        if len({row['field_path'] for row in evidence}) != len(evidence):
            raise ValueError('duplicate_benefit_evidence_path')
        for row in evidence:
            if snapshot.extracted_text[row['start_offset']:row['end_offset']] != row['quote']:
                raise ValueError('benefit_evidence_locator_invalid')
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        version = CardBenefitVersion(program_key=payload['program_key'], source_id=source.id,
            snapshot_id=snapshot.id, extractor_version=extractor, payload=payload,
            payload_hash=hashlib.sha256(encoded.encode()).hexdigest(), created_at=now,
            review_state='pending', publication_state='draft')
        db.add(version)
        db.flush()
        for row in evidence:
            db.add(CardBenefitFieldEvidence(version_id=version.id, **row))
        attempt = CardBenefitExtraction(source_id=source.id, snapshot_id=snapshot.id,
            extractor_version=extractor, version_id=version.id, status='needs_review',
            issues=payload['issues'], extracted_at=now)
        db.add(attempt)
        db.flush()
        return {'status': attempt.status, 'created': True, 'version_id': version.id,
                'rule_count': len(payload['rules']), 'evidence_count': len(evidence), 'issues': attempt.issues}
    except ValueError as exc:
        attempt = CardBenefitExtraction(source_id=source.id, snapshot_id=snapshot.id,
            extractor_version=extractor, status='failed', issues=[str(exc)[:200]], extracted_at=now)
        db.add(attempt)
        db.flush()
        return {'status': 'failed', 'created': False, 'issues': attempt.issues}


def extract_stored_sources(db):
    """Reparse the last actually observed document; snapshot IDs are not recency.

    A source may revert to an earlier byte-identical object. Its latest attempt,
    not the largest immutable snapshot ID, identifies the currently observed body.
    """
    results = []
    for source in db.scalars(select(CardCollectionSource).where(CardCollectionSource.canonical_url.in_(PARSERS))):
        observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
            CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
        snapshot = db.get(CardSourceSnapshot, observed.snapshot_id) if observed else db.scalar(
            select(CardSourceSnapshot).where(CardSourceSnapshot.source_id == source.id)
            .order_by(CardSourceSnapshot.id.desc()).limit(1))
        if snapshot is None:
            results.append({'source_id': source.id, 'status': 'no_snapshot', 'created': False})
            continue
        results.append({'source_id': source.id, **extract_snapshot(db, source, snapshot)})
    db.flush()
    return results


def review_pack(db, version_ids):
    records = []
    for version_id in dict.fromkeys(version_ids):
        version = db.get(CardBenefitVersion, version_id)
        source = db.get(CardCollectionSource, version.source_id)
        snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
        evidence = db.scalars(select(CardBenefitFieldEvidence).where(
            CardBenefitFieldEvidence.version_id == version.id).order_by(CardBenefitFieldEvidence.id)).all()
        records.append({'version_id': version.id, 'source_url': source.canonical_url,
            'snapshot_id': version.snapshot_id, 'extractor_version': version.extractor_version,
            'snapshot_hash': snapshot.raw_sha256,
            'review_state': version.review_state, 'publication_state': version.publication_state,
            'payload_hash': version.payload_hash, 'payload': version.payload,
            'evidence': [{'field_path': row.field_path, 'quote': row.quote,
                'start_offset': row.start_offset, 'end_offset': row.end_offset} for row in evidence]})
    return {'scope': 'pending_candidates_only_not_publication', 'versions': records}
