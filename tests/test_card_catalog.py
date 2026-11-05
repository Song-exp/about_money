from datetime import datetime, timezone

from app.models.card import Card
from app.models.card_collection import CardCollectionSource, CardIssuer, CardProductSource, CardSourceSnapshot
from app.services.card_catalog import build_catalog, card_image, clean_name


def test_shinhan_meta_path_is_corrected_because_it_returns_404():
    html = '<meta property="og:image" content="https://www.shinhancard.com/static/images/card/plate/AUAARH_00_v_f_s.png" />'
    assert card_image(html, 'https://www.shinhancard.com/pconts/html/card/apply/credit/1.html') == \
        'https://www.shinhancard.com/pconts/static/images/card/plate/AUAARH_00_v_f_s.png'


def test_hana_ignores_recommended_cards_listed_before_this_card():
    html = '''"image": {"url": "https://www.hanacard.co.kr/ATTACH/NEW_HOMEPAGE/images/cardinfo/card_img/14955.gif"}
    cardUnit = {LIST_IMG_TYPE_IMG: '/ATTACH/NEW_HOMEPAGE/images/cardinfo/card_img/15439.gif', CD_NM: 'Day'};'''
    assert card_image(html, 'https://www.hanacard.co.kr/OPI41000000D.web?CD_PD_SEQ=1') == \
        'https://www.hanacard.co.kr/ATTACH/NEW_HOMEPAGE/images/cardinfo/card_img/15439.gif'


def test_unknown_site_is_not_guessed():
    assert card_image('<img src="https://example.com/card.png">', 'https://example.com/card') is None


def test_clean_name_removes_info_mark():
    assert clean_name('신한카드 YOLO ⓘ') == '신한카드 YOLO'


def product(db, tmp_path, name, code, type_='credit', card_id=None, html=None):
    now = datetime.now(timezone.utc)
    issuer = db.query(CardIssuer).first()
    if issuer is None:
        issuer = CardIssuer(issuer_key='shinhan', name='신한카드')
        db.add(issuer)
        db.flush()
    source = CardCollectionSource(source_key=f'source-{code}', issuer_id=issuer.id, provider_key='shinhan',
                                  source_kind='detail', canonical_url='https://www.shinhancard.com/pconts/html/card/1.html',
                                  adapter_key='generic', enabled=True, schedule_policy={}, failure_count=0)
    db.add(source)
    db.flush()
    if html is not None:
        (tmp_path / f'{code}.html').write_text(html, encoding='utf-8')
        db.add(CardSourceSnapshot(source_id=source.id, fetched_at=now, raw_sha256=code.ljust(64, '0'),
                                  semantic_sha256=code.ljust(64, '0'), raw_object_ref=f'{code}.html', extracted_text='',
                                  mime_type='text/html', parser_version='test', completeness='complete'))
    db.add(CardProductSource(card_id=card_id, source_id=source.id, issuer_id=issuer.id, source_namespace='shinhan',
                             external_product_code=code, product_name=name, product_type=type_, review_state='pending',
                             first_seen_at=now, last_seen_at=now))
    db.commit()


def test_catalog_adds_pending_candidates_with_image_and_is_repeatable(db, tmp_path):
    plate = '"https://www.shinhancard.com/static/images/card/plate/ABC_00_v_f_s.png"'
    product(db, tmp_path, '신한카드 Mr.Life ⓘ', 'mrlife', html=plate)
    product(db, tmp_path, '이미지 없는 카드', 'noimage')
    assert build_catalog(db, tmp_path) == {'created': 2, 'images': 1, 'total': 2}
    assert build_catalog(db, tmp_path)['created'] == 0
    card = db.query(Card).filter(Card.card_name == '신한카드 Mr.Life').one()
    assert card.issuer == '신한카드' and card.card_type == 'credit'
    assert card.image_url.endswith('/pconts/static/images/card/plate/ABC_00_v_f_s.png')
    assert db.query(CardProductSource).filter(CardProductSource.card_id.is_not(None)).count() == 0


def test_catalog_updates_linked_card_and_guesses_check_type(db, tmp_path):
    linked = Card(card_key='linked', card_name='KB국민 펭수 노리 체크카드', issuer='KB국민카드')
    other = Card(card_key='seeded', card_name='하나 나라사랑카드(체크)', issuer='하나카드')
    db.add_all([linked, other])
    db.commit()
    product(db, tmp_path, '펭수', 'pengsoo', type_='unclassified', card_id=linked.id)
    assert build_catalog(db, tmp_path)['created'] == 0
    assert linked.card_type == 'check' and other.card_type == 'check'


def test_options_expose_catalog_fields_and_hide_bundle_entry(client, db):
    db.add_all([Card(card_key='real', card_name='실제 카드', issuer='신한카드', card_type='credit', image_url='https://example.com/a.png'),
                Card(card_key='KB국민(나라사랑·펭수 노리)', card_name='KB국민(나라사랑·펭수 노리)')])
    db.commit()
    cards = client.get('/meta/options').json()['cards']
    assert [(c['card_name'], c['card_type'], c['image_url']) for c in cards] == [('실제 카드', 'credit', 'https://example.com/a.png')]
