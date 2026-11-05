"""수집한 공식 상품 후보를 사용자가 고를 수 있는 카드 목록으로 올린다.

혜택 승인과는 별개다. 이름·발급사·종류·이미지 주소만 채운다. 이미지는 복사하지 않고
공식 주소를 저장해 브라우저가 직접 불러온다.
"""
import re
from pathlib import Path
from urllib.parse import urljoin

from sqlalchemy import select

from app.models.card import Card
from app.models.card_collection import CardCollectionSource, CardIssuer, CardProductSource, CardSourceSnapshot
from app.services.card_collection import ARCHIVE_ROOT

IMAGE_PATTERNS = {
    'www.shinhancard.com': r'https://www\.shinhancard\.com(?:/pconts)?/static/images/card/plate/[A-Za-z0-9_]+\.(?:png|gif)',
    'card.kbcard.com': r'https://img1\.kbcard\.com/ST/img/cxc/kbcard/upload/img/product/[A-Za-z0-9_]+\.png',
    # 페이지 앞쪽은 다른 카드 추천 목록이다. 이 카드의 이미지는 cardUnit에만 있다.
    'www.hanacard.co.kr': r"LIST_IMG_TYPE_IMG:\s*'([^']+\.(?:png|gif|jpe?g))'",
    'www.tossbank.com': r'https://static\.toss\.im/(?:3d/tossbank-cards-2|illusts/img-kpass-squre)\.png',
}


def card_image(html, source_url):
    """공식 페이지에서 카드 이미지 주소를 찾는다. 모르는 사이트는 추측하지 않는다."""
    host = re.sub(r'^https?://', '', source_url).split('/')[0]
    pattern = IMAGE_PATTERNS.get(host)
    match = re.search(pattern, html) if pattern else None
    if not match:
        return None
    url = urljoin(source_url, match[match.lastindex or 0])
    # 신한 메타 태그의 /static 주소는 404다. 실제 파일은 /pconts/static 아래에 있다.
    if host == 'www.shinhancard.com' and '/pconts/' not in url:
        url = url.replace('/static/', '/pconts/static/', 1)
    return url


def clean_name(name):
    return re.sub(r'\s*ⓘ\s*', '', name).strip()


def guess_type(name):
    return 'check' if '체크' in name else None


def latest_html(db, source_id, archive_root):
    snapshot = db.scalar(select(CardSourceSnapshot).where(CardSourceSnapshot.source_id == source_id)
                         .order_by(CardSourceSnapshot.id.desc()).limit(1))
    path = Path(archive_root) / snapshot.raw_object_ref if snapshot else None
    if not path or not path.is_file() or 'html' not in snapshot.mime_type:
        return ''
    return path.read_bytes().decode('utf-8', 'ignore')


def build_catalog(db, archive_root=ARCHIVE_ROOT):
    created = images = 0
    for product in db.scalars(select(CardProductSource).order_by(CardProductSource.id)).all():
        key = f'{product.source_namespace}:{product.external_product_code}'[:100]
        card = db.get(Card, product.card_id) if product.card_id else db.scalar(select(Card).where(Card.card_key == key))
        if card is None:
            issuer = db.get(CardIssuer, product.issuer_id) if product.issuer_id else None
            card = Card(card_key=key, card_name=clean_name(product.product_name), issuer=issuer.name if issuer else None)
            db.add(card)
            created += 1
        card.card_type = product.product_type if product.product_type in ('credit', 'check') else guess_type(card.card_name)
        source = db.get(CardCollectionSource, product.source_id)
        image = card_image(latest_html(db, source.id, archive_root), source.canonical_url)
        if image:
            card.image_url = image
            images += 1
    for card in db.scalars(select(Card).where(Card.card_type.is_(None))).all():
        card.card_type = guess_type(card.card_name)
    db.commit()
    return {'created': created, 'images': images, 'total': len(db.scalars(select(Card)).all())}
