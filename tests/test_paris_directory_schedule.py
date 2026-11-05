import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.models.map_source import MapSourceState
from app.models.map_verification import MapReviewEvent
from app.models.store import Store, StoreOffer
from app.services import store_directory_collection as service
from app.services.paris_store_identity import paris_capture_rows


AREAS = json.dumps({'cat_117': {'id': 117, 'name': '서울', 'children': [{'id': 167, 'name': '동대문구'}]}}, ensure_ascii=False)
HTML = '''<ul data-total-count="1"><li><div class="store-list-item" data-id="123"
data-latitude="37.59" data-longitude="127.05"><h3 class="store-name"><span class="label">검증용</span></h3>
<p class="store-addr">서울 동대문구 테스트로 18 1층</p></div></li></ul>'''
KEY = 'store-directory:paris-dongdaemun'


@pytest.fixture(autouse=True)
def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'paris-dongdaemun', 'provider': 'paris', 'province': '서울', 'district': '동대문구'}]))
    monkeypatch.setattr(settings, 'card_source_archive_dir', str(tmp_path))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)


def mock_client(html=HTML, areas=AREAS):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == 'www.paris.co.kr'
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php')
        if request.url.params['action'] == 'pb_store_get_area':
            return httpx.Response(200, text=areas)
        assert request.url.params['area1'] == '117' and request.url.params['area2'] == '167'
        assert request.url.params['keyword'] == '' and request.url.params['type'] == '#around'
        return httpx.Response(200, text=html)

    return httpx.Client(transport=httpx.MockTransport(respond)), requests


def test_region_observation_archives_all_evidence_and_preserves_timestamps(db):
    client, requests = mock_client()
    with client:
        service.collect_store_directories(db, client=client, trigger='scheduled')
        state = db.get(MapSourceState, KEY)
        assert state.status == 'success' and state.records[0]['official_id'] == '123'
        assert service.collect_store_directories(db, client=client)['sources'][0]['status'] == 'not_due'
        assert len(requests) == 3
        assert service.collect_store_directories(db, client=client, force=True)['sources'][0]['status'] == 'unchanged'
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_observed'))
    bundle = json.loads(Path(event.payload['archive']).read_text(encoding='utf-8'))
    # Reconstruct the observation from immutable content plus the actual DB time.
    receipt = {**bundle['receipt'], 'captured_at': service.aware(event.at).isoformat()}
    rows = paris_capture_rows(bundle['search_html'], receipt, datetime.now(timezone.utc), bundle['areas_json'])
    assert len(rows) == 1 and rows[0]['address'].endswith('1층')
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert db.scalar(select(func.count()).select_from(StoreOffer)) == 0


@pytest.mark.parametrize('html,areas', [('\ufeff\t', AREAS),
    (HTML.replace('data-total-count="1"', 'data-total-count="2"'), AREAS),
    (HTML.replace('서울 동대문구', '서울 성북구'), AREAS),
    (HTML, AREAS.replace('동대문구', '다른구')), ('<ul data-total-count="0"></ul>', AREAS)])
def test_bad_or_disappearing_region_response_preserves_previous_observation(db, html, areas):
    client, _ = mock_client()
    with client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    previous = (state.content_hash, state.records, state.last_success_at)
    client, _ = mock_client(html=html, areas=areas)
    with client:
        service.collect_store_directories(db, client=client, force=True)
    assert state.status == 'error'
    assert (state.content_hash, state.records, state.last_success_at) == previous


def test_unreviewed_regions_and_arbitrary_urls_cannot_be_configured(monkeypatch):
    for item in [
        {'key': 'paris', 'provider': 'paris', 'province': '서울', 'district': '성북구'},
        {'key': 'paris', 'provider': 'paris', 'province': '서울', 'district': '동대문구', 'url': 'https://example.com'}]:
        monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([item]))
        with pytest.raises(ValueError):
            service.configured_directories()


def test_one_brand_failure_does_not_prevent_the_other_brand_observation(db, monkeypatch):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([
        {'key': 'paris-dongdaemun', 'provider': 'paris', 'province': '서울', 'district': '동대문구'},
        {'key': 'outback', 'provider': 'outback', 'query': '청량리'}]))

    def fetch(client, source):
        if source['provider'] == 'paris':
            raise ValueError('official_search_structure_changed')
        return '{"retained":"official fixture"}', [{'official_id': '279'}]

    monkeypatch.setattr(service, 'fetch_directory', fetch)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        result = service.collect_store_directories(db, client=client)
    assert [s['status'] for s in result['sources']] == ['error', 'success']
    assert db.get(MapSourceState, KEY).last_success_at is None
    assert len(db.get(MapSourceState, 'store-directory:outback').records) == 1
