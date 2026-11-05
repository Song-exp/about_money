"""Actual text disagreements, immutable PDF derivations and stale-source guards."""
import hashlib
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.models.card_document_text import CardDocumentText
from app.models.card_benefit_version import CardBenefitVersion
from app.services.card_benefit_parsers import HANA_DAY_URL, parse_hana_day
from app.services.card_collection import register_source, bootstrap_supporting_documents
from app.services.card_benefit_extraction import extract_snapshot
from app.services.card_document_crosscheck import (
    DAY_LEAFLET_URL, TEXT_PARSER, compare_day, document_checks, prepare_document_text,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def fixture(name):
    return Path('tests/fixtures/' + name).read_text(encoding='utf-8')


def snapshot(db, source, text, raw=b'%PDF test fixture'):
    row = CardSourceSnapshot(source_id=source.id, fetched_at=NOW,
        raw_sha256=hashlib.sha256(raw).hexdigest(), semantic_sha256=hashlib.sha256(text.encode()).hexdigest(),
        raw_object_ref='fixture', extracted_text=text, mime_type='application/pdf',
        parser_version='fixture', completeness='text_available')
    db.add(row)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=row.id, status='captured',
        http_status=200, attempted_at=NOW))
    return row


@pytest.fixture
def documents(db):
    primary = register_source(db, HANA_DAY_URL, provider_key='hana', issuer=('hana', '하나카드'), now=NOW)
    source = register_source(db, DAY_LEAFLET_URL, provider_key='hana', kind='leaflet', issuer=('hana', '하나카드'), now=NOW)
    text = fixture('hana-day-benefits.txt')
    main = snapshot(db, primary, text, raw=b'primary')
    result = extract_snapshot(db, primary, main, now=NOW)
    secondary = snapshot(db, source, fixture('hana-day-leaflet.txt'))
    decoded = fixture('hana-day-leaflet-decoded.txt')
    derived = CardDocumentText(snapshot_id=secondary.id, parser_version=TEXT_PARSER,
        raw_sha256=secondary.raw_sha256, text_sha256=hashlib.sha256(decoded.encode()).hexdigest(),
        text=decoded, created_at=NOW)
    db.add(derived)
    db.commit()
    return db.get(CardBenefitVersion, result['version_id']), source, secondary, derived


def test_actual_documents_disagree_and_bind_evidence_to_each_text(db, documents):
    version, _, snapshot_row, derived = documents
    result = document_checks(db, version, now=NOW)
    assert result['status'] == 'conflict'
    assert len(result['checked_fields']) == 13
    assert {row['field'] for row in result['conflicts']} == {
        'discount_exclusions.' + field for field in ('아파트관리비', '국민연금', '고용보험', '산재보험', '건강보험')}
    assert all(row['detail_value'] is True and row['leaflet_value'] is False for row in result['conflicts'])
    assert result['text_id'] == derived.id and result['raw_sha256'] == snapshot_row.raw_sha256
    for evidence in result['leaflet_evidence']:
        assert derived.text[evidence['start_offset']:evidence['end_offset']] == evidence['quote']
    primary = db.get(CardSourceSnapshot, version.snapshot_id).extracted_text
    evidence = result['primary_evidence']
    assert primary[evidence['start_offset']:evidence['end_offset']] == evidence['quote']


def test_agreeing_subset_still_is_not_full_policy_approval():
    primary = fixture('hana-day-benefits.txt')
    decoded = fixture('hana-day-leaflet-decoded.txt').replace('할인 제외 대상 ',
        '할인 제외 대상 아파트관리비, 국민연금, 고용보험, 산재보험, 건강보험, ', 1)
    result = compare_day(parse_hana_day(primary), primary, decoded)
    assert result['status'] == 'partial_match' and not result['conflicts']
    assert 'rounding_policy' in result['unresolved']


@pytest.mark.parametrize('change,expected', [('stale', 'source_stale'), ('304', 'source_stale'),
    ('tamper', 'text_changed'), ('no_text', 'reparse_required'), ('failed', 'source_unavailable'),
    ('issuer', 'identity_unresolved'), ('new_snapshot', 'reparse_required')])
def test_incomplete_or_untrusted_secondary_source_is_never_a_match(db, documents, change, expected):
    version, source, secondary, derived = documents
    if change in ('stale', '304'):
        attempt = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id))
        attempt.attempted_at = NOW - timedelta(days=8)
        if change == '304':
            db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=secondary.id,
                status='unchanged', http_status=304, attempted_at=NOW))
    elif change == 'tamper':
        derived.text += ' changed'
    elif change == 'no_text':
        db.delete(derived)
    elif change == 'failed':
        source.last_error = 'source_disallows_fetch'
    elif change == 'issuer':
        source.issuer_id = None
    else:
        snapshot(db, source, 'new body', raw=b'%PDF newer')
    db.commit()
    assert document_checks(db, version, now=NOW)['status'] == expected


def test_registration_is_weekly_and_does_not_create_duplicate_products(db):
    first = bootstrap_supporting_documents(db, now=NOW)
    assert bootstrap_supporting_documents(db, now=NOW)['source_ids'] == first['source_ids']
    from app.models.card_collection import CardCollectionSource, CardProductSource
    source = db.get(CardCollectionSource, first['source_ids'][0])
    assert source.schedule_policy['interval_hours'] == 168
    assert db.scalar(select(func.count()).select_from(CardProductSource)) == 0


def test_pdf_reparse_preserves_old_snapshot_and_is_idempotent(db, monkeypatch):
    source = register_source(db, DAY_LEAFLET_URL, provider_key='hana', now=NOW)
    original = fixture('hana-day-leaflet.txt')
    secondary = snapshot(db, source, original)
    import pypdfium2
    decoded = fixture('hana-day-leaflet-decoded.txt')
    class TextPage:
        def get_text_range(self): return decoded
        def close(self): pass
    class Page:
        def get_textpage(self): return TextPage()
        def close(self): pass
    class Document:
        def __init__(self, content): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __len__(self): return 1
        def __getitem__(self, index): return Page()
    monkeypatch.setattr(pypdfium2, 'PdfDocument', Document)
    row = prepare_document_text(db, source, secondary, b'%PDF test fixture', now=NOW)
    again = prepare_document_text(db, source, secondary, b'%PDF test fixture', now=NOW+timedelta(days=1))
    assert row.id == again.id
    assert secondary.extracted_text == original
    assert '/uniC2E4' in original and '/uni' not in row.text
    assert db.scalar(select(func.count()).select_from(CardDocumentText)) == 1
    with pytest.raises(ValueError, match='integrity'):
        prepare_document_text(db, source, secondary, b'%PDF wrong bytes')


def test_comparison_is_visible_in_authenticated_review(client, documents):
    version = documents[0]
    path = f'/admin/map/card-programs/{version.id}'
    assert client.get(path).status_code == 401
    result = client.get(path, headers={'X-Admin-Token': 'test-admin-token'}).json()
    assert result['readiness']['supporting_document']['status'] == 'conflict'
    assert result['readiness']['can_approve'] is False
    assert result['readiness']['blocker'] == 'supporting_document_conflict'


def test_document_text_migration_roundtrip(tmp_path):
    from sqlalchemy import create_engine, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module('migrations.versions.3cdf51498762_card_document_texts')
    engine = create_engine('sqlite:///' + (tmp_path / 'document.db').as_posix())
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert 'card_document_texts' in inspect(connection).get_table_names()
            migration.downgrade()
            assert 'card_document_texts' not in inspect(connection).get_table_names()
    engine.dispose()
