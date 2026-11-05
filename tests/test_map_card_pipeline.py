"""Source fixtures and independently stated expected publication/failure behavior."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bs4 import BeautifulSoup
import httpx
import pytest
from sqlalchemy import select
from app.core.config import settings
from app.models.card import Card, CardBenefit
from app.models.map_source import MapSourceState
from app.models.map_verification import MapEvidence, MapReviewEvent
from app.services.map_card_extractors import extract_pengsoo, KB_CARD, KB_URL
from app.services.map_verification import aware, evidence_status
from app.services.map_sync import sync_sources, source_health


def raw():
    return Path('tests/fixtures/kb-pengsoo-detail.html').read_text(encoding='utf-8')


def changed(transform):
    soup = BeautifulSoup(raw(), 'html.parser')
    block = soup.select_one('.contentDetail')
    text = ''.join(block.get_text().split())
    block.clear()
    block.string = transform(text)
    return str(soup)


@pytest.fixture
def target(db, seed_stores):
    card = db.get(Card, seed_stores['card_id'])
    card.card_key, card.card_name = KB_CARD, 'KB국민 펭수 노리 체크카드'
    db.commit()
    return card.benefits[0]


def client_for(document, requests=None, status=200):
    def handle(request):
        if requests is not None: requests.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        if request.url.host == 'card.kbcard.com':
            return httpx.Response(status, text=document, headers={'etag': 'kb-version'})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handle))


def run(db, document, **kwargs):
    with client_for(document, **kwargs) as client:
        return sync_sources(db, client=client, trigger='test')


def test_capture_validate_publish_and_map_read_in_one_run(db, target, client, seed_stores):
    # Registered product is discovered by card_key without deployment-specific IDs in env.
    assert settings.map_official_sources_json == '[]'
    result = run(db, raw())
    row = db.get(MapEvidence, ('card', target.id))
    assert result['outcome'] == 'success', result['sources'][-1].get('validation')
    assert evidence_status(row, target) == 'approved'
    assert row.rules['value'] == '5' and row.rules['per_transaction_cap'] == 1000
    assert target.value_min == 5 and target.min_payment_krw == 10000
    response = client.get(f"/stores/{seed_stores['mid']}")
    assert any(b['id'] == target.id and b['recommendation_status'] == 'approved' for b in response.json()['card_benefits'])
    history = db.scalars(select(MapReviewEvent).where(MapReviewEvent.action == 'pipeline_finished')).all()
    assert len(history) == 1 and history[0].payload['run_id'] == result['run_id']


def test_consistent_rate_change_updates_native_and_rules_atomically(db, target):
    run(db, raw())
    doc = changed(lambda text: text.replace('GS255%', 'GS2510%').replace(
        'GS2510%환급할인이용금액건당1만원~2만원까지할인적용(1회최대할인금액1,000원)',
        'GS2510%환급할인이용금액건당1만원~2만원까지할인적용(1회최대할인금액2,000원)'))
    assert extract_pengsoo(doc)['policy_hash'] == extract_pengsoo(raw())['policy_hash']
    run(db, doc)
    row = db.get(MapEvidence, ('card', target.id))
    assert evidence_status(row, target) == 'approved'
    assert row.rules['value'] == '10' and target.value_min == 10
    assert row.rules['per_transaction_cap'] == 2000


@pytest.mark.parametrize('transform', [
    lambda text: text.replace('GS255%환급할인이용금액', 'GS2510%환급할인이용금액'),
    lambda text: text.replace('최대할인금액1,000원', '최대할인금액9,000원'),
    lambda text: text + 'GS25일요일할인제외',
    lambda text: text.replace('선불카드구입·충전금액', '선불카드충전만제외'),
    lambda text: text + '행사기간2026.10.01~2026.10.31',
])
def test_conflicting_or_unreviewed_policy_changes_quarantine_old_version(db, target, transform):
    run(db, raw())
    result = run(db, changed(transform))
    row = db.get(MapEvidence, ('card', target.id))
    assert evidence_status(row, target) == 'pending'
    assert result['outcome'] == 'needs_review'
    assert target.value_min == 5  # retained only as historical data; cannot be recommended


def test_recheck_requires_full_body_and_304_does_not_renew(db, target):
    run(db, raw())
    row = db.get(MapEvidence, ('card', target.id))
    old = datetime.now(timezone.utc)-timedelta(days=8)
    row.verified_at, row.recheck_at = old, old+timedelta(days=7)
    db.commit()
    requests = []
    run(db, '', requests=requests, status=304)
    assert evidence_status(row, target) == 'recheck_required'
    assert aware(row.verified_at) == old
    req = next(r for r in requests if r.url.host == 'card.kbcard.com' and r.url.path != '/robots.txt')
    assert 'if-none-match' not in req.headers
    # Full successful fetch is required; retry backoff is bypassed explicitly here.
    with client_for(raw()) as client:
        sync_sources(db, client=client, force=True)
    assert evidence_status(row, target) == 'approved'
    assert aware(row.verified_at) > old


def test_manual_revocation_is_not_overridden_by_changed_source(db, target):
    run(db, raw())
    row = db.get(MapEvidence, ('card', target.id))
    row.status = 'revoked'
    db.commit()
    run(db, changed(lambda text: text + '안내변경'))
    assert row.status == 'revoked'


def test_existing_explicit_period_is_never_erased(db, target):
    target.valid_to = datetime.now(timezone.utc).date()-timedelta(days=1)
    db.commit()
    run(db, raw())
    assert db.get(MapEvidence, ('card', target.id)).status == 'pending'
    assert target.valid_to is not None


def test_guard_error_rolls_back_native_changes_and_quarantines(db, target, monkeypatch):
    from app.services import map_approval
    from fastapi import HTTPException
    def reject(*args, **kwargs): raise HTTPException(422, 'rejected')
    monkeypatch.setattr(map_approval, 'apply_benefit_review', reject)
    run(db, raw())
    assert target.value_min == 20
    assert db.get(MapEvidence, ('card', target.id)).status == 'pending'


def test_time_transition_changes_map_revision_without_new_source(db, target):
    run(db, raw())
    before = source_health(db)['revision']
    row = db.get(MapEvidence, ('card', target.id))
    row.recheck_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    db.commit()
    assert before != source_health(db)['revision']


def test_pipeline_history_admin_authentication(client):
    assert client.get('/admin/map/pipeline/runs').status_code == 401
    assert client.get('/admin/map/pipeline/runs', headers={'X-Admin-Token':'test-admin-token'}).status_code == 200
    assert client.post('/admin/map/pipeline/run').status_code == 401


def test_scheduler_runs_pipeline_at_start_and_every_five_minutes(monkeypatch):
    from threading import Event
    from app.services import scheduler, map_sync
    called = Event()
    triggers = []
    def fake_sync(db, **kwargs):
        triggers.append(kwargs['trigger'])
        called.set()
        return {'status':'complete'}
    monkeypatch.setattr(map_sync, 'sync_sources', fake_sync)
    monkeypatch.setattr(settings, 'map_sync_enabled', True)
    monkeypatch.setattr(settings, 'map_sync_interval_sec', 300)
    monkeypatch.setattr(settings, 'crawl_schedule_enabled', False)
    scheduler.shutdown_scheduler()
    try:
        instance = scheduler.start_scheduler()
        assert called.wait(5)
        assert triggers == ['scheduled']
        assert instance.get_job('map_source_sync').trigger.interval.total_seconds() == 300
    finally:
        scheduler.shutdown_scheduler()


def test_same_product_document_is_fetched_once_for_multiple_benefits(db, target):
    other = CardBenefit(card_id=target.card_id, dedup_key='second-source-item', merchant_raw='스타벅스',
        brand_key='스타벅스', benefit_type=target.benefit_type, benefit_text='과거 데이터', is_active=True)
    db.add(other)
    db.commit()
    requests = []
    run(db, raw(), requests=requests)
    assert len([r for r in requests if r.url.host == 'card.kbcard.com' and r.url.path != '/robots.txt']) == 1
    assert evidence_status(db.get(MapEvidence, ('card', other.id)), other) == 'approved'
