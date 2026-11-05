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
from app.services.mega_store_identity import mega_candidate


KEY = 'store-directory:mega-khu'
CONFIG = {'key': 'mega-khu', 'provider': 'mega', 'query': '경희', 'branch': '경희대점', 'official_id': '732'}
ROW = {'idx': '732', 'store_name2': '경희대점', 'store_address': '서울특별시 동대문구 경희대로 18 1층 (회기동)',
    'store_lat': '37.59298', 'store_lng': '127.05285'}
HTML = '''<li><a onclick="panTo('서울특별시 동대문구 경희대로 18 ');"><div class="cont_text">
<b>경희대점</b><div class="cont_text_info">서울특별시 동대문구 경희대로 18 , 1층 (회기동) 02-959-4282</div>
</div></a></li>'''


@pytest.fixture(autouse=True)
def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([CONFIG]))
    monkeypatch.setattr(settings, 'card_source_archive_dir', str(tmp_path))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)


def client_for(rows=None, html=HTML):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == 'www.mega-mgccoffee.com'
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        if request.url.path.endswith('store_search.php'):
            assert request.url.params['store_search'] == '경희'
            return httpx.Response(200, text=html)
        assert request.url.params['lat'] == '37.59' and request.url.params['lng'] == '127.06'
        return httpx.Response(200, json={'positions': [ROW] if rows is None else rows})

    return httpx.Client(transport=httpx.MockTransport(respond)), requests


def test_scheduled_branch_requires_both_sources_and_reconstructs_evidence(db):
    nearby = {**ROW, 'idx': '999', 'store_name2': '다른지점'}
    client, requests = client_for(rows=[ROW, nearby])
    with client:
        service.collect_store_directories(db, client=client, trigger='scheduled')
        state = db.get(MapSourceState, KEY)
        assert state.status == 'success' and len(state.records) == 1
        assert state.records[0]['official_id'] == '732' and '1층' in state.records[0]['address']
        assert service.collect_store_directories(db, client=client)['sources'][0]['status'] == 'not_due'
        assert len(requests) == 3
        assert service.collect_store_directories(db, client=client, force=True)['sources'][0]['status'] == 'unchanged'
    event = db.scalar(select(MapReviewEvent).where(MapReviewEvent.action == 'store_directory_observed'))
    evidence = json.loads(Path(event.payload['archive']).read_text(encoding='utf-8'))
    receipt = {**evidence['receipt'], 'captured_at': service.aware(event.at).isoformat()}
    candidate = mega_candidate(evidence['positions_json'], evidence['search_html'], receipt,
        '732', '경희대점', datetime.now(timezone.utc))
    assert candidate['official_id'] == '732'
    assert db.scalar(select(func.count()).select_from(Store)) == 0
    assert db.scalar(select(func.count()).select_from(StoreOffer)) == 0


@pytest.mark.parametrize('rows,html', [([], HTML), ([ROW, ROW], HTML),
    ([{**ROW, 'store_address': ROW['store_address'].replace('1층', '2층')}], HTML),
    ([{**ROW, 'store_lat': 'nan'}], HTML), ([ROW], HTML.replace('경희대점', '다른지점')),
    ([ROW], '\ufeff\t')])
def test_missing_ambiguous_or_changed_branch_preserves_last_good_observation(db, rows, html):
    client, _ = client_for()
    with client:
        service.collect_store_directories(db, client=client)
    state = db.get(MapSourceState, KEY)
    previous = (state.records, state.content_hash, state.last_success_at)
    client, _ = client_for(rows=rows, html=html)
    with client:
        service.collect_store_directories(db, client=client, force=True)
    assert state.status == 'error'
    assert (state.records, state.content_hash, state.last_success_at) == previous


@pytest.mark.parametrize('change', [{'official_id': '999'}, {'branch': '경희대정문점'},
    {'query': '???'}, {'url': 'https://example.com'}])
def test_only_previously_reviewed_branch_mappings_are_configurable(monkeypatch, change):
    monkeypatch.setattr(settings, 'store_collection_sources_json', json.dumps([{**CONFIG, **change}]))
    with pytest.raises(ValueError):
        service.configured_directories()
