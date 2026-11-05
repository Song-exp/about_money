"""Deterministic issuer adapters: reject any unreviewed policy/structure change."""
import hashlib
import re
from decimal import Decimal
from bs4 import BeautifulSoup

KB_URL = 'https://card.kbcard.com/CRD/DVIEW/HCAMCXPRICAC0076?cooperationcode=01900&mainCC=a'
KB_CARD = 'kb_pengsoo_nori_check'
BRANDS = {'아웃백': '아웃백,VIPS', 'VIPS': '아웃백,VIPS', '스타벅스': '스타벅스', 'GS25': 'GS25'}


def extract_pengsoo(content):
    soup = BeautifulSoup(content, 'html.parser')
    headings = soup.select('#main_contents h1.tit')
    blocks = soup.select('#main_contents .contentDetail')
    if len(headings) != 1 or '펭수노리체크카드' not in re.sub(r'\s+', '', headings[0].get_text()):
        raise ValueError('product_identity_mismatch')
    if len(blocks) != 1:
        raise ValueError('benefit_document_missing_or_ambiguous')
    main = soup.select_one('#main_contents')
    for tag in main(['script', 'style']):
        tag.decompose()
    # Include product notices outside the detail tabs in the policy fingerprint.
    text = re.sub(r'\s+', '', main.get_text())
    tier_tables = re.findall(r'전월이용실적((?:\d+만원이상){4})월간통합할인한도((?:\d+만원){4})', text)
    if not tier_tables or len(set(tier_tables)) != 1:
        raise ValueError('missing_or_conflicting_monthly_limits')
    thresholds, limits = tier_tables[0]
    tiers = [{'min_spend': int(a)*10000, 'cap': int(b)*10000} for a, b in zip(
        re.findall(r'(\d+)만원', thresholds), re.findall(r'(\d+)만원', limits))]
    grace = re.findall(r'최초발급후사용등록일로부터(\d+)일까지는월간통합할인한도(\d+)만원이내', text)
    if not grace or len(set(grace)) != 1:
        raise ValueError('missing_or_conflicting_grace_limits')
    skeleton = text
    extracted = {}
    for label in dict.fromkeys(BRANDS.values()):
        # Summary and detail must agree; cap must equal the stated eligible base * rate.
        escaped = re.escape(label)
        detail = escaped + r'(\d+(?:\.\d+)?)%환급할인이용금액건당(\d+)만원~(\d+)만원까지할인적용\(1회최대할인금액([\d,]+)원\)'
        found = list(re.finditer(detail, text))
        summary = list(re.finditer(escaped + r'(\d+(?:\.\d+)?)%이용금액건당(\d+)만원~(\d+)만원까지할인적용', text))
        if len(found) != 1 or len(summary) != 1:
            raise ValueError('missing_or_duplicate_benefit_fields')
        rate, minimum, maximum, cap = found[0].groups()
        if summary[0].groups() != (rate, minimum, maximum):
            raise ValueError('summary_detail_conflict')
        rate, minimum, maximum, cap = Decimal(rate), int(minimum)*10000, int(maximum)*10000, int(cap.replace(',', ''))
        if not 0 < rate <= 100 or minimum > maximum or cap != Decimal(maximum)*rate/100:
            raise ValueError('invalid_rate_minimum_or_cap')
        # Overview may use the middle-dot variant; every occurrence must agree.
        headline = re.escape(label).replace(',', '[,·]') + r'(\d+(?:\.\d+)?)%환급할인'
        if any(Decimal(m[1]) != rate for m in re.finditer(headline, text)):
            raise ValueError('headline_detail_conflict')
        skeleton = re.sub(detail, label+'<rate>%환급할인이용금액건당<min>만원~<max>만원까지할인적용(1회최대할인금액<cap>원)', skeleton)
        skeleton = re.sub(escaped + r'\d+(?:\.\d+)?%이용금액건당\d+만원~\d+만원까지할인적용', label+'<rate>%이용금액건당<min>만원~<max>만원까지할인적용', skeleton)
        skeleton = re.sub(headline, lambda m: m[0].replace(m[1]+'%', '<rate>%'), skeleton)
        extracted[label] = {'value': str(rate), 'minimum': minimum, 'cap': cap,
                            'value_quote': f'{label} {rate}% 환급할인', 'minimum_quote': f'{minimum//10000}만원~{maximum//10000}만원',
                            'cap_quote': f'최대할인금액{cap:,}원'}
    return {'policy_hash': hashlib.sha256(skeleton.encode()).hexdigest(),
            'monthly_cap_tiers': tiers, 'grace_days': int(grace[0][0]), 'grace_monthly_cap': int(grace[0][1])*10000,
            'benefits': {brand: extracted[label] for brand, label in BRANDS.items()}}
