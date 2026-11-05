from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.store import Store, StoreCategory
from app.models.map_verification import MapLocation, MapReviewEvent
from app.scripts.review_store_addresses import apply_review, parse_cj_identity
from app.scripts.review_store_addresses import parse_starbucks_identity, source_provider
from app.scripts.review_store_addresses import parse_tlj_identity
from app.services.map_verification import location_hash, verified_location


REVIEW = {'brand': '올리브영', 'branch': '경희대점', 'expected_old_address': '회기로 199',
          'new_address': '서울특별시 동대문구 경희대로 2',
          'source_url': 'https://www.cjone.com/cjmmobile/store/storeAreaDetail.do?no=D008'}
HTML = '<div class="store_info"><h2 class="h2_tit">올리브영<br>경희대점</h2><p class="tit_info">서울특별시 동대문구 경희대로 2</p></div>'


def make_store(db):
    row = Store(brand='올리브영', branch='경희대점', category=StoreCategory.HNB,
                address='회기로 199', lat=37.59, lng=127.05)
    db.add(row)
    db.commit()
    return row


def test_correction_invalidates_old_pin_and_keeps_source_history(db):
    row = make_store(db)
    now = datetime.now(timezone.utc)
    db.add(MapLocation(kind='store', item_id=row.id, identity_hash=location_hash(row),
        place_id='123', lat=37.59, lng=127.05, source_url='https://place.map.kakao.com/123',
        reviewer='reviewer', checked_at=now))
    db.commit()
    assert verified_location(db, 'store', row)
    assert apply_review(db, REVIEW, HTML, now)['status'] == 'address_corrected'
    db.commit()
    assert verified_location(db, 'store', row) is None
    event = db.scalar(select(MapReviewEvent))
    assert event.payload['before']['address'] == '회기로 199'
    assert event.payload['official_address'] == REVIEW['new_address']
    assert len(event.payload['source_hash']) == 64
    assert apply_review(db, REVIEW, HTML, now)['status'] == 'unchanged'


@pytest.mark.parametrize('html', [HTML.replace('경희대점', '외대점'),
                                    HTML.replace('경희대로 2', '경희대로 20')])
def test_changed_source_identity_or_address_rejected(db, html):
    row = make_store(db)
    with pytest.raises(ValueError):
        apply_review(db, REVIEW, html, datetime.now(timezone.utc))
    assert row.address == '회기로 199'


def test_stale_review_cannot_overwrite_new_address(db):
    row = make_store(db)
    row.address = '서울 동대문구 다른길 3'
    with pytest.raises(ValueError, match='changed_since_review'):
        apply_review(db, REVIEW, HTML, datetime.now(timezone.utc))


def test_unrelated_footer_addresses_cannot_supply_store_identity():
    with pytest.raises(ValueError):
        parse_cj_identity('<h2>올리브영 경희대점</h2><footer>서울특별시 동대문구 경희대로 2</footer>')
    with pytest.raises(ValueError):
        parse_cj_identity(HTML + HTML)


def test_reseed_preserves_reviewed_address_and_coordinates(db, monkeypatch):
    from app.scripts import seed_category_stores as seeder
    row = make_store(db)
    apply_review(db, REVIEW, HTML, datetime.now(timezone.utc))
    db.commit()
    monkeypatch.setattr(seeder, '_STORES', [{'brand': row.brand, 'branch': row.branch,
        'category': row.category, 'address': '오래된 주소', 'lat': 1, 'lng': 2, 'mark': 'O', 'color': '#abc'}])
    monkeypatch.setattr(seeder, 'init_db', lambda: None)
    seeder.seed()
    db.refresh(row)
    assert row.address == REVIEW['new_address']
    assert (row.lat, row.lng) == (37.59, 127.05)


STARBUCKS_REVIEW = {'brand': '스타벅스', 'branch': '경희대점', 'official_name': '경희대',
    'official_id': '9551', 'source_url': 'https://www.starbucks.co.kr/store/store_map.do?in_biz_cd=9551'}


def test_official_starbucks_code_distinguishes_nearby_similar_branches():
    import json
    rows = [{'s_name': '경희대삼거리', 's_biz_code': '3391', 'doro_address': '다른 주소'},
            {'s_name': '경희대', 's_biz_code': '9551', 'doro_address': '서울 동대문구 경희대로 16-1'}]
    assert parse_starbucks_identity(json.dumps({'list': rows}), STARBUCKS_REVIEW) == (
        '스타벅스 경희대점', '서울 동대문구 경희대로 16-1')


@pytest.mark.parametrize('rows', [[],
    [{'s_name': '경희대삼거리', 's_biz_code': '9551', 'doro_address': '다른 주소'}],
    [{'s_name': '경희대', 's_biz_code': '9551'}],
    [{'s_name': '경희대', 's_biz_code': '9551', 'doro_address': '주소'}] * 2])
def test_starbucks_missing_changed_or_duplicate_identity_rejected(rows):
    import json
    with pytest.raises(ValueError):
        parse_starbucks_identity(json.dumps({'list': rows}), STARBUCKS_REVIEW)


@pytest.mark.parametrize('url', [
    'https://www.starbucks.co.kr/store/store_map.do?in_biz_cd=3391',
    'https://www.starbucks.co.kr.example.com/store/store_map.do?in_biz_cd=9551',
    'http://www.starbucks.co.kr/store/store_map.do?in_biz_cd=9551'])
def test_official_source_url_must_bind_reviewed_store_code(url):
    with pytest.raises(ValueError):
        source_provider({**STARBUCKS_REVIEW, 'source_url': url})


TLJ_REVIEW = {'brand': '뚜레쥬르', 'branch': '회기역점', 'official_name': '회기역',
    'official_id': '1109908', 'source_url': 'https://www.tlj.co.kr/store/search.asp?keyword=%C8%B8%B1%E2&store_cd=1109908'}
TLJ_HTML = '<span id="listName_1109908">뚜레쥬르 회기역</span><span id="listAddrD_1109908">서울특별시 동대문구 이문로 37 1층 102호</span>'


def test_tlj_store_code_pairs_name_and_address_without_footer():
    name, address = parse_tlj_identity(TLJ_HTML + '<footer>서울 중구 마른내로 34</footer>', TLJ_REVIEW)
    assert name == '뚜레쥬르 회기역점'
    assert address == '서울특별시 동대문구 이문로 37 1층 102호'
    assert source_provider(TLJ_REVIEW) == 'tlj'


@pytest.mark.parametrize('html', [TLJ_HTML.replace('회기역', '다른지점'),
    TLJ_HTML.replace('listAddrD_1109908', 'listAddrD_1109909'), TLJ_HTML + TLJ_HTML])
def test_tlj_wrong_branch_missing_address_or_duplicate_rejected(html):
    with pytest.raises(ValueError):
        parse_tlj_identity(html, TLJ_REVIEW)


def test_tlj_source_code_and_brand_must_match_review():
    for review in ({**TLJ_REVIEW, 'official_id': '1109909'}, {**TLJ_REVIEW, 'brand': '다른 브랜드'}):
        with pytest.raises(ValueError):
            source_provider(review)
