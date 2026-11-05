"""Offline intake of declared official PDFs; applicability requires later review."""
from datetime import datetime, timezone
from sqlalchemy import select
from app.models.card_collection import CardCollectionSource
from app.models.card_document_import import CardDocumentImport
from app.services.card_linked_documents import DOCUMENTS
from app.services.toss_kpass_document import KPASS_URL

MAX_BYTES = 20 * 1024 * 1024


def import_document(db, document_identifier, content, *, declared_title, declared_revision,
                    obtained_at, operator, origin_url, now=None, archive_root=None):
    from app.services.card_collection import capture_document
    from app.services.card_document_crosscheck import prepare_document_text
    now = now or datetime.now(timezone.utc)
    spec = DOCUMENTS.get(document_identifier)
    if not spec or origin_url not in {KPASS_URL, spec['url']}:
        raise ValueError('manual_document_origin_unresolved')
    for value, limit in ((declared_title, 200), (declared_revision, 200), (operator, 100)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError('manual_document_metadata_required')
    if obtained_at.tzinfo is None or now.tzinfo is None or obtained_at > now:
        raise ValueError('manual_document_obtained_at_invalid')
    if not content.startswith(b'%PDF') or not 0 < len(content) <= MAX_BYTES:
        raise ValueError('manual_document_pdf_required')
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == spec['url']))
    if not source or (source.schedule_policy or {}).get('document_identifier') != document_identifier:
        raise ValueError('manual_document_source_not_registered')
    # capture_document updates this HTTP field; manual intake must preserve it.
    previous_full_fetch = source.last_full_fetch_at
    with db.begin_nested():
        snapshot = capture_document(db, source, content, now=now, archive_root=archive_root)
        source.last_full_fetch_at = previous_full_fetch
        text = prepare_document_text(db, source, snapshot, content, now=now)
        record = db.scalar(select(CardDocumentImport).where(CardDocumentImport.snapshot_id == snapshot.id))
        if record is None:
            record = CardDocumentImport(snapshot_id=snapshot.id, document_identifier=document_identifier,
                declared_title=declared_title.strip(), declared_revision=declared_revision.strip(),
                obtained_at=obtained_at, imported_at=now, operator=operator.strip(), origin_url=origin_url)
            db.add(record)
            db.flush()
    return {'import_id': record.id, 'snapshot_id': snapshot.id, 'text_id': text.id,
            'raw_sha256': snapshot.raw_sha256, 'characters': len(text.text),
            'status': 'manual_identity_review_required', 'metadata_verified': False,
            'crosscheck_completed': False, 'public_approval': False}
