import json
from datetime import date, timedelta
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from app.core.config import settings
from app.models.local_benefit import LocalBenefitMerchant
from app.models.map_source import MapSourceState
from app.models.store import StoreOffer
from app.services.map_sync import geojimap_records, sync_sources, push_promotions, source_health


def listing():
    return Path('tests/fixtures/geojimap-dongdaemun.html').read_bytes()


def mock_client(content, status=200):
    def handler(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        return httpx.Response(status, content=content, headers={'etag': 'listing-v1'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def feed(id='offer-1', complete=True):
    today = date.today()
    return {'schema_version': 1, 'complete': complete, 'items': [{
        'id': id, 'brand': '실제식당', 'branch': '경희대점', 'address': '서울 동대문구',
        'category': 'restaurant', 'lat': 37.59, 'lng': 127.05, 'pay_method': 'kakao',
        'discount_rate': 10, 'condition_text': '행사 기간 1회',
        'valid_from': today.isoformat(), 'valid_to': (today + timedelta(days=1)).isoformat(),
        'source_url': 'https://provider.example/event/offer-1'}]}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(settings, 'map_promotion_sources_json', json.dumps([
        {'key': 'test', 'url': 'https://provider.example/feed.json'}]))


def test_actual_public_listing_has_22_records():
    rows = geojimap_records(listing(), settings.map_geojimap_listing_url)
    assert len(rows) == 22
    assert all(r['price'] > 0 and r['address'] and r['url'].startswith('https://xn--v69ak0xskm.com/r/') for r in rows)
    assert any('따뜻한밥상' in r['name'] and r['price'] == 3000 for r in rows)


def test_upsert_304_and_failure_preserve_last_good(db):
    with mock_client(listing()) as client:
        assert sync_sources(db, client=client)['sources'][0]['records'] == 22
        sync_sources(db, client=client)
    assert len(db.scalars(select(LocalBenefitMerchant)).all()) == 22
    with mock_client(b'', 304) as client:
        assert sync_sources(db, client=client)['sources'][0]['status'] == 'unchanged'
    with mock_client(b'<html>structure changed</html>') as client:
        result = sync_sources(db, client=client)['sources'][0]
    assert result['status'] == 'error' and result['error'] == 'listing_structure_changed'
    state = db.get(MapSourceState, 'geojimap:dongdaemun')
    assert state.last_success_at and len(state.records) == 22
    with mock_client(listing()) as client:
        assert sync_sources(db, client=client)['sources'][0]['status'] == 'backoff'
    assert all(r.is_active for r in db.scalars(select(LocalBenefitMerchant)).all())


def test_robots_disallow_does_not_ingest(db):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='User-agent: *\nDisallow: /'))) as client:
        assert sync_sources(db, client=client)['sources'][0]['error'] == 'source_disallows_fetch'
    assert not db.scalars(select(LocalBenefitMerchant)).all()


def test_complete_snapshot_missing_record_deactivates_only_source_owned(db):
    from app.services.map_sync import apply_records
    state = MapSourceState(source_key='geojimap:dongdaemun', source_url=settings.map_geojimap_listing_url, source_kind='menu_price')
    db.add(state)
    rows = geojimap_records(listing(), settings.map_geojimap_listing_url)
    apply_records(db, state, rows, True)
    db.commit()
    apply_records(db, state, rows[:-1], True)
    db.commit()
    assert len(db.scalars(select(LocalBenefitMerchant).where(LocalBenefitMerchant.is_active.is_(True))).all()) == 21


def test_promotion_push_partial_and_complete(db, provider):
    push_promotions(db, 'promotion:test', feed())
    offer = db.scalar(select(StoreOffer))
    assert not offer.is_sample and offer.discount_rate == 10
    push_promotions(db, 'promotion:test', {'schema_version': 1, 'complete': False, 'items': []})
    assert db.get(StoreOffer, offer.id).is_active
    push_promotions(db, 'promotion:test', {'schema_version': 1, 'complete': True, 'items': []})
    assert not db.get(StoreOffer, offer.id).is_active


def test_invalid_feed_rolls_back_all_rows(db, provider):
    payload = feed()
    payload['items'].append({**payload['items'][0], 'id': 'offer-2', 'source_url': 'https://evil.example/event'})
    with pytest.raises(ValueError):
        push_promotions(db, 'promotion:test', payload)
    assert not db.scalars(select(StoreOffer)).all()


def test_cross_source_conflict_preserves_owned_offer(db, provider, monkeypatch):
    push_promotions(db, 'promotion:test', feed())
    monkeypatch.setattr(settings, 'map_promotion_sources_json', json.dumps([
        {'key': 'test', 'url': 'https://provider.example/feed.json'},
        {'key': 'other', 'url': 'https://provider.example/other.json'}]))
    with pytest.raises(ValueError, match='promotion_source_conflict'):
        push_promotions(db, 'promotion:other', feed())
    assert db.scalar(select(StoreOffer)).discount_rate == 10


def test_public_status_and_admin_auth(client):
    response = client.get('/stores/source-status')
    assert response.status_code == 200
    assert response.json()['promotion_sources_configured'] == 0
    assert client.post('/admin/crawl/map-sources/sync').status_code == 401


def test_sample_and_reported_offers_do_not_rank(client, db, seed_stores):
    for offer in db.scalars(select(StoreOffer)).all():
        offer.is_sample = True
    db.commit()
    items = client.get('/stores/nearby', params={'card_ids': 99999}).json()
    assert all(r['max_discount_rate'] == 0 and r['best_deal'] is None for r in items)
    assert all(r['offers'] == [] for r in items)
    for row in items:
        assert client.get(f"/stores/{row['id']}", params={'card_ids': 99999}).json()['offers'] == []


def test_promotion_evidence_and_stale_exclusion(client, db, provider):
    from datetime import datetime, timezone
    push_promotions(db, 'promotion:test', feed())
    response = client.get('/stores/nearby', params={'card_ids': 99999}).json()[0]
    assert response['best_deal'] is None
    assert response['max_discount_rate'] == 0
    assert response['offers'][0]['source_url'] == 'https://provider.example/event/offer-1'
    assert response['offers'][0]['source_checked_at']
    state = db.get(MapSourceState, 'promotion:test')
    state.last_success_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    response = client.get('/stores/nearby', params={'card_ids': 99999}).json()[0]
    assert response['offers'][0]['source_stale']
    assert response['best_deal'] is None and response['max_discount_rate'] == 0


def test_expired_local_benefits_not_returned(client, db):
    from app.services.map_sync import apply_records
    state = MapSourceState(source_key='geojimap:dongdaemun', source_url=settings.map_geojimap_listing_url, source_kind='menu_price')
    db.add(state)
    apply_records(db, state, geojimap_records(listing(), settings.map_geojimap_listing_url)[:1], True)
    db.scalar(select(LocalBenefitMerchant)).valid_to = date.today() - timedelta(days=1)
    db.commit()
    assert client.get('/stores/local-benefits', params={'program': 'geojimap'}).json()['total'] == 0


def test_map_scheduler_independent_of_scholarship(monkeypatch):
    import app.services.scheduler as scheduler
    monkeypatch.setattr(scheduler, '_map_sync_job', lambda: None)
    monkeypatch.setattr(settings, 'crawl_schedule_enabled', False)
    monkeypatch.setattr(settings, 'map_sync_enabled', True)
    try:
        result = scheduler.start_scheduler()
        assert {j.id for j in result.get_jobs()} == {'map_source_sync'}
    finally:
        scheduler.shutdown_scheduler()
