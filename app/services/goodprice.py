"""행정안전부 착한가격업소 공공데이터를 가성비 식당으로 적재한다.

데이터는 분기마다 새 주소로 올라온다. 명세에서 가장 최근 기준일을 골라 받는다.
좌표가 없으므로 상가(상권)정보의 같은 도로명주소 좌표를 쓴다.
"""
import hashlib
import re

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.models.local_benefit import LocalBenefitMerchant
from app.models.store import StoreCategory

SPEC_URL = 'https://infuser.odcloud.kr/oas/docs?namespace=3045247/v1'
API_BASE = 'https://api.odcloud.kr/api'
SOURCE_PAGE = 'https://www.data.go.kr/data/3045247/fileData.do'
FOOD_TYPES = {'한식', '중식', '일식', '양식', '기타요식업'}
CAFE_MENU = re.compile('아메리카노|커피|라떼')
BAKERY_MENU = re.compile('꽈배기|빵|카스테라|펑리수|인절미|강정')
STORE_API = 'https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInDong'
# ponytail: 수집 지역이 늘면 코드를 추가한다. 많아지면 baroApi 조회로 바꾼다.
DISTRICT_CODES = {'서울특별시 동대문구': '11230'}
ROAD_ADDRESS = re.compile(r'([가-힣0-9]+(?:로|길))\s*(\d+(?:-\d+)?)')


def latest_dataset(spec):
    """(경로, 기준일). 요약 끝의 날짜로 가장 최근 데이터를 고른다."""
    dated = []
    for path, operations in spec['paths'].items():
        match = re.search(r'_(\d{8})$', operations['get']['summary'])
        if match:
            dated.append((match[1], path))
    if not dated:
        raise ValueError('goodprice_spec_changed')
    day, path = max(dated)
    return path, f'{day[:4]}-{day[4:6]}-{day[6:]}'


def fetch_rows(client, path, key):
    rows, page = [], 1
    while True:
        response = client.get(API_BASE + path, params={'page': page, 'perPage': 1000, 'serviceKey': key})
        response.raise_for_status()
        body = response.json()
        rows += body['data']
        if not body['data'] or len(rows) >= body['totalCount']:
            return rows
        page += 1


def menus(row):
    """가격이 있는 메뉴만. 원본 순서를 유지해 첫 메뉴를 대표 메뉴로 쓴다."""
    result = []
    for index in range(1, 5):
        name, price = row.get(f'메뉴{index}'), row.get(f'가격{index}')
        if isinstance(name, str) and name.strip() and isinstance(price, (int, float)) and price > 0:
            result.append((name.strip(), int(price)))
    return result


def records(rows, districts):
    result = {}
    for row in rows:
        name, address = (row.get('업소명') or '').strip(), (row.get('주소') or '').strip()
        items = menus(row)
        if f"{row.get('시도')} {row.get('시군')}" not in districts or row.get('업종') not in FOOD_TYPES:
            continue
        if not name or not address or not items:
            continue
        key = 'goodprice:' + hashlib.sha256(f'{name}|{address}'.encode()).hexdigest()[:24]
        category = StoreCategory.RESTAURANT
        if any(CAFE_MENU.search(menu) for menu, _ in items):
            category = StoreCategory.CAFE
        elif any(BAKERY_MENU.search(menu) for menu, _ in items):
            category = StoreCategory.BAKERY
        result[key] = {'key': key, 'name': name, 'address': address, 'menus': items,
                       'category': category}
    return list(result.values())


def road_key(address):
    """'서울특별시 동대문구 이문로25길 39 (이문동) 1층' -> '이문로25길 39'."""
    match = ROAD_ADDRESS.search(address or '')
    return f'{match[1]} {match[2]}' if match else None


def fetch_coordinates(client, key, code):
    """도로명주소별 좌표. 층과 호만 다른 업소는 같은 건물 좌표를 공유한다."""
    result, page, seen = {}, 1, 0
    while True:
        response = client.get(STORE_API, params={'serviceKey': key, 'pageNo': page, 'numOfRows': 1000,
                                                 'divId': 'signguCd', 'key': code, 'type': 'json'})
        response.raise_for_status()
        body = response.json().get('body') or {}
        items = body.get('items') or []
        for item in items:
            address = road_key(item.get('rdnmAdr'))
            if address and item.get('lat') and item.get('lon'):
                result.setdefault(address, (float(item['lat']), float(item['lon'])))
        seen += len(items)
        if not items or seen >= body.get('totalCount', 0):
            return result
        page += 1


def attach_coordinates(db, coordinates):
    rows = db.scalars(select(LocalBenefitMerchant).where(LocalBenefitMerchant.source_type == 'goodprice',
                                                         LocalBenefitMerchant.is_active.is_(True))).all()
    located = 0
    for row in rows:
        point = coordinates.get(road_key(row.address))
        if point:
            row.lat, row.lng = point
        located += row.lat is not None
    db.commit()
    return located


def apply_records(db, rows, as_of):
    owned = db.scalars(select(LocalBenefitMerchant).where(LocalBenefitMerchant.source_type == 'goodprice')).all()
    active = [row for row in owned if row.is_active]
    if len(active) >= 10 and len(rows) < len(active) / 2:
        raise ValueError('goodprice_record_count_dropped_requires_review')
    by_key = {row.source_key: row for row in owned}
    for record in rows:
        row = by_key.get(record['key'])
        if row is None:
            row = LocalBenefitMerchant(source_key=record['key'])
            db.add(row)
        menu_text = ' · '.join(f'{menu} {price:,}원' for menu, price in record['menus'])
        values = {'merchant_name': record['name'], 'address': record['address'],
                  'search_query': f"{record['name']} {record['address']}", 'category': record['category'],
                  'category_label': '가성비 식당', 'source_type': 'goodprice', 'programs': ['goodprice'],
                  'credentials': [], 'requires': '공공데이터 등록 가격이며 실제 매장 가격을 확인해 주세요.',
                  'benefit_text': menu_text, 'discount_type': 'menu_price',
                  'discount_value': record['menus'][0][1],
                  'conditions': f'행정안전부 착한가격업소 현황 · {as_of} 기준',
                  'source_url': SOURCE_PAGE, 'confidence': 'public_data', 'is_active': True}
        for name, value in values.items():
            setattr(row, name, value)
    seen = {record['key'] for record in rows}
    for row in active:
        if row.source_key not in seen:
            row.is_active = False
    db.commit()
    return {'as_of': as_of, 'records': len(rows), 'deactivated': len([r for r in active if r.source_key not in seen])}


def sync_goodprice(db, client=None):
    key = settings.data_go_kr_service_key
    if not key:
        raise ValueError('goodprice_key_missing')
    districts = {item.strip() for item in settings.goodprice_districts.split(',') if item.strip()}
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.map_source_timeout_sec)
    try:
        spec = client.get(SPEC_URL)
        spec.raise_for_status()
        path, as_of = latest_dataset(spec.json())
        result = apply_records(db, records(fetch_rows(client, path, key), districts), as_of)
        coordinates = {}
        for district in districts:
            coordinates.update(fetch_coordinates(client, key, DISTRICT_CODES[district]))
        return {**result, 'located': attach_coordinates(db, coordinates)}
    finally:
        if owns_client:
            client.close()
