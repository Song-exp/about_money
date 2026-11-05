"""Partial official catalog discovery never claims full coverage or approval."""
from datetime import datetime, timedelta, timezone
from threading import Event

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.card import Card, CardBenefit
from app.models.card_collection import CardCollectionSource, CardProductSource, CardIssuer, CardCollectionRun
from app.models.map_verification import MapReviewEvent
from app.services.card_collection import collect_due, register_source, register_product
from app.services.card_discovery import (
    BC_LISTING_URL, BC_ADAPTER, bootstrap_discovery, parse_bc_recommendations,
    SHINHAN_LISTING_URL, parse_shinhan_public_links,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def listing(extra=''):
    return ('''<html><main><h1>카드상품 한눈에보기</h1><h2>추천카드</h2>
      <a href="/app/card/contents/CreditCardMain.do?gdsno=1020400000">[BC바로] On＆Off 카드</a>
      <a href="/app/card/contents/CreditCardMain.do?gdsno=7577770000">[IBK기업은행] 데일리위드카드</a>
      <a href="https://evil.test/?gdsno=1234567890">[하나] 외부 링크 카드</a>
      <a href="/app/card/contents/CreditCardMain.do?gdsno=3333330000">[미확인은행] 확인 필요 카드</a>
      <a href="/app/card/CreditCardMain.do">신용카드</a>
      <p>개인 카드의 추천 상품 목록입니다. 실제 발급사와 상품별 상세 안내를 확인해 주세요.
      혜택 적용 대상, 이용 조건, 제외 사항 및 서비스 변경 안내는 각 상품의 공식 설명서에서 확인할 수 있습니다.</p>
      ''' + extra + '</main></html>').encode('utf-8')


def test_explicit_issuer_codes_and_partial_coverage():
    result = parse_bc_recommendations(listing(), BC_LISTING_URL)
    assert [(p['code'], p['issuer'][0]) for p in result['products']] == [
        ('1020400000', 'bc_baro'), ('7577770000', 'ibk')]
    assert result['catalog_complete'] is False
    assert result['scope'] == 'recommendations_only'
    assert result['excluded_labels'] == 2


@pytest.mark.parametrize('body', [b'<html>no product list</html>', '<h1>추천카드</h1>'.encode()])
def test_missing_layout_or_products_not_empty_success(body):
    with pytest.raises(ValueError):
        parse_bc_recommendations(body, BC_LISTING_URL)


def test_duplicate_identity_conflict_rejected():
    body = listing('<a href="/app/card/contents/CreditCardMain.do?gdsno=1020400000">[하나] 다른 카드</a>')
    with pytest.raises(ValueError, match='conflicting_card_listing_identity'):
        parse_bc_recommendations(body, BC_LISTING_URL)


def test_query_injection_not_registered():
    body = listing('<a href="/app/card/contents/CreditCardMain.do?gdsno=2222220000&amp;token=secret">[하나] 카드</a>')
    with pytest.raises(ValueError, match='unapproved_card_query'):
        parse_bc_recommendations(body, BC_LISTING_URL)


def test_discovery_registration_idempotent(db):
    bootstrap_discovery(db)
    bootstrap_discovery(db)
    assert db.scalar(select(func.count()).select_from(CardCollectionSource)) == 3
    assert db.scalar(select(func.count()).select_from(CardProductSource)) == 0


def run_listing(db, tmp_path, body=None, now=NOW, status=200):
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == BC_LISTING_URL))
    if source is None:
        source = register_source(db, BC_LISTING_URL, provider_key='bc_catalog', kind='listing', now=now)
        source.adapter_key = BC_ADAPTER
    else:
        source.next_check_at = now
    db.commit()
    requested = []
    def handle(request):
        requested.append(str(request.url))
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        assert str(request.url) == BC_LISTING_URL  # New details are collected next tick.
        return httpx.Response(status, content=body or listing(), headers={'etag': 'catalog-v1'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = collect_due(db, client=client, now=now, archive_root=tmp_path, request_interval=0, max_sources=1)
    return source, result, requested


def test_discovery_creates_pending_candidates_with_snapshot_evidence(db, tmp_path):
    source, result, requested = run_listing(db, tmp_path)
    assert result['new_products'] == result['observed_products'] == 2, (result, source.last_error)
    assert result['auto_approved'] == 0
    products = db.scalars(select(CardProductSource)).all()
    assert {p.review_state for p in products} == {'pending'}
    assert {db.get(CardIssuer, p.issuer_id).issuer_key for p in products} == {'bc_baro', 'ibk'}
    assert db.scalar(select(func.count()).select_from(Card)) == 0
    assert db.scalar(select(func.count()).select_from(CardBenefit)) == 0
    events = db.scalars(select(MapReviewEvent).where(MapReviewEvent.kind == 'card_discovery')).all()
    assert len(events) == 2
    assert all(e.payload['snapshot_id'] and e.payload['catalog_complete'] is False for e in events)
    assert len(requested) == 2


def test_repeat_listing_updates_last_seen_without_duplicates_or_retiring_missing(db, tmp_path):
    source, first, _ = run_listing(db, tmp_path)
    # Keep details out of this catalog-only test. Disappearing partial recommendations
    # must not be interpreted as card issuance/benefit termination.
    for detail in db.scalars(select(CardCollectionSource).where(CardCollectionSource.id != source.id)):
        detail.next_check_at = NOW + timedelta(days=5)
    _, second, _ = run_listing(db, tmp_path, now=NOW + timedelta(days=1))
    assert first['new_products'] == 2 and second['new_products'] == 0
    assert db.scalar(select(func.count()).select_from(CardProductSource)) == 2
    assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 2
    assert {p.last_seen_at.replace(tzinfo=timezone.utc) for p in db.scalars(select(CardProductSource))} == {NOW + timedelta(days=1)}


def test_mid_listing_identity_failure_rolls_back_all_candidates(db, tmp_path):
    bad_source = register_source(db, 'https://www.bccard.com/app/card/app/CreditCardMain.do?gdsno=7577770000',
                                 provider_key='bc_catalog', issuer=('ibk', 'IBK기업은행'), now=NOW)
    bad_source.next_check_at = NOW + timedelta(days=5)
    register_product(db, bad_source, namespace='bc_catalog', code='7577770000', name='기존 자료', product_type='credit')
    db.commit()
    _, result, _ = run_listing(db, tmp_path)
    assert result['failed'] == 1 and result['new_products'] == 0
    assert db.scalar(select(func.count()).select_from(CardProductSource)) == 1
    assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 0


def test_card_scheduler_independent_and_records_scheduled_trigger(monkeypatch):
    from app.services import scheduler, card_collection
    called = Event()
    parameters = []
    def fake_collect(db, **kwargs):
        parameters.append(kwargs)
        called.set()
        return {'status': 'idle'}
    monkeypatch.setattr(settings, 'crawl_schedule_enabled', False)
    monkeypatch.setattr(settings, 'map_sync_enabled', False)
    monkeypatch.setattr(settings, 'card_collection_enabled', True)
    monkeypatch.setattr(card_collection, 'collect_due', fake_collect)
    scheduler.shutdown_scheduler()
    try:
        instance = scheduler.start_scheduler()
        assert called.wait(5)
        assert parameters == [{'trigger': 'scheduled', 'max_sources': 20}]
        assert instance.get_job('card_source_collection').trigger.interval.total_seconds() == 300
        assert instance.get_job('map_source_sync') is None
    finally:
        scheduler.shutdown_scheduler()


def test_run_trigger_persisted(db, tmp_path):
    _, result, _ = run_listing(db, tmp_path)
    assert db.get(CardCollectionRun, result['run_id']).trigger == 'manual'


def test_map_job_hands_off_after_releasing_its_work(monkeypatch):
    from app.services import scheduler, map_sync
    order = []
    monkeypatch.setattr(settings, 'card_collection_enabled', True)
    monkeypatch.setattr(map_sync, 'sync_sources', lambda *a, **kw: order.append('map'))
    monkeypatch.setattr(scheduler, '_card_collection_job', lambda: order.append('card'))
    scheduler._map_sync_job()
    assert order == ['map', 'card']


def test_shinhan_real_public_links_fixture():
    from pathlib import Path
    body = Path('tests/fixtures/shinhan-card-search.html').read_bytes()
    result = parse_shinhan_public_links(body, SHINHAN_LISTING_URL)
    assert result['catalog_complete'] is False
    assert result['scope'] == 'static_public_links'
    by_code = {p['code']: p for p in result['products']}
    assert by_code['1188312_2207']['name'] == '신한카드 D-day'
    assert by_code['1187937_2207']['name'] == '신한카드 Mr.Life'
    assert by_code['1188312_2207']['url'].endswith('/1188312_2207.html')
    from bs4 import BeautifulSoup
    assert len(BeautifulSoup(body, 'html.parser').select('.card_name_wrap a.card_name')) == 48
    assert len(by_code) == 24  # Desktop/mobile repeats must share product identity.
    assert all('?' not in p['url'] and p['issuer'][0] == 'shinhan' for p in by_code.values())


def test_shinhan_query_selectors_not_silently_removed():
    body = b'''<ul class="card_thumb_list_wrap"><li class="card_name_wrap">
        <a class="card_name" href="/pconts/html/card/apply/credit/1188312_2207.html?version=2">Card Name</a>
        </li></ul>'''
    with pytest.raises(ValueError, match='unapproved_card_listing_query'):
        parse_shinhan_public_links(body, SHINHAN_LISTING_URL)


def test_failed_listing_retains_original_snapshot_for_diagnosis(db, tmp_path):
    from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
    body = ('<main>' + '상품 목록을 구성하는 동적 페이지이며 현재 상세 링크를 제공하지 않습니다. ' * 8 + '</main>').encode()
    _, result, _ = run_listing(db, tmp_path, body=body)
    assert result['failed'] == 1 and result['new_products'] == 0
    snapshot = db.scalar(select(CardSourceSnapshot))
    assert snapshot is not None
    assert db.scalar(select(CardCollectionAttempt)).snapshot_id == snapshot.id
    assert db.scalar(select(func.count()).select_from(CardProductSource)) == 0
