"""Read-only evidence packages for manual K-pass document review."""
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardSourceSnapshot
from app.models.card_document_import import CardDocumentImport
from app.models.card_document_text import CardDocumentText
from app.services.card_linked_documents import DOCUMENTS, archived_document_intact
from app.services.map_verification import aware
from app.services.toss_kpass_document import KPASS_URL, KPASS_TEXT_PARSER, parse_kpass_document


def document_review_pack(db, import_id, *, archive_root=None, now=None):
    from app.services.card_collection import ARCHIVE_ROOT
    from app.services.card_document_crosscheck import TEXT_PARSER
    now = aware(now or datetime.now(timezone.utc))
    root = Path(archive_root or ARCHIVE_ROOT).resolve()
    record = db.get(CardDocumentImport, import_id)
    if record is None:
        raise ValueError('manual_document_import_not_found')
    document = db.get(CardSourceSnapshot, record.snapshot_id)
    source = db.get(CardCollectionSource, document.source_id) if document else None
    spec = DOCUMENTS.get(record.document_identifier)
    issues = []
    pack = {'schema_version': 'kpass_manual_review_v1', 'import_id': record.id,
        'generated_at': now.isoformat(), 'scope': 'manual_evidence_not_verified_terms',
        'declaration': {'document_identifier': record.document_identifier,
            'title': record.declared_title, 'revision': record.declared_revision,
            'origin_url': record.origin_url, 'operator': record.operator,
            'obtained_at': aware(record.obtained_at).isoformat(),
            'imported_at': aware(record.imported_at).isoformat(), 'verified': False},
        'document': None, 'parent': None, 'issues': issues,
        'review_evidence_ready': False, 'metadata_verified': False,
        'crosscheck_completed': False, 'public_approval': False,
        'required_checks': ['document_identity', 'revision_and_effective_period',
            'kpass_product_applicability', 'clause_by_clause_comparison', 'benefit_rule_extraction']}
    if (not spec or not source or source.canonical_url != spec['url']
            or (source.schedule_policy or {}).get('document_identifier') != record.document_identifier
            or record.origin_url not in {KPASS_URL, spec['url']}):
        issues.append('manual_document_identity_unresolved')
        return pack
    if not aware(record.obtained_at) <= aware(record.imported_at) <= now:
        issues.append('manual_document_timestamps_invalid')
    text = db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id == document.id,
        CardDocumentText.parser_version == TEXT_PARSER))
    if not archived_document_intact(root, document):
        issues.append('manual_document_archive_integrity_failure')
    elif (text is None or text.raw_sha256 != document.raw_sha256
            or hashlib.sha256(text.text.encode()).hexdigest() != text.text_sha256):
        issues.append('manual_document_text_integrity_failure')
    else:
        pack['document'] = {'source_id': source.id, 'snapshot_id': document.id,
            'raw_sha256': document.raw_sha256, 'text_id': text.id, 'parser': text.parser_version,
            'text_sha256': text.text_sha256, 'text': text.text,
            'evidence': {'quote': text.text, 'start_offset': 0, 'end_offset': len(text.text)},
            'document_role': spec['role']}
    parent_source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == KPASS_URL))
    if not parent_source or not parent_source.issuer_id or parent_source.issuer_id != source.issuer_id:
        issues.append('parent_identity_unresolved')
        return pack
    if not parent_source.enabled or parent_source.last_error:
        issues.append('parent_source_unavailable')
    observed = db.scalar(select(CardCollectionAttempt).where(
        CardCollectionAttempt.source_id == parent_source.id,
        CardCollectionAttempt.snapshot_id.is_not(None),
        CardCollectionAttempt.status.in_(['captured', 'unchanged']),
        CardCollectionAttempt.http_status.in_([200, 304]))
        .order_by(CardCollectionAttempt.id.desc()).limit(1))
    parent = db.get(CardSourceSnapshot, observed.snapshot_id) if observed else None
    if not parent or parent.source_id != parent_source.id:
        issues.append('parent_document_missing')
        return pack
    full = db.scalar(select(CardCollectionAttempt).where(
        CardCollectionAttempt.source_id == parent_source.id, CardCollectionAttempt.snapshot_id == parent.id,
        CardCollectionAttempt.status == 'captured', CardCollectionAttempt.http_status == 200)
        .order_by(CardCollectionAttempt.id.desc()).limit(1))
    if not full or not now - timedelta(days=7) <= aware(full.attempted_at) <= now:
        issues.append('parent_full_document_stale')
    parent_text = db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id == parent.id,
        CardDocumentText.parser_version == KPASS_TEXT_PARSER))
    if not archived_document_intact(root, parent):
        issues.append('parent_archive_integrity_failure')
        return pack
    if (parent_text is None or parent_text.raw_sha256 != parent.raw_sha256
            or hashlib.sha256(parent_text.text.encode()).hexdigest() != parent_text.text_sha256):
        issues.append('parent_text_integrity_failure')
        return pack
    try:
        parsed = parse_kpass_document((root / parent.raw_object_ref).read_bytes())
    except (OSError, ValueError):
        issues.append('parent_reference_parse_failed')
        return pack
    if parsed['text_sha256'] != parent_text.text_sha256:
        issues.append('parent_derived_text_mismatch')
        return pack
    references = [r for r in parsed['references'] if r['target'] == record.document_identifier
        and r['label'] == spec['label']]
    if len(references) != 1:
        issues.append('current_parent_reference_unresolved')
    pack['parent'] = {'source_id': parent_source.id, 'snapshot_id': parent.id,
        'observed_at': aware(observed.attempted_at).isoformat(), 'raw_sha256': parent.raw_sha256,
        'text_sha256': parent_text.text_sha256, 'parser': parent_text.parser_version,
        'text': parent_text.text, 'evidence': parsed['segments'], 'references': references}
    pack['review_evidence_ready'] = not issues
    return pack
