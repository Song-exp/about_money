from app.services.map_verification import location_address_key, normalize_location_text
from app.scripts.collect_map_official_sources import resolve_sources


def test_floor_and_shop_suffix_preserve_building_identity():
    assert location_address_key('서울특별시 동대문구 장한로28가길 1 히요꼬의주먹밥집 1층') == location_address_key('서울 동대문구 장한로28가길 1')
    assert location_address_key('서울 동대문구 왕산로9길 6 1층 푸드김밥') == location_address_key('서울 동대문구 왕산로9길 6')


def test_different_house_numbers_or_unknown_suffix_do_not_match():
    assert location_address_key('서울 동대문구 왕산로9길 6-1 1층') != location_address_key('서울 동대문구 왕산로9길 6')
    assert location_address_key('서울 동대문구 왕산로9길 6 뒤편') != location_address_key('서울 동대문구 왕산로9길 6')
    assert normalize_location_text('동명 식당') != normalize_location_text('동명 식당 다른지점')


def test_source_catalog_resolves_by_card_key_not_deployment_ids(db, seed_stores):
    from app.models.card import Card
    card = db.get(Card, seed_stores['card_id'])
    catalog = {'sources': [{'card_key': card.card_key, 'url': 'https://card.kbcard.com/example'}]}
    targets, missing = resolve_sources(db, catalog)
    assert targets and not missing
    assert all(row['kind'] == 'card' for row in targets)


def test_approval_rejects_missing_or_fabricated_source_quotes(client, db, seed_stores):
    from tests.test_map_verification import captured, RULES, QUOTES
    item, evidence, _ = captured(db, seed_stores)
    body = {'expected_hash': evidence.content_hash, 'action': 'approved', 'reviewer': '검토자',
            'reason': '원문 조건 대조 완료 기록', 'indefinite': True, 'rules': RULES}
    headers = {'X-Admin-Token': 'test-admin-token'}
    url = f'/admin/map/review/card/{item.id}'
    assert client.post(url, json=body, headers=headers).status_code == 422
    assert client.post(url, json={**body, 'evidence_quotes': {**QUOTES, 'value': '존재하지 않는 99% 할인'}}, headers=headers).status_code == 422
    assert client.post(url, json={**body, 'evidence_quotes': QUOTES, 'rules': {**RULES, 'value': 99}}, headers=headers).status_code == 422
    assert client.post(url, json={**body, 'evidence_quotes': QUOTES}, headers=headers).status_code == 200
