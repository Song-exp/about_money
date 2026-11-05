"""Review evidence must be current and intact, and never constitutes approval."""
from datetime import timedelta
import pytest
from sqlalchemy import select, func

from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardSourceSnapshot
from app.models.card_document_text import CardDocumentText
from app.models.card_document_import import CardDocumentImport
from app.services.card_collection import bootstrap_supporting_documents
from app.services.card_document_review import document_review_pack
from tests.test_card_document_import import intake
from tests.test_card_linked_documents import parent
from tests.test_card_collection import NOW
from tests.test_toss_kpass_document import payload


def setup_documents(db, tmp_path, data=None):
    snapshot, text = parent(db, tmp_path, data)
    db.add(CardCollectionAttempt(source_id=snapshot.source_id, snapshot_id=snapshot.id,
        attempted_at=NOW, status='captured', http_status=200))
    bootstrap_supporting_documents(db, now=NOW)
    imported = intake(db, tmp_path)
    db.flush()
    return snapshot, text, imported


def test_ready_pack_has_exact_quotes_and_no_approval_or_writes(db, tmp_path):
    snapshot, text, imported = setup_documents(db, tmp_path)
    before = db.scalar(select(func.count()).select_from(CardCollectionAttempt))
    pack = document_review_pack(db, imported['import_id'], archive_root=tmp_path, now=NOW)
    assert pack['review_evidence_ready'] and pack['issues'] == []
    assert pack['parent']['snapshot_id'] == snapshot.id
    assert pack['document']['snapshot_id'] == imported['snapshot_id']
    assert pack['parent']['references'][0]['target'] == '60303005'
    assert pack['declaration']['revision'] == 'unknown' and not pack['declaration']['verified']
    for segment in pack['parent']['evidence']:
        assert pack['parent']['text'][segment['start_offset']:segment['end_offset']] == segment['quote']
    evidence = pack['document']['evidence']
    assert pack['document']['text'][evidence['start_offset']:evidence['end_offset']] == evidence['quote']
    assert not pack['metadata_verified'] and not pack['crosscheck_completed'] and not pack['public_approval']
    assert db.scalar(select(func.count()).select_from(CardCollectionAttempt)) == before
    assert not db.new and not db.dirty and not db.deleted


@pytest.mark.parametrize('change,issue', [
    ('pdf_missing', 'manual_document_archive_integrity_failure'),
    ('pdf_tampered', 'manual_document_archive_integrity_failure'),
    ('pdf_text', 'manual_document_text_integrity_failure'),
    ('parent_tampered', 'parent_archive_integrity_failure'),
    ('parent_text', 'parent_text_integrity_failure'),
    ('parent_stale', 'parent_full_document_stale'),
    ('parent_failed', 'parent_source_unavailable'),
    ('wrong_issuer', 'parent_identity_unresolved'),
    ('future_import', 'manual_document_timestamps_invalid'),
    ('wrong_identifier', 'manual_document_identity_unresolved'),
])
def test_changed_or_incomplete_evidence_never_ready(db, tmp_path, change, issue):
    snapshot, text, imported = setup_documents(db, tmp_path)
    document = db.get(CardSourceSnapshot, imported['snapshot_id'])
    record = db.get(CardDocumentImport, imported['import_id'])
    if change == 'pdf_missing': (tmp_path/document.raw_object_ref).unlink()
    if change == 'pdf_tampered': (tmp_path/document.raw_object_ref).write_bytes(b'changed')
    if change == 'pdf_text': db.get(CardDocumentText, imported['text_id']).text += ' invented'
    if change == 'parent_tampered': (tmp_path/snapshot.raw_object_ref).write_bytes(b'changed')
    if change == 'parent_text': text.text += ' invented'
    if change == 'parent_stale':
        attempt = db.scalar(select(CardCollectionAttempt))
        attempt.attempted_at = NOW-timedelta(days=8)
    if change == 'parent_failed': db.get(CardCollectionSource, snapshot.source_id).last_error = 'fetch_failed'
    if change == 'wrong_issuer': db.get(CardCollectionSource, document.source_id).issuer_id = None
    if change == 'future_import': record.imported_at = NOW+timedelta(days=1)
    if change == 'wrong_identifier': record.document_identifier = '60303001'
    db.flush()
    pack = document_review_pack(db, record.id, archive_root=tmp_path, now=NOW)
    assert issue in pack['issues'] and not pack['review_evidence_ready']
    assert not pack['crosscheck_completed'] and not pack['public_approval']


def test_removed_reference_requires_review_even_with_valid_old_pdf(db, tmp_path):
    data = payload()
    part = data['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['data'][0]['children'][7]['bulleted_list_item']['rich_text'][0]
    part['href'] = part['text']['link']['url'] = '99999999'
    _, _, imported = setup_documents(db, tmp_path, data)
    pack = document_review_pack(db, imported['import_id'], archive_root=tmp_path, now=NOW)
    assert 'current_parent_reference_unresolved' in pack['issues']
    assert pack['parent']['references'] == [] and not pack['review_evidence_ready']


def test_only_304_parent_has_no_full_capture_evidence(db, tmp_path):
    snapshot, _, imported = setup_documents(db, tmp_path)
    attempt = db.scalar(select(CardCollectionAttempt))
    attempt.status, attempt.http_status = 'unchanged', 304
    db.flush()
    pack = document_review_pack(db, imported['import_id'], archive_root=tmp_path, now=NOW)
    assert 'parent_full_document_stale' in pack['issues']
    assert not pack['review_evidence_ready']


def test_missing_parent_is_explicit(db, tmp_path):
    bootstrap_supporting_documents(db, now=NOW)
    imported = intake(db, tmp_path)
    pack = document_review_pack(db, imported['import_id'], archive_root=tmp_path, now=NOW)
    assert 'parent_identity_unresolved' in pack['issues'] and pack['parent'] is None


def test_review_api_requires_admin_and_missing_is_404(client):
    path = '/admin/map/card-document-imports/999/review-pack'
    assert client.get(path).status_code in (401, 403)
    assert client.get(path, headers={'X-Admin-Token': 'test-admin-token'}).status_code == 404


def test_review_api_exposes_evidence_without_approving(db, tmp_path, client, monkeypatch):
    _, _, imported = setup_documents(db, tmp_path)
    db.commit()
    monkeypatch.setattr('app.services.card_collection.ARCHIVE_ROOT', tmp_path)
    response = client.get(f"/admin/map/card-document-imports/{imported['import_id']}/review-pack",
        headers={'X-Admin-Token': 'test-admin-token'})
    assert response.status_code == 200
    pack = response.json()
    assert pack['document']['text'] and not pack['public_approval']
