from io import BytesIO
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardSourceSnapshot
from app.models.card_document_import import CardDocumentImport
from app.services.card_collection import bootstrap_supporting_documents
from app.services.card_document_import import import_document
from app.services.card_linked_documents import DOCUMENTS, linked_document_status
from app.services.toss_kpass_document import KPASS_URL
from tests.test_card_linked_documents import parent
from tests.test_card_collection import NOW


def pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 10 Tf 10 700 Td (' + b'Declared card terms need identity and applicability review. ' * 5 + b') Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def intake(db, tmp_path, **overrides):
    args = dict(declared_title='Declared product guide', declared_revision='unknown',
        obtained_at=NOW, operator='local-reviewer', origin_url=KPASS_URL, now=NOW, archive_root=tmp_path)
    args.update(overrides)
    return import_document(db, '60303005', pdf(), **args)


def test_manual_import_is_idempotent_and_never_http_success(db, tmp_path):
    bootstrap_supporting_documents(db, now=NOW)
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == DOCUMENTS['60303005']['url']))
    source.last_error = 'source_disallows_fetch'
    source.last_full_fetch_at = NOW - timedelta(days=10)
    db.flush()
    before = source.last_full_fetch_at
    result = intake(db, tmp_path)
    again = intake(db, tmp_path, operator='another-reviewer')
    assert again['import_id'] == result['import_id']
    assert db.scalar(select(func.count()).select_from(CardDocumentImport)) == 1
    assert db.scalar(select(func.count()).select_from(CardCollectionAttempt)) == 0
    assert source.last_full_fetch_at == before and source.last_error == 'source_disallows_fetch'
    assert source.last_success_at is None
    assert result['status'] == 'manual_identity_review_required'
    assert not result['public_approval'] and not result['metadata_verified']
    record = db.get(CardDocumentImport, result['import_id'])
    assert record.operator == 'local-reviewer' and record.declared_revision == 'unknown'


@pytest.mark.parametrize('change', [dict(origin_url='https://example.org/file.pdf'),
    dict(operator=''), dict(declared_revision=''), dict(obtained_at=NOW+timedelta(seconds=1)),
    dict(obtained_at=NOW.replace(tzinfo=None))])
def test_invalid_provenance_has_no_snapshot(db, tmp_path, change):
    bootstrap_supporting_documents(db, now=NOW)
    with pytest.raises(ValueError):
        intake(db, tmp_path, **change)
    assert db.scalar(select(func.count()).select_from(CardSourceSnapshot)) == 0


def test_manual_evidence_visible_but_collection_remains_blocked(db, tmp_path):
    snapshot, text = parent(db, tmp_path)
    bootstrap_supporting_documents(db, now=NOW)
    result = intake(db, tmp_path)
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == DOCUMENTS['60303005']['url']))
    source.last_error = 'source_disallows_fetch'
    db.flush()
    status = linked_document_status(db, snapshot, text, archive_root=tmp_path, now=NOW)
    row = next(r for r in status['documents'] if r['document_identifier'] == '60303005')
    assert row['status'] == 'collection_blocked'
    assert row['manual_document']['status'] == 'manual_identity_review_required'
    document = db.get(CardSourceSnapshot, result['snapshot_id'])
    (tmp_path/document.raw_object_ref).write_bytes(b'tampered')
    status = linked_document_status(db, snapshot, text, archive_root=tmp_path, now=NOW)
    row = next(r for r in status['documents'] if r['document_identifier'] == '60303005')
    assert row['manual_document']['status'] == 'integrity_failure'


def test_non_pdf_rejected_before_archive(db, tmp_path):
    bootstrap_supporting_documents(db, now=NOW)
    with pytest.raises(ValueError, match='manual_document_pdf_required'):
        import_document(db, '60303005', b'not pdf', declared_title='Guide', declared_revision='unknown',
            obtained_at=NOW, operator='reviewer', origin_url=KPASS_URL, now=NOW, archive_root=tmp_path)
    assert db.scalar(select(func.count()).select_from(CardDocumentImport)) == 0


def test_decoder_failure_rolls_back_database_and_collection_metadata(db, tmp_path, monkeypatch):
    bootstrap_supporting_documents(db, now=NOW)
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == DOCUMENTS['60303005']['url']))
    source.last_error = 'source_disallows_fetch'
    db.flush()
    def fail(*args, **kwargs):
        raise ValueError('supporting_document_unreadable')
    monkeypatch.setattr('app.services.card_document_crosscheck.prepare_document_text', fail)
    with pytest.raises(ValueError, match='supporting_document_unreadable'):
        intake(db, tmp_path)
    assert db.scalar(select(func.count()).select_from(CardSourceSnapshot)) == 0
    assert db.scalar(select(func.count()).select_from(CardDocumentImport)) == 0
    db.refresh(source)
    assert source.last_full_fetch_at is None and source.last_error == 'source_disallows_fetch'


def test_manual_only_archive_cannot_become_http_cache(db, tmp_path, monkeypatch):
    from app.services.card_collection import collect_due
    from tests.test_card_collection import mock_client
    bootstrap_supporting_documents(db, now=NOW)
    result = intake(db, tmp_path)
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == DOCUMENTS['60303005']['url']))
    # Even inconsistent historical HTTP metadata cannot promote manual content.
    source.last_full_fetch_at = NOW
    db.commit()
    def fetch(client, config, cache, *, force):
        assert force and cache.content_hash is None
        return None, {}
    monkeypatch.setattr('app.services.map_sync.fetch_source', fetch)
    collected = collect_due(db, client=mock_client(lambda _: None), now=NOW,
        archive_root=tmp_path, only_source_ids=[source.id], request_interval=0)
    assert collected['failed'] == 1 and collected['unchanged'] == 0
    attempt = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id))
    assert attempt.snapshot_id is None and attempt.status == 'failed'
    assert db.get(CardSourceSnapshot, result['snapshot_id']) is not None
