"""Product-specific leaflet provenance and conservative cross-source checks."""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardSourceSnapshot
from app.models.card_document_text import CardDocumentText
from app.services.card_benefit_parsers import HANA_DAY_URL, DEEP_OIL_URL, Quotes, money, between
from app.services.shinhan_disclosure import DEEP_OIL_DISCLOSURE_URL
from app.services.toss_kpass_document import KPASS_URL,KPASS_TEXT_PARSER,parse_kpass_document
from app.services.card_linked_documents import DOCUMENT_URLS
from app.services.map_verification import aware

DAY_LEAFLET_URL = 'https://m.hanacard.co.kr/leaflet/15/15439_20250626.pdf'
TEXT_PARSER = 'card_pdfium_text_v1'


def prepare_document_text(db, source, snapshot, content, *, now=None):
    if source.canonical_url not in {DAY_LEAFLET_URL, DEEP_OIL_DISCLOSURE_URL,KPASS_URL,*DOCUMENT_URLS}:
        return None
    digest = hashlib.sha256(content).hexdigest()
    if snapshot.source_id != source.id or digest != snapshot.raw_sha256:
        raise ValueError('supporting_document_integrity_failure')
    parser=KPASS_TEXT_PARSER if source.canonical_url==KPASS_URL else TEXT_PARSER
    existing = db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id == snapshot.id,
        CardDocumentText.parser_version == parser))
    if existing:
        return existing
    if source.canonical_url==KPASS_URL:
        parsed=parse_kpass_document(content)
        row=CardDocumentText(snapshot_id=snapshot.id,parser_version=parser,raw_sha256=digest,
            text_sha256=parsed['text_sha256'],text=parsed['text'],created_at=now or datetime.now(timezone.utc))
        db.add(row)
        db.flush()
        return row
    if not content.startswith(b'%PDF'):
        raise ValueError('supporting_document_not_pdf')
    import pypdfium2 as pdfium
    texts = []
    with pdfium.PdfDocument(content) as document:
        if not 0 < len(document) <= 100:
            raise ValueError('supporting_document_page_limit')
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    texts.append(textpage.get_text_range())
                finally:
                    textpage.close()
            finally:
                page.close()
    text = re.sub(r'\s+', ' ', ' '.join(texts)).strip()
    if not 100 <= len(text) <= 500000 or re.search(r'/uni[0-9A-Fa-f]{4}|\(cid:\d+\)', text):
        raise ValueError('supporting_document_unreadable')
    row = CardDocumentText(snapshot_id=snapshot.id, parser_version=TEXT_PARSER,
        raw_sha256=digest, text_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text,
        created_at=now or datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    return row


def parse_day_leaflet(text):
    q = Quotes(text)
    q.find('identity', r'「토스뱅크 하나카드 Day」는 토스뱅크와 제휴하여 만든 카드입니다\.')
    q.find('spend_tiers', r'50만 이상 100만 이상')
    total = q.find('integrated_cap', r'통합 월 할인 한도 ([\d,]+(?:만|천)?원) ([\d,]+(?:만|천)?원)')
    rules = {}
    for key, label, next_label in [('coffee', '커피', '쇼핑'), ('shopping', '쇼핑', '보험')]:
        row = q.find(key, label + r' (?!:)(.{1,200}?) (\d+)% ([\d,]+(?:만|천)?원) ([\d,]+(?:만|천)?원)(?= ' + next_label + ')')
        rules[key] = {'brands': [b.strip() for b in row[1].split(',')], 'rate_bps': int(row[2]) * 100,
            'caps': [money(row[3]), money(row[4])]}
    exclusions = between(text, '할인 제외 대상 ', ' 연회비 청구 및 반환기준')
    q.add('discount_exclusions', exclusions)
    return {'rules': rules, 'integrated_caps': [money(total[1]), money(total[2])],
        'spend_thresholds': [500000, 1000000], 'discount_exclusions': exclusions,
        'evidence': list(q.evidence.values())}


def compare_day(payload, source_text, leaflet_text):
    """Partial field comparison; an agreement is never blanket policy approval."""
    leaflet = parse_day_leaflet(leaflet_text)
    conflicts, checked = [], []
    def check(field, primary, secondary):
        checked.append(field)
        if primary != secondary:
            conflicts.append({'field': field, 'detail_value': primary, 'leaflet_value': secondary})
    for rule in payload['rules']:
        key = rule['rule_key']
        other = leaflet['rules'][key]
        brands = other['brands']
        if key == 'shopping':
            if '토스쇼핑' not in brands:
                raise ValueError('leaflet_online_scope_unresolved')
            brands = [b for b in brands if b != '토스쇼핑']
        check(key + '.brands', [re.sub(r'\s+', '', b) for b in rule['scope']['brands']],
            [re.sub(r'\s+', '', b) for b in brands])
        check(key + '.rate_bps', rule['rate_bps'], other['rate_bps'])
        group = next(g for g in payload['limit_groups'] if g['group_key'] == key)
        check(key + '.caps', [group['caps_by_spend_tier_krw'][tier] for tier in ('standard', 'higher')], other['caps'])
    integrated = next(g for g in payload['limit_groups'] if g['group_key'] == 'program_total')
    check('integrated_caps', [integrated['caps_by_spend_tier_krw'][tier] for tier in ('standard', 'higher')], leaflet['integrated_caps'])
    check('spend_thresholds', [tier['min_prior_spend_krw'] for tier in payload['spend_tiers']], leaflet['spend_thresholds'])
    primary_exclusions = payload['policy_raw'].split('[할인 제외 대상] ', 1)[1]
    for token in ('아파트관리비', '국민연금', '고용보험', '산재보험', '건강보험'):
        check('discount_exclusions.' + token, token in primary_exclusions, token in leaflet['discount_exclusions'])
    start = source_text.find(primary_exclusions)
    if start < 0:
        raise ValueError('primary_exclusion_evidence_missing')
    return {'status': 'conflict' if conflicts else 'partial_match', 'checked_fields': checked, 'conflicts': conflicts,
        'primary_evidence': {'quote': primary_exclusions, 'start_offset': start, 'end_offset': start + len(primary_exclusions)},
        'leaflet_evidence': leaflet['evidence'], 'unresolved': ['rounding_policy', 'all_policy_clauses', 'document_applicability']}


def compare_deep_oil(payload, source_text, leaflet_text):
    """Compare the living rules; report ambiguity rather than repair PDF wording."""
    q = Quotes(leaflet_text)
    q.find('identity', r'신한카드 Deep Oil')
    rules = {}
    for key, pattern in [('convenience', r'(GS25, CU) (\d+)% 결제일 할인'),
                         ('coffee', r'(스타벅스, 이디야) (\d+)% 결제일 할인')]:
        match = q.find(key, pattern)
        rules[key] = {'brands': match[1].split(', '), 'rate_bps': int(match[2])*100}
    table = q.find('cap_table', r'구분 전월이용금액 (30만원 이상 70만원 미만) '
        r'주유 서비스 월 이용금액 한도 차량 서비스 월 이용금액 한도 생활 서비스 월 이용금액 한도 '
        r'영화 서비스 월 이용금액 한도 (\d+만원) (\d+만원) (\d+만원) \d+회 '
        r'(70만원 (?:미만 )?이상) (\d+만원) (\d+만원) (\d+만원) \d+회')
    family = q.find('family', r'월 이용금액 및 제공횟수 한도는 할인대상 가맹점의 이용금액 및 횟수 기준이며, 본인\+가족카드 통합 적용됩니다\.')
    shared = q.find('shared', r'생활 서비스 월 이용금액 한도[^※]{0,80}?편의점\+커피\+택시 통합[^※]{0,80}?거래 순서에 따라 할인 제공됩니다\.')
    cinema = q.find('cinema', r'(CGV|롯데시네마) 일반관 5천원 현장 할인')
    main_q = Quotes(source_text)
    main_cinema = main_q.find('cinema', r'(CGV|롯데시네마) 일반관 5천원 현장 할인')
    checked, conflicts = [], []
    def check(field, primary, secondary, scope='living_rules'):
        checked.append(field)
        if primary != secondary:
            conflicts.append({'field':field, 'detail_value':primary, 'leaflet_value':secondary, 'scope':scope})
    for rule in payload['rules']:
        other = rules[rule['rule_key']]
        check(rule['rule_key']+'.brands',rule['scope']['brands'],other['brands'])
        check(rule['rule_key']+'.rate_bps',rule['rate_bps'],other['rate_bps'])
    group = payload['limit_groups'][0]
    check('living.caps', [group['caps_by_spend_tier_krw'][t] for t in ('standard','higher')],
          [money(table[4]),money(table[8])])
    check('living.cap_unit',group['cap_unit'],'eligible_payment_krw' if '이용금액' in family[0] else None)
    check('living.family_shared',group['shared_with_family_cards'],'본인+가족카드' in family[0])
    check('living.taxi_shared',group['includes_unextracted_services'],['taxi'] if '택시 통합' in shared[0] else [])
    check('spend.higher_label','70만원 이상',table[5])
    check('outside_scope.cinema.brand',main_cinema[1],cinema[1],'outside_extracted_rules')
    return {'status':'conflict' if conflicts else 'partial_match','checked_fields':checked,'conflicts':conflicts,
            'primary_evidence': list(main_q.evidence.values()), 'leaflet_evidence':list(q.evidence.values()),
            'unresolved':['merchant_channel_policy','rounding_policy','cap_boundary_policy',
                          'all_policy_clauses','document_applicability'],
            'note':'공시 게시일과 심의일은 혜택 적용 시작일 또는 종료일로 사용하지 않음'}


def document_checks(db, version, *, now=None):
    """Read-only: current observations and decoder version bind every comparison."""
    configs = {'hana_day_offline': (DAY_LEAFLET_URL,HANA_DAY_URL,'hana_day_crosscheck_v1',compare_day),
        'shinhan_deep_oil_living': (DEEP_OIL_DISCLOSURE_URL,DEEP_OIL_URL,'shinhan_deep_oil_crosscheck_v1',compare_deep_oil)}
    if version.program_key not in configs:
        return {'status': 'not_configured', 'required': False}
    document_url, primary_url, validator, compare = configs[version.program_key]
    now = aware(now or datetime.now(timezone.utc))
    result = {'required': True, 'source_url': document_url, 'validator_version': validator,
        'primary_snapshot_id': version.snapshot_id, 'primary_payload_hash': version.payload_hash}
    source = db.scalar(select(CardCollectionSource).where(CardCollectionSource.canonical_url == document_url))
    if not source or not source.enabled or source.last_error:
        return {**result, 'status': 'source_unavailable'}
    primary_source = db.get(CardCollectionSource, version.source_id)
    if not primary_source or primary_source.canonical_url != primary_url or primary_source.issuer_id != source.issuer_id or not source.issuer_id:
        return {**result, 'status': 'identity_unresolved'}
    observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
        CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
    snapshot = db.get(CardSourceSnapshot, observed.snapshot_id) if observed else None
    if not snapshot:
        return {**result, 'status': 'snapshot_missing'}
    result.update(snapshot_id=snapshot.id, raw_sha256=snapshot.raw_sha256)
    full = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
        CardCollectionAttempt.snapshot_id == snapshot.id, CardCollectionAttempt.status == 'captured',
        CardCollectionAttempt.http_status == 200).order_by(CardCollectionAttempt.id.desc()).limit(1))
    if not full or not now - timedelta(days=7) <= aware(full.attempted_at) <= now:
        return {**result, 'status': 'source_stale'}
    text = db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id == snapshot.id,
        CardDocumentText.parser_version == TEXT_PARSER))
    if not text:
        return {**result, 'status': 'reparse_required'}
    if text.raw_sha256 != snapshot.raw_sha256 or hashlib.sha256(text.text.encode()).hexdigest() != text.text_sha256:
        return {**result, 'status': 'text_changed'}
    primary = db.get(CardSourceSnapshot, version.snapshot_id)
    result.update(text_id=text.id, text_parser=text.parser_version, text_sha256=text.text_sha256,
        full_body_checked_at=aware(full.attempted_at).isoformat())
    try:
        return {**result, **compare(version.payload, primary.extracted_text, text.text)}
    except (ValueError, KeyError, StopIteration) as exc:
        return {**result, 'status': 'unresolved', 'reason': type(exc).__name__}
