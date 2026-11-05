"""Identity, archive integrity and failure handling must not imply publication."""
from datetime import datetime, timedelta, timezone
import hashlib

import httpx
import pytest
from sqlalchemy import func, select

from app.models.card import Card, CardBenefit
from app.models.card_collection import (
    CardIssuer, CardCollectionSource, CardProductSource, CardSourceSnapshot,
    CardCollectionAttempt, CardCollectionRun,
)
from app.services.card_collection import (
    bootstrap_candidates, canonical_url, capture_document, collect_due,
    next_check, register_source,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
URL = 'https://www.tossbank.com/card/benefits'
BODY = ('<html><main><h1>토스뱅크 체크카드</h1>' + '공식 혜택 안내와 적용 조건. ' * 20 + '</main></html>').encode()


def scalar_count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_candidates_idempotent_keep_partner_issuer(db):
    bootstrap_candidates(db)
    bootstrap_candidates(db)
    db.commit()
    assert scalar_count(db, CardProductSource) == 8
    assert scalar_count(db, CardCollectionSource) == 9
    assert scalar_count(db, Card) == scalar_count(db, CardBenefit) == 0
    products = db.scalars(select(CardProductSource)).all()
    assert {p.review_state for p in products} == {'pending'}
    for p in products:
        key = db.get(CardIssuer, p.issuer_id).issuer_key
        if p.external_product_code in ('chakboot', 'plcc'):
            assert key == 'shinhan'
        if p.external_product_code == '18669':
            assert key == 'hana'


@pytest.mark.parametrize('url', [
    'http://www.tossbank.com/card/benefits', 'https://127.0.0.1/',
    'https://www.tossbank.com.evil.test/', 'https://x@www.tossbank.com/',
    'https://www.tossbank.com:8443/', 'https://www.tossbank.com/?token=secret',
    'https://www.hanacard.co.kr/?CD_PD_SEQ=1&CD_PD_SEQ=2',
    'https://www.tossbank.com/#fragment',
])
def test_reject_unapproved_source(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_archive_immutable_and_deduplicated(db, tmp_path):
    source = register_source(db, URL, provider_key='tossbank', now=NOW)
    first = capture_document(db, source, BODY, now=NOW, archive_root=tmp_path)
    same = capture_document(db, source, BODY, now=NOW + timedelta(days=1), archive_root=tmp_path)
    changed = capture_document(db, source, BODY + b'<!-- second version -->', archive_root=tmp_path)
    db.commit()
    assert first.id == same.id != changed.id
    assert first.fetched_at.replace(tzinfo=timezone.utc) == NOW
    assert first.semantic_sha256 == changed.semantic_sha256
    assert scalar_count(db, CardSourceSnapshot) == 2
    assert (tmp_path / first.raw_object_ref).read_bytes() == BODY
    assert first.raw_sha256 == hashlib.sha256(BODY).hexdigest()
    (tmp_path / first.raw_object_ref).write_bytes(b'corruption')
    with pytest.raises(ValueError, match='integrity_failure'):
        capture_document(db, source, BODY, archive_root=tmp_path)


@pytest.mark.parametrize('body', [b'<html><script>app()</script></html>', b'Access denied ' * 20])
def test_empty_and_blocked_document_not_archived(db, tmp_path, body):
    source = register_source(db, URL, provider_key='tossbank')
    with pytest.raises(ValueError):
        capture_document(db, source, body, archive_root=tmp_path)
    assert scalar_count(db, CardSourceSnapshot) == 0
    assert not list(tmp_path.rglob('*'))


def test_end_approaching_schedule_and_timezone(db):
    source = register_source(db, URL, provider_key='tossbank')
    assert next_check(source, NOW) == NOW + timedelta(hours=24)
    source.schedule_policy = {'reviewed_end_at': (NOW + timedelta(days=5)).isoformat()}
    assert next_check(source, NOW) == NOW + timedelta(hours=6)
    source.schedule_policy = {'reviewed_end_at': '2026-10-20T00:00:00'}
    with pytest.raises(ValueError, match='timezone_required'):
        next_check(source, NOW)


def mock_client(response):
    def handler(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        return response(request)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_due_worker_preserves_pending_and_skips_second_run(db, tmp_path):
    register_source(db, URL, provider_key='tossbank', now=NOW)
    db.commit()
    client = mock_client(lambda _: httpx.Response(200, content=BODY, headers={'etag': 'v1'}))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['captured'] == 1 and result['auto_approved'] == 0
    assert scalar_count(db, CardBenefit) == 0
    again = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert again['skipped'] == 1 and again['captured'] == 0
    assert scalar_count(db, CardCollectionAttempt) == 1
    assert again['status'] == 'idle'
    assert scalar_count(db, CardCollectionRun) == 1


def test_daily_304_cannot_refresh_full_capture(db, tmp_path):
    source = register_source(db, URL, provider_key='tossbank', now=NOW)
    capture_document(db, source, BODY, now=NOW - timedelta(days=2), archive_root=tmp_path)
    db.commit()
    client = mock_client(lambda request: httpx.Response(304))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['failed'] == 1
    db.refresh(source)
    assert source.last_full_fetch_at.replace(tzinfo=timezone.utc) == NOW - timedelta(days=2)
    assert source.last_error == 'full_card_document_required'
    assert scalar_count(db, CardSourceSnapshot) == 1


def test_cached_response_uses_latest_observation_after_source_reverts(db, tmp_path, monkeypatch):
    source = register_source(db, URL, provider_key='tossbank', now=NOW)
    first = capture_document(db, source, BODY, now=NOW, archive_root=tmp_path)
    newer = capture_document(db, source, BODY+b'<!-- version two -->', now=NOW, archive_root=tmp_path)
    for snapshot in (first, newer, first):
        db.add(CardCollectionAttempt(source_id=source.id, snapshot_id=snapshot.id,
            attempted_at=NOW, status='captured', http_status=200))
        db.flush()
    db.commit()
    def fetch(client, config, cache, *, force):
        assert not force and cache.content_hash == first.raw_sha256
        return None, {}
    monkeypatch.setattr('app.services.map_sync.fetch_source', fetch)
    result = collect_due(db, client=mock_client(lambda _: httpx.Response(304)),
        now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['unchanged'] == 1 and result['failed'] == 0
    attempt = db.scalar(select(CardCollectionAttempt).order_by(CardCollectionAttempt.id.desc()))
    assert attempt.snapshot_id == first.id


def test_rate_limit_retry_and_history(db, tmp_path):
    source = register_source(db, URL, provider_key='tossbank', now=NOW)
    db.commit()
    client = mock_client(lambda request: httpx.Response(429, headers={'Retry-After': '3600'}))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['failed'] == 1
    db.refresh(source)
    assert source.next_retry_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=1)
    attempt = db.scalar(select(CardCollectionAttempt))
    assert attempt.http_status == 429 and attempt.status == 'failed'
    assert scalar_count(db, CardSourceSnapshot) == 0


@pytest.mark.parametrize('status', [302, 403])
def test_redirect_or_denied_source_is_not_published(db, tmp_path, status):
    register_source(db, URL, provider_key='tossbank', now=NOW)
    db.commit()
    client = mock_client(lambda request: httpx.Response(status, headers={'location': 'https://evil.test/'}))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['failed'] == 1 and result['auto_approved'] == 0
    assert scalar_count(db, CardSourceSnapshot) == 0


def test_archive_migration_roundtrip(tmp_path):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql
    from app.models.card_collection import COLLECTION_MODELS
    migration = importlib.import_module('migrations.versions.09ac2e165439_card_collection_archive')
    engine = create_engine('sqlite:///' + (tmp_path / 'migration.db').as_posix())
    with engine.begin() as connection:
        Card.__table__.create(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            assert {model.__tablename__ for model in COLLECTION_MODELS} <= set(inspect(connection).get_table_names())
            migration.downgrade()
            assert inspect(connection).get_table_names() == ['cards']
    engine.dispose()
    # Verify the additional schema can also generate PostgreSQL DDL.
    for model in COLLECTION_MODELS:
        assert str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))


def test_robots_denial_is_recorded_without_page_fetch(db, tmp_path):
    register_source(db, URL, provider_key='tossbank', now=NOW)
    db.commit()
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text='User-agent: *\nDisallow: /')
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    assert result['failed'] == 1 and paths == ['/robots.txt']
    assert db.scalar(select(CardCollectionAttempt.error_code)) == 'source_disallows_fetch'


def test_existing_map_lease_prevents_parallel_worker(db, tmp_path):
    from app.models.map_verification import MapJobLease
    register_source(db, URL, provider_key='tossbank', now=NOW)
    db.add(MapJobLease(key='map_sync', owner='another-worker',
                      expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
    db.commit()
    client = mock_client(lambda request: pytest.fail('parallel external request'))
    assert collect_due(db, client=client, now=NOW, archive_root=tmp_path)['status'] == 'busy'
    assert scalar_count(db, CardCollectionRun) == 0
    assert db.get(MapJobLease, 'map_sync').owner == 'another-worker'


def test_retry_after_http_date(db, tmp_path):
    from email.utils import format_datetime
    source = register_source(db, URL, provider_key='tossbank', now=NOW)
    db.commit()
    retry_at = NOW + timedelta(hours=2)
    client = mock_client(lambda request: httpx.Response(503, headers={'Retry-After': format_datetime(retry_at)}))
    result = collect_due(db, client=client, now=NOW, archive_root=tmp_path, request_interval=0)
    db.refresh(source)
    assert result['failed'] == 1
    assert source.next_retry_at.replace(tzinfo=timezone.utc) == retry_at
