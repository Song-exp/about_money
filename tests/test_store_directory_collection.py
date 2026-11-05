import json
from datetime import datetime, timedelta, timezone
from threading import Event

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapJobLease, MapReviewEvent
from app.models.store import Store, StoreOffer
from app.services import store_directory_collection as service


KEY = 'store-directory:outback-cheongnyangni'
ROW = {'stIdx': 279, 'stNm': '청량리역사점', 'addrN': '서울 동대문구 왕산로 214',
    'addrDetail': '4층', 'lat': '37.5841971', 'lng': '127.051139', 'deliveryOnly': 'N'}


@pytest.fixture(autouse=True)
def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'outback-cheongnyangni', 'provider': 'outback', 'query': '청량리'}]))
    monkeypatch.setattr(settings, 'card_source_archive_dir', str(tmp_path))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)


def transport(rows=None, code='0005', robots='User-agent: *\nAllow: /'):
    requests = []

    def reply(request):
        requests.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text=robots)
        assert request.url.params['searchKeyword'] == '청량리'
        return httpx.Response(200, json={'resultCode': code, 'resultData': {'storeList': [ROW] if rows is None else rows}})

    return httpx.Client(transport=httpx.MockTransport(reply)), requests


def test_success_due_skip_raw_archive_and_no_map_or_benefit_publication(db):
    client, requests = transport()
    with client:
        result = service.collect_store_directories(db, client=client)
        state = db.get(MapSourceState, KEY)
        assert result['sources'][0]['status'] == 'success' and len(state.records) == 1
        assert state.source_kind == 'store_directory' and state.last_success_at
        assert service.aware(state.next_retry_at) - service.aware(state.last_success_at) == timedelta(hours=24)
        skipped = service.collect_store_directories(db, client=client)
        assert skipped['sources'][0]['status'] == 'not_due' and len(requests) == 2
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert db.scalar(select(func.count()).select_from(StoreOffer)) == 0
    assert db.get(MapJobLease, service.LEASE_KEY) is None
    event = db.scalar(select(MapReviewEvent))
    from pathlib import Path
    assert Path(event.payload['archive']).exists() and event.payload['map_publication'] is False


@pytest.mark.parametrize('mode', ['unsuccessful', 'empty', 'network'])
def test_failure_preserves_last_good_rows_hash_and_observation_time(db, mode):
    client, _ = transport()
    with client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    before = (state.content_hash, state.records, state.last_success_at)
    if mode == 'network':
        def fail(request):
            raise httpx.ReadTimeout('sensitive diagnostic')
        client = httpx.Client(transport=httpx.MockTransport(fail))
    else:
        client, _ = transport(rows=[] if mode == 'empty' else None, code='ERROR' if mode == 'unsuccessful' else '0005')
    with client:
        service.collect_store_directories(db, client=client, force=True)
    db.refresh(state)
    assert state.status == 'error' and state.failures == 1
    assert (state.content_hash, state.records, state.last_success_at) == before
    assert 'sensitive' not in state.error
    assert service.aware(state.next_retry_at) > datetime.now(timezone.utc)


def test_robots_denial_prevents_search_and_sets_24_hour_retry(db):
    client, requests = transport(robots='User-agent: *\nDisallow: /')
    with client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    assert state.error == 'source_disallows_fetch' and len(requests) == 1
    assert service.aware(state.next_retry_at) > datetime.now(timezone.utc) + timedelta(hours=23)


def test_repeated_same_raw_updates_observation_without_publishing(db):
    client, _ = transport()
    with client:
        service.collect_store_directories(db, client=client)
        result = service.collect_store_directories(db, client=client, force=True)
    assert result['sources'][0]['status'] == 'unchanged'
    assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 2
    assert db.scalar(select(func.count()).select_from(Store)) == 0


def test_another_worker_lease_prevents_network_calls(db):
    db.add(MapJobLease(key=service.LEASE_KEY, owner='other', expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    db.commit()
    client, requests = transport()
    with client:
        assert service.collect_store_directories(db, client=client)['status'] == 'busy'
    assert requests == [] and db.get(MapJobLease, service.LEASE_KEY).owner == 'other'


def test_configured_query_cannot_relabel_prior_records(db, monkeypatch):
    client, _ = transport()
    with client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    old_url = state.source_url
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'outback-cheongnyangni', 'provider': 'outback', 'query': '다른지점'}]))
    client, requests = transport()
    with client:
        service.collect_store_directories(db, client=client, force=True)
    assert requests == []
    assert state.source_url == old_url and state.error == 'source_configuration_changed_requires_review'


def test_admin_status_requires_authentication_and_reports_due_state(client):
    assert client.get('/admin/map/store-directories').status_code == 401
    response = client.get('/admin/map/store-directories', headers={'X-Admin-Token': 'test-admin-token'})
    assert response.status_code == 200
    assert response.json()['sources'][0]['status'] == 'never'


def test_store_job_can_run_with_other_schedulers_disabled(monkeypatch):
    import app.services.scheduler as scheduler
    for name in ('crawl_schedule_enabled', 'map_sync_enabled', 'card_collection_enabled'):
        monkeypatch.setattr(settings, name, False)
    monkeypatch.setattr(settings, 'store_collection_enabled', True)
    ran = Event()
    monkeypatch.setattr(scheduler, '_store_collection_job', ran.set)
    try:
        scheduled = scheduler.start_scheduler()
        assert {j.id for j in scheduled.get_jobs()} == {'store_directory_collection'}
        assert ran.wait(2)
    finally:
        scheduler.shutdown_scheduler()


@pytest.mark.parametrize('query', ['???', '', '   '])
def test_broken_or_empty_queries_cannot_be_scheduled(monkeypatch, query):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'broken', 'provider': 'outback', 'query': query}]))
    with pytest.raises(ValueError, match='invalid_store_directory_configuration'):
        service.configured_directories()


def test_lost_lease_cannot_publish_observation_or_release_new_owner(db, monkeypatch):
    def replacement(client, source):
        lease = db.get(MapJobLease, service.LEASE_KEY)
        lease.owner = 'replacement'
        db.commit()
        return '{}', []
    monkeypatch.setattr(service, 'fetch_directory', replacement)
    client, _ = transport()
    with client, pytest.raises(RuntimeError, match='lease_lost'):
        service.collect_store_directories(db, client=client)
    assert db.get(MapJobLease, service.LEASE_KEY).owner == 'replacement'
    assert db.get(MapSourceState, KEY).last_success_at is None
    assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 0


def test_corrupt_archive_is_detected_without_replacing_last_good_state(db):
    from pathlib import Path
    client, _ = transport()
    with client:
        service.collect_store_directories(db, client=client)
        event = db.scalar(select(MapReviewEvent))
        Path(event.payload['archive']).write_text('corrupt', encoding='utf-8')
        observed = db.get(MapSourceState, KEY).last_success_at
        service.collect_store_directories(db, client=client, force=True)
    state = db.get(MapSourceState, KEY)
    assert state.error == 'store_directory_archive_corrupt'
    assert state.last_success_at == observed and len(state.records) == 1
