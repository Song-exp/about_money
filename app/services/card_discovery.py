"""Conservative product discovery from reviewed official listing layouts."""
import re
from urllib.parse import parse_qs, parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models.card_collection import CardProductSource
from app.models.map_verification import MapReviewEvent

BC_LISTING_URL = 'https://www.bccard.com/app/card/contents/CardSub.do'
BC_ADAPTER = 'bc_recommendations_v1'
SHINHAN_LISTING_URL = 'https://www.shinhancard.com/pconts/html/landing/searchcard.html'
SHINHAN_ADAPTER = 'shinhan_public_links_v1'
SHINHAN_JOOPJOOP_URL = 'https://www.shinhancard.com/pconts/html/card/apply/credit/1234363_2207.html'

# Only explicit issuer labels count. BC processing/network membership is not issuance.
ISSUER_LABELS = {
    'BC바로': ('bc_baro', 'BC 바로카드'),
    'IBK기업은행': ('ibk', 'IBK기업은행'),
    '하나': ('hana', '하나카드'),
    'NH농협': ('nh', 'NH농협카드'),
    'BNK경남': ('bnk_kyongnam', 'BNK경남은행'),
    'iM뱅크': ('im_bank', 'iM뱅크'),
    'BNK부산': ('bnk_busan', 'BNK부산은행'),
}


def parse_bc_recommendations(content, url):
    from app.services.card_collection import canonical_url
    if canonical_url(url) != BC_LISTING_URL:
        raise ValueError('card_listing_url_mismatch')
    soup = BeautifulSoup(content, 'html.parser')
    if not soup.find(string=re.compile('추천카드')):
        raise ValueError('card_listing_layout_changed')
    products = {}
    excluded = 0
    for link in soup.find_all('a', href=True):
        label = link.get_text(' ', strip=True)
        match = re.fullmatch(r'\[([^\]]+)\]\s*(.{2,150})', label)
        if not match:
            continue
        issuer_label, name = match.groups()
        if issuer_label not in ISSUER_LABELS:
            excluded += 1
            continue
        target = urljoin(url, link['href'])
        parts = urlsplit(target)
        if parts.hostname != 'www.bccard.com' or parts.path not in {
            '/app/card/contents/CreditCardMain.do', '/app/card/contents/CheckCardMain.do',
            '/app/card/app/CreditCardMain.do', '/app/card/app/CheckCardMain.do',
        }:
            excluded += 1
            continue
        code = parse_qs(parts.query).get('gdsno', [''])[0]
        if not re.fullmatch(r'\d{6,12}', code):
            raise ValueError('invalid_discovered_product_code')
        target = canonical_url(target)
        kind = 'credit' if 'CreditCardMain' in parts.path else 'check'
        record = {'namespace': 'bc_catalog', 'code': code, 'name': name.strip(),
                  'product_type': kind, 'url': target, 'issuer': ISSUER_LABELS[issuer_label]}
        if code in products and products[code] != record:
            raise ValueError('conflicting_card_listing_identity')
        products[code] = record
    if not products:
        raise ValueError('card_listing_products_missing')
    if len(products) > 100:
        raise ValueError('card_listing_product_limit')
    return {'products': list(products.values()), 'scope': 'recommendations_only',
            'catalog_complete': False, 'excluded_labels': excluded}


def parse_shinhan_public_links(content, url):
    from app.services.card_collection import canonical_url
    if canonical_url(url) != SHINHAN_LISTING_URL:
        raise ValueError('card_listing_url_mismatch')
    soup = BeautifulSoup(content, 'html.parser')
    products = {}
    excluded = 0
    for link in soup.select('.card_thumb_list_wrap .card_name_wrap a.card_name[href]'):
        name = link.get_text(' ', strip=True)
        parts = urlsplit(urljoin(url, link['href']))
        match = re.fullmatch(r'/pconts/html/card/apply/(credit|check)/(\d{6,8}_\d{4})\.html', parts.path)
        if parts.hostname != 'www.shinhancard.com' or not match or not 2 <= len(name) <= 150:
            raise ValueError('card_listing_identity_unrecognized')
        if any(word in name.lower() for word in ('법인', '사업자', '기업', 'biz')):
            excluded += 1
            continue
        # Remove reviewed campaign attribution, not arbitrary product selectors.
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not {k for k, _ in pairs} <= {'EntryLoc', 'empSeq', 'btnApp'}:
            raise ValueError('unapproved_card_listing_query')
        clean_url = canonical_url(urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')))
        kind, code = match.groups()
        record = {'namespace': 'shinhan', 'code': code, 'name': name,
            'product_type': kind, 'url': clean_url, 'issuer': ('shinhan', '신한카드')}
        if code in products and products[code] != record:
            raise ValueError('conflicting_card_listing_identity')
        products[code] = record
    if not products:
        raise ValueError('card_listing_products_missing')
    if len(products) > 100:
        raise ValueError('card_listing_product_limit')
    return {'products': list(products.values()), 'scope': 'static_public_links',
            'catalog_complete': False, 'excluded_labels': excluded}


def apply_discovery(db, source, snapshot, content, now):
    from app.services.card_collection import register_product, register_source
    adapters = {BC_ADAPTER: parse_bc_recommendations, SHINHAN_ADAPTER: parse_shinhan_public_links}
    if source.adapter_key not in adapters:
        raise ValueError('unsupported_card_listing_adapter')
    parsed = adapters[source.adapter_key](content, source.canonical_url)
    created = 0
    for value in parsed['products']:
        existing = db.scalar(select(CardProductSource).where(
            CardProductSource.source_namespace == value['namespace'],
            CardProductSource.external_product_code == value['code']))
        detail = register_source(db, value['url'], provider_key=source.provider_key, issuer=value['issuer'], now=now)
        product = register_product(db, detail, namespace=value['namespace'], code=value['code'],
            name=value['name'], product_type=value['product_type'], now=now)
        if existing is None:
            created += 1
        latest = db.scalar(select(MapReviewEvent).where(MapReviewEvent.kind == 'card_discovery',
            MapReviewEvent.item_id == product.id).order_by(MapReviewEvent.id.desc()).limit(1))
        if not latest or latest.payload.get('snapshot_id') != snapshot.id:
            db.add(MapReviewEvent(kind='card_discovery', item_id=product.id, action='discovered', at=now,
                payload={'listing_source_id': source.id, 'snapshot_id': snapshot.id,
                    'adapter': source.adapter_key, 'scope': parsed['scope'], 'catalog_complete': False,
                    'source_namespace': value['namespace'], 'external_product_code': value['code'],
                    'product_name': value['name'], 'issuer_key': value['issuer'][0]}))
    return {'new_products': created, 'observed_products': len(parsed['products']),
            'catalog_complete': False, 'discovery_scope': parsed['scope']}


def bootstrap_discovery(db):
    from app.services.card_collection import register_source
    listing = register_source(db, BC_LISTING_URL, provider_key='bc_catalog', kind='listing')
    listing.adapter_key = BC_ADAPTER
    shinhan = register_source(db, SHINHAN_LISTING_URL, provider_key='shinhan', kind='listing',
                             issuer=('shinhan', '신한카드'))
    shinhan.adapter_key = SHINHAN_ADAPTER
    # A separate issuer document, not a workaround for a denied KakaoBank path.
    register_source(db, SHINHAN_JOOPJOOP_URL, provider_key='shinhan', issuer=('shinhan', '신한카드'))
    db.flush()
    return {'listing_sources': 2, 'supplementary_sources': 1, 'catalog_complete': False}
