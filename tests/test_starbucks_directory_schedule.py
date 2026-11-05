import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent
from app.models.store import Store, StoreOffer
from app.services import store_directory_collection as service
from app.scripts.probe_starbucks_stores import is_starbucks_search_response, capture_starbucks


KEY = 'store-directory:starbucks-khu'
CONFIG = {'key': 'starbucks-khu', 'provider': 'starbucks', 'query': '경희대', 'official_name': '경희대', 'official_id': '9551'}
ROW = {'s_biz_code': '9551', 's_name': '경희대', 'doro_address': '서울특별시 동대문구 경희대로 16-1 (회기동)',
    'lat': '37.59149', 'lot': '127.0528'}


@pytest.fixture(autouse=True)
def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([CONFIG]))
    monkeypatch.setattr(settings, 'card_source_archive_dir', str(tmp_path))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)
    monkeypatch.setattr(service, 'capture_starbucks', lambda *args: json.dumps({'list': [ROW]}))


def client_for(policy='User-agent: *\nAllow: /'):
    return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=policy)))


def test_browser_observation_can_be_reconstructed_and_does_not_publish(db):
    with client_for() as client:
        service.collect_store_directories(db, client=client, trigger='scheduled')
        state = db.get(MapSourceState, KEY)
        assert state.status == 'success' and state.records[0]['official_id'] == '9551'
        assert service.collect_store_directories(db, client=client)['sources'][0]['status'] == 'not_due'
        assert service.collect_store_directories(db, client=client, force=True)['sources'][0]['status'] == 'unchanged'
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_observed'))
    bundle = json.loads(Path(event.payload['archive']).read_text(encoding='utf-8'))
    receipt = {**bundle['receipt'], 'captured_at': service.aware(event.at).isoformat()}
    candidate = service.starbucks_candidate(bundle['stores_json'], receipt, '9551', '경희대', datetime.now(timezone.utc))
    assert candidate['official_id'] == '9551'
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert db.scalar(select(func.count()).select_from(StoreOffer)) == 0


@pytest.mark.parametrize('failure', ['timeout', 'browser', 'identity', 'duplicate'])
def test_failed_browser_or_changed_store_preserves_prior_records(db, monkeypatch, failure):
    with client_for() as client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    previous = (state.records, state.content_hash, state.last_success_at)

    def capture(*args):
        if failure == 'timeout':
            raise service.BrowserTimeoutError('sensitive browser diagnostics')
        if failure == 'browser':
            raise service.BrowserError('sensitive executable path')
        return json.dumps({'list': [ROW, ROW] if failure == 'duplicate' else [{**ROW, 's_name': '다른지점'}]})

    monkeypatch.setattr(service, 'capture_starbucks', capture)
    with client_for() as client:
        service.collect_store_directories(db, client=client, force=True)
    assert state.status == 'error' and 'sensitive' not in state.error
    assert (state.records, state.content_hash, state.last_success_at) == previous


def test_disallowed_endpoint_prevents_browser_start(db, monkeypatch):
    def must_not_open(*args):
        raise AssertionError('disallowed browser request')
    monkeypatch.setattr(service, 'capture_starbucks', must_not_open)
    with client_for('User-agent: *\nDisallow: /store/getStore.do') as client:
        service.collect_store_directories(db, client=client)
    assert db.get(MapSourceState, KEY).error == 'source_disallows_fetch'


@pytest.mark.parametrize('url,method,body,expected', [
    ('https://www.starbucks.co.kr/store/getStore.do', 'POST', 'search_text=abc', True),
    ('https://evil.example/store/getStore.do', 'POST', 'search_text=abc', False),
    ('https://www.starbucks.co.kr/store/getStore.do', 'GET', 'search_text=abc', False),
    ('https://www.starbucks.co.kr/store/getStore.do', 'POST', 'search_text=other', False),
    ('https://www.starbucks.co.kr/store/getStore.do', 'POST', 'search_text=abc&search_text=other', False)])
def test_only_exact_official_search_response_is_captured(url, method, body, expected):
    response = SimpleNamespace(url=url, request=SimpleNamespace(method=method, post_data=body))
    assert is_starbucks_search_response(response, 'abc') == expected


def test_untrusted_source_is_rejected_before_opening_browser():
    with pytest.raises(ValueError):
        capture_starbucks('경희대', 'https://example.com/store/store_map.do')


@pytest.mark.parametrize('change', [{'official_id': '3391'}, {'official_name': '회기역'}, {'query': '???'}])
def test_only_previously_verified_branch_bindings_are_schedulable(monkeypatch, change):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([{**CONFIG, **change}]))
    with pytest.raises(ValueError):
        service.configured_directories()
