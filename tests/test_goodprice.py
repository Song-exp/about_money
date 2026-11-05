import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.local_benefit import LocalBenefitMerchant
from app.models.store import StoreCategory
from app.services.goodprice import apply_records, latest_dataset, records, sync_goodprice

DISTRICT = {'서울특별시 동대문구'}


def row(name='테스트식당', address='서울특별시 동대문구 회기로 1', type_='한식', **extra):
    return {'시도': '서울특별시', '시군': '동대문구', '업소명': name, '주소': address, '업종': type_,
            '메뉴1': '백반', '가격1': 7000, '메뉴2': None, '가격2': None, '메뉴3': None, '가격3': None,
            '메뉴4': None, '가격4': None, **extra}


def test_latest_dataset_picks_newest_quarter_not_last_listed():
    spec = {'paths': {'/new': {'get': {'summary': '현황_20260930'}},
                      '/old': {'get': {'summary': '현황_20190903'}},
                      '/unknown': {'get': {'summary': '날짜 없음'}}}}
    assert latest_dataset(spec) == ('/new', '2026-09-30')
    with pytest.raises(ValueError):
        latest_dataset({'paths': {'/unknown': {'get': {'summary': '날짜 없음'}}}})


def test_records_keep_only_food_in_district_with_a_priced_menu():
    rows = [row(), row(name='미용실', type_='미용업'), row(name='타구', **{'시군': '성북구'}),
            row(name='가격없음', **{'가격1': None}), row(name='카페', **{'메뉴1': '아메리카노'})]
    result = records(rows, DISTRICT)
    assert [r['name'] for r in result] == ['테스트식당', '카페']
    assert result[0]['category'] == StoreCategory.RESTAURANT
    assert result[1]['category'] == StoreCategory.CAFE


def test_apply_uses_first_menu_as_price_and_records_source_date(db):
    rows = records([row(**{'메뉴2': '공기밥', '가격2': 1000})], DISTRICT)
    apply_records(db, rows, '2026-09-30')
    saved = db.scalar(select(LocalBenefitMerchant))
    assert saved.discount_value == 7000
    assert saved.benefit_text == '백반 7,000원 · 공기밥 1,000원'
    assert '2026-09-30' in saved.conditions and saved.source_type == 'goodprice'


def test_missing_store_is_deactivated_and_coordinates_survive_refresh(db):
    rows = records([row(), row(name='폐업식당', address='서울특별시 동대문구 회기로 2')], DISTRICT)
    apply_records(db, rows, '2026-06-30')
    kept = db.scalar(select(LocalBenefitMerchant).where(LocalBenefitMerchant.merchant_name == '테스트식당'))
    kept.lat, kept.lng = 37.59, 127.05
    db.commit()
    apply_records(db, rows[:1], '2026-09-30')
    assert kept.lat == 37.59
    gone = db.scalar(select(LocalBenefitMerchant).where(LocalBenefitMerchant.merchant_name == '폐업식당'))
    assert gone.is_active is False


def test_sudden_record_drop_stops_instead_of_wiping_the_map(db):
    rows = records([row(name=f'식당{i}', address=f'서울특별시 동대문구 회기로 {i}') for i in range(12)], DISTRICT)
    apply_records(db, rows, '2026-06-30')
    with pytest.raises(ValueError, match='requires_review'):
        apply_records(db, rows[:3], '2026-09-30')


def test_sync_reads_spec_then_every_page(db, monkeypatch):
    monkeypatch.setattr(settings, 'data_go_kr_service_key', 'test-key')
    monkeypatch.setattr(settings, 'goodprice_districts', '서울특별시 동대문구')
    pages = {'1': [row(name='첫째', address='서울특별시 동대문구 회기로 1')],
             '2': [row(name='둘째', address='서울특별시 동대문구 회기로 2')]}

    def handler(request):
        if request.url.host == 'infuser.odcloud.kr':
            return httpx.Response(200, json={'paths': {'/latest': {'get': {'summary': '현황_20260930'}}}})
        assert request.url.params['serviceKey'] == 'test-key'
        return httpx.Response(200, json={'data': pages[request.url.params['page']], 'totalCount': 2})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert sync_goodprice(db, client=client)['records'] == 2


def test_sync_requires_key(db, monkeypatch):
    monkeypatch.setattr(settings, 'data_go_kr_service_key', '')
    with pytest.raises(ValueError, match='goodprice_key_missing'):
        sync_goodprice(db)


def test_public_coordinates_reach_the_map_and_unlocated_rows_stay_off(client, db):
    apply_records(db, records([row(), row(name='좌표없음', address='서울특별시 동대문구 회기로 2')], DISTRICT), '2026-09-30')
    located = db.scalar(select(LocalBenefitMerchant).where(LocalBenefitMerchant.merchant_name == '테스트식당'))
    located.lat, located.lng = 37.5966, 127.0520
    db.commit()
    page = client.get('/stores/local-benefits', params={'program': 'goodprice', 'lat': 37.5966, 'lng': 127.0520}).json()
    assert [item['merchant_name'] for item in page['items']] == ['테스트식당']
    assert page['unlocated_count'] == 1


def test_expired_local_benefits_not_returned(client, db):
    from datetime import date, timedelta
    apply_records(db, records([row()], DISTRICT), '2026-09-30')
    db.scalar(select(LocalBenefitMerchant)).valid_to = date.today() - timedelta(days=1)
    db.commit()
    assert client.get('/stores/local-benefits', params={'program': 'goodprice'}).json()['total'] == 0
