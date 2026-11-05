"""Source-specific benefit extraction with exact quotes and cross-checks.

Only the supported portions are extracted. No output is an approval or a promise
that user-specific eligibility or every policy exclusion has been resolved.
"""
import re
from datetime import datetime, timedelta, timezone

TOSS_URL = 'https://www.tossbank.com/card/benefits'
JOOPJOOP_URL = 'https://www.shinhancard.com/pconts/html/card/apply/credit/1234363_2207.html'
HANA_DAY_URL = 'https://www.hanacard.co.kr/OPI41000000D.web?CD_PD_SEQ=18669'
DEEP_OIL_URL = 'https://www.shinhancard.com/pconts/html/card/apply/credit/1188274_2207.html'
KST = timezone(timedelta(hours=9))


class Quotes:
    def __init__(self, text):
        self.text, self.evidence = text, {}

    def find(self, path, pattern, block=None):
        match = re.search(pattern, self.text if block is None else block)
        if not match:
            raise ValueError('benefit_required_evidence_missing:' + path)
        self.add(path, match.group())
        return match

    def add(self, path, quote):
        start = self.text.find(quote)
        if not quote or start < 0:
            raise ValueError('benefit_evidence_not_in_snapshot')
        self.evidence[path] = {'field_path': path, 'quote': quote,
                               'start_offset': start, 'end_offset': start + len(quote)}


def money(value):
    match = re.fullmatch(r'([\d,]+)(만|천)?원', value)
    if not match:
        raise ValueError('unsupported_benefit_money')
    return int(match[1].replace(',', '')) * {None: 1, '만': 10000, '천': 1000}[match[2]]


def between(text, start, end):
    begin = text.find(start)
    finish = text.find(end, begin + len(start)) if begin >= 0 else -1
    if begin < 0 or finish < 0:
        raise ValueError('benefit_section_missing')
    return text[begin + len(start):finish].strip()


def parse_toss(text):
    q = Quotes(text)
    period = q.find('period', r'토스뱅크 체크카드 스위치 캐시백 시즌\s*(\d+)\s*:\s*오프라인 캐시백 기간\s*\|\s*(\d{4}\.\d{2}\.\d{2})\.\s*~\s*(\d{4}\.\d{2}\.\d{2})\.')
    start = datetime.strptime(period[2], '%Y.%m.%d').replace(tzinfo=KST)
    end = datetime.strptime(period[3], '%Y.%m.%d').replace(tzinfo=KST) + timedelta(days=1)
    if end <= start:
        raise ValueError('benefit_period_invalid')
    banner = q.find('period_crosscheck', r'(\d{2})년 (\d{1,2})월 (\d{1,2})일까지')
    if (2000 + int(banner[1]), int(banner[2]), int(banner[3])) != (end - timedelta(days=1)).timetuple()[:3]:
        raise ValueError('benefit_period_conflict')
    tiers = q.find('tiers', r'([\d,]+(?:만|천)?원)\s*~\s*([\d,]+(?:만|천)?원) 미만 ([\d,]+원) 캐시백, ([\d,]+(?:만|천)?원) 이상 결제 시 ([\d,]+원) 캐시백')
    minimum, threshold, low, threshold2, high = [money(tiers[i]) for i in range(1, 6)]
    detail = q.find('tiers_crosscheck', r'7개 영역별 ([\d,]+(?:만|천)?원) 이상 결제 시 ([\d,]+원), ([\d,]+(?:만|천)?원) 미만 결제 시 ([\d,]+원) 캐시백')
    if (threshold != threshold2 or [money(detail[i]) for i in range(1, 5)] != [threshold, high, threshold, low]
            or not 0 < minimum < threshold or not 0 < low <= high):
        raise ValueError('benefit_tiers_conflict')
    limits = q.find('limits', r'각 캐시백 영역별 당일 첫 결제건에 대해서만 하루 (\d+)회, 월 (\d+)회까지 캐시백이 제공돼요\.')
    cap = q.find('monthly_cap', r'영역별 최대 월 ([\d,]+원) 한도로 캐시백을 받을 수 있어요\.')
    day_count, month_count, amount_cap = int(limits[1]), int(limits[2]), money(cap[1])
    if day_count != 1 or month_count <= 0 or not 0 < amount_cap <= high * month_count:
        raise ValueError('benefit_limit_conflict')
    minimum_check = q.find('minimum_crosscheck', r'최소 결제 금액\(([\d,]+원)\) 이상 결제 건에 대해서만 캐시백을 받을 수 있어요\.')
    if money(minimum_check[1]) != minimum:
        raise ValueError('benefit_minimum_conflict')
    q.find('selection', r'오프라인 캐시백으로 혜택 변경 시 신청 익일 00시부터 변경된 혜택이 적용돼요\.')
    q.find('stacking', r'토스뱅크 체크카드의 다른 스위치 캐시백 프로모션과 중복 적용되지 않아요\.')
    q.find('early_end', r'이 프로모션은 [^.]*?(?:변경 또는 중단|조기 종료)[^.]*\.')
    q.find('channel', r'간편결제\(네이버페이, 카카오페이 등\)를 통해 결제하면 캐시백이 제공되지 않을 수 있어요\.')
    exclusions = between(text, '캐시백 제외대상 및 유의사항 ', '기타 안내 ')
    q.add('exclusions', exclusions)
    q.find('merchant_classification', r'캐시백은 하나카드에 등록된 가맹점 및 업종코드를 기준으로만 받을 수 있어요\.')
    q.find('cinema_channel', r'영화관 캐시백 : 영화관 공식 홈페이지 및 앱 또는 현장 결제 시 제공되며, 상품권 구매 및 매점 이용 건은 제외돼요\.')
    definitions = [
        ('convenience', '편의점 전국 모든 ', '대중교통 후불'),
        ('food_drink', '음식 & 음료 ', '마트 & 백화점 '),
        ('shopping', '마트 & 백화점 ', '서점 '),
        ('books', '서점 ', '즉석사진 '),
        ('photos', '즉석사진 ', '영화관 '),
        ('cinema', '영화관 ', '꼭 확인해주세요'),
    ]
    rules, groups = [], []
    for key, begin, finish in definitions:
        brands = between(text, begin, finish)
        if len(brands) > 500:
            raise ValueError('benefit_brand_section_invalid')
        if not all(name.strip() for name in brands.split(',')):
            raise ValueError('benefit_brand_section_invalid')
        q.add('rules.' + key + '.brands', begin + brands)
        rules.append({'rule_key': key, 'reward_method': 'cashback', 'calculation_type': 'amount_tiers',
            'scope': {'type': 'brands', 'brands': [name.strip() for name in brands.split(',')]},
            'tiers': [{'min_payment_krw': minimum, 'max_payment_exclusive_krw': threshold, 'amount_krw': low},
                      {'min_payment_krw': threshold, 'max_payment_exclusive_krw': None, 'amount_krw': high}],
            'limit_group': key, 'channel': 'offline_or_official_cinema_online' if key == 'cinema' else 'offline',
            'simple_pay': 'may_be_excluded'})
        groups.append({'group_key': key, 'scope': 'program_area', 'daily_count_cap': day_count,
            'monthly_count_cap': month_count, 'monthly_amount_cap_krw': amount_cap,
            'first_transaction_only': True, 'shared_across_brands': True})
    return {'program_key': 'toss_switch_offline', 'benefit_kind': 'seasonal', 'season_id': period[1],
        'period_status': 'explicit', 'valid_from': start.astimezone(timezone.utc).isoformat(),
        'valid_until_exclusive': end.astimezone(timezone.utc).isoformat(),
        'product_refs': [{'namespace': 'tossbank', 'code': 'check-card'}],
        'selection': {'mode': 'offline', 'effective': 'next_day_00_kst'}, 'rules': rules, 'limit_groups': groups,
        'early_end_possible': True, 'stacking': {'other_switch_modes': False, 'transport_kpass_outside_scope': True},
        'exclusions_raw': exclusions, 'outside_scope': ['transport', 'overseas', 'online_mode', 'anywhere_mode', 'donation_mode'],
        'unknown_user_conditions': ['selected_mode_effective_at', 'remaining_limits', 'issuer_merchant_classification', 'excluded_branch'],
        'issues': ['manual_policy_review_required', 'other_modes_not_extracted'],
        'evidence': list(q.evidence.values())}


def parse_joopjoop(text):
    q = Quotes(text)
    q.find('identity', r'상품명:\s*카카오뱅크 줍줍 신한카드')
    detail = text.split('혜택 안내 ', 1)[-1]
    boundary = re.search(r'카드값 바로내면 \d+% 캐시백 서비스', detail)
    if not boundary:
        raise ValueError('benefit_section_missing')
    blocks = [('base', detail[:boundary.start()]), ('early_payment', detail[boundary.start():])]
    rules, groups = [], []
    for key, block in blocks:
        pattern = r'국내/외 이용금액 (\d+)% 캐시백' if key == 'base' else r'즉시결제하면 (\d+)% 추가 캐시백'
        rate = q.find('rules.' + key + '.rate', pattern, block)
        cap = q.find('rules.' + key + '.cap', r'지난달 이용금액에 관계 없이,? 월\((승인일자|즉시결제일자) 기준\) 최대 (\d+만원)까지 제공됩니다\.', block)
        expected_basis = '승인일자' if key == 'base' else '즉시결제일자'
        if cap[1] != expected_basis or not 0 < int(rate[1]) <= 100 or money(cap[2]) <= 0:
            raise ValueError('benefit_rate_or_basis_invalid')
        heading = '카드 쓰면' if key == 'base' else '카드값 바로내면'
        table = q.find('rules.' + key + '.cap_crosscheck', heading + r' (\d+)% 캐시백 서비스 \(' + expected_basis + r' 기준\) (\d+만원)')
        if table[1] != rate[1] or money(table[2]) != money(cap[2]):
            raise ValueError('benefit_cap_conflict')
        if key == 'early_payment':
            exclusions = between(block, '서비스 제외 대상은 아래와 같습니다. ', '카카오뱅크 줍줍 신한카드 유의사항')
        else:
            exclusions = block.split('서비스 제외 대상은 아래와 같습니다. ', 1)[-1].strip()
        if '거래 취소금액' not in exclusions:
            raise ValueError('benefit_exclusions_missing')
        q.add('rules.' + key + '.exclusions', exclusions)
        rules.append({'rule_key': key, 'reward_method': 'cashback', 'calculation_type': 'percent',
            'rate_bps': int(rate[1]) * 100, 'scope': {'type': 'issuer_merchants'},
            'limit_group': key, 'prior_spend_required_krw': 0, 'exclusions_raw': exclusions})
        groups.append({'group_key': key, 'scope': 'program_service', 'period': 'calendar_month_kst',
            'monthly_amount_cap_krw': money(cap[2]), 'aggregation_basis': 'approval_date' if key == 'base' else 'early_payment_date',
            'shared_across_brands': True})
    days = q.find('early_payment.days', r'매입일 \+ (\d+)일\(휴일 포함\) 이내 즉시 결제한 일시불 건에 한하여 캐시백이 제공됩니다\.')
    minimum = q.find('early_payment.minimum', r'즉시결제 1회당 (\d+만원) 이상인 경우에만 제공됩니다\.')
    if not 1 <= int(days[1]) <= 365 or money(minimum[1]) <= 0:
        raise ValueError('benefit_early_payment_condition_invalid')
    q.find('account', r'카카오뱅크 줍줍 신한카드 결제계좌는 카카오뱅크 계좌만 가능합니다\.')
    q.find('early_payment.full_payment', r'‘이용 건별 결제’를 통해 완납한 거래 건에 한하여 제공됩니다\.')
    q.find('early_payment.direct_debit', r'‘카드값 바로내면 1% 캐시백’ 서비스는 카카오뱅크 계좌에서 즉시 출금하여 결제한 경우에만 제공됩니다\. \(가상계좌 등으로 결제한 경우 서비스 제공 불가\)')
    q.find('stacking', r'‘카드 쓰면 1% 캐시백’ 서비스와 ‘카드값 바로내면 1% 캐시백’ 서비스는 중복으로 제공이 가능합니다\.')
    rules[1]['additional_conditions'] = {'within_days_from_acquisition_inclusive': int(days[1]),
        'installment': False, 'full_payment_per_transaction': True,
        'min_early_payment_batch_krw': money(minimum[1]), 'payment_account_bank': 'kakaobank',
        'direct_debit_required': True, 'virtual_account_allowed': False}
    return {'program_key': 'shinhan_joopjoop_cashback', 'benefit_kind': 'base', 'season_id': None,
        'period_status': 'end_not_stated', 'valid_from': None, 'valid_until_exclusive': None,
        'product_refs': [{'namespace': 'kakaobank', 'code': 'plcc'}], 'rules': rules, 'limit_groups': groups,
        'selection': None, 'policy_raw': detail,
        'stacking': {'within_program_only': ['base', 'early_payment'], 'other_benefits': 'unresolved'},
        'unknown_user_conditions': ['remaining_limits', 'account_bank', 'early_payment_completion', 'transaction_exclusions'],
        'outside_scope': ['issuance_campaigns', 'emoji_promotions'],
        'issues': ['manual_policy_review_required', 'rounding_rule_unresolved'], 'evidence': list(q.evidence.values())}


def parse_hana_day(text):
    """Offline brand rules, retaining all eight areas' shared-cap context."""
    q = Quotes(text)
    q.find('identity', r'토스뱅크 하나카드 Day 청구할인 서비스를 나타내는 표 입니다\.')
    summary = q.find('summary', r'청구할인 서비스 8대 생활영역 (\d+)% 할인')
    header = q.find('spend_tiers', r'지난달 실적에 따른 월 할인 한도 (\d+)만 이상 (\d+)만 이상')
    thresholds = [int(header[i]) * 10000 for i in (1, 2)]
    if not 0 < thresholds[0] < thresholds[1]:
        raise ValueError('benefit_spend_tiers_invalid')
    table = between(text, header.group() + ' ', ' 커피 :')
    definitions = [('coffee', '커피', '쇼핑'), ('shopping', '쇼핑', '보험'),
        ('insurance', '보험', '통신'), ('telecom', '통신', '아파트'),
        ('apartment', '아파트', '학원'), ('academy', '학원', '병원'),
        ('hospital', '병원', '골프'), ('golf', '골프', '통합 월 할인 한도')]
    area_caps, scopes, rates = {}, {}, {}
    for key, label, next_label in definitions:
        row = q.find('areas.' + key, re.escape(label) + r' (.+?) (\d+)% ([\d,]+(?:만|천)?원) ([\d,]+(?:만|천)?원)(?= ' + re.escape(next_label) + r')', table)
        rates[key] = int(row[2]) * 100
        scopes[key] = row[1]
        area_caps[key] = [money(row[3]), money(row[4])]
        if rates[key] != int(summary[1]) * 100 or not 0 < rates[key] <= 10000:
            raise ValueError('benefit_rate_conflict')
    integrated = q.find('integrated_cap', r'통합 월 할인 한도 ([\d,]+(?:만|천)?원) ([\d,]+(?:만|천)?원)', table)
    integrated_caps = [money(integrated[1]), money(integrated[2])]
    if not 0 < integrated_caps[0] <= integrated_caps[1]:
        raise ValueError('benefit_integrated_cap_invalid')
    for caps in area_caps.values():
        if not 0 < caps[0] <= caps[1] or any(caps[i] > integrated_caps[i] for i in (0, 1)):
            raise ValueError('benefit_area_cap_invalid')
    crosscheck = q.find('spend_tiers_crosscheck', r'지난달 1일부터 말일\(승인 시점 기준\)까지 국내외 일시불/할부 이용금액 (\d+만원)/(\d+만원) 이상 시 서비스 차등 제공')
    if [money(crosscheck[1]), money(crosscheck[2])] != thresholds:
        raise ValueError('benefit_spend_tiers_conflict')
    grace = q.find('new_card_grace', r'최초 카드 사용등록일로부터 다음달 말일까지는 지난달 카드 실적 (\d+만원) 미만이어도 (\d+만원) 이상 (\d+만원) 미만 기준으로 서비스 제공')
    if [money(grace[i]) for i in (1, 2, 3)] != [thresholds[0], *thresholds]:
        raise ValueError('benefit_grace_tier_conflict')
    coffee_policy = between(text, '커피 : ', ' 쇼핑(이마트, 롯데마트, 홈플러스) :')
    shopping_policy = between(text, '쇼핑(이마트, 롯데마트, 홈플러스) : ', ' 쇼핑(토스쇼핑) :')
    q.add('coffee_exclusions', coffee_policy)
    q.add('shopping_exclusions', shopping_policy)
    if not all(word in coffee_policy for word in ('국내 가맹점', '입점 점포', '임대매장', '배달앱', '예약앱', '제외')):
        raise ValueError('benefit_coffee_scope_unresolved')
    if not all(word in shopping_policy for word in ('오프라인 결제건에 한하여', '상설·임대매장', '창고형 할인매장', '제외')):
        raise ValueError('benefit_shopping_scope_unresolved')
    q.find('online_shopping_scope', r'쇼핑\(토스쇼핑\) : 공식 앱을 통한 결제 건에 한하여 서비스 제공')
    policy = between(text, '청구할인 서비스 제공 조건 ', ' 연회비 청구 및 반환기준')
    q.add('policy', policy)
    spend_exclusions = between(policy, '[실적 제외 대상] ', ' [서비스 제공 방법]')
    discount_exclusions = policy.split('[할인 제외 대상] ', 1)
    if len(discount_exclusions) != 2 or '할인받은 매출 전체 제외' not in spend_exclusions:
        raise ValueError('benefit_exclusions_missing')
    q.add('spend_exclusions', spend_exclusions)
    q.add('discount_exclusions', discount_exclusions[1])
    q.find('merchant_classification', r'모든 할인 서비스는 하나카드에 등록된 가맹점 및 업종 기준에 따라 제공')
    q.find('interest_free_exclusion', r'무이자 할부\(부분 무이자 할부 포함\) 이용 건은 할인 혜택 제공하지 않음')
    rules, groups = [], []
    for key, raw, policy_text in [('coffee', scopes['coffee'], coffee_policy), ('shopping', scopes['shopping'], shopping_policy)]:
        brands = [value.strip() for value in raw.split(',')]
        if any(not name or len(name) > 50 for name in brands):
            raise ValueError('benefit_brand_section_invalid')
        if key == 'shopping':
            if brands != ['이마트', '롯데마트', '홈플러스', '토스쇼핑']:
                raise ValueError('benefit_shopping_brand_scope_changed')
            brands.remove('토스쇼핑')
        rules.append({'rule_key': key, 'reward_method': 'statement_discount', 'calculation_type': 'percent',
            'rate_bps': rates[key], 'scope': {'type': 'brands', 'brands': brands}, 'channel': 'offline',
            'limit_groups': [key, 'program_total'], 'exclusions_raw': policy_text,
            'rounding_rule': 'unresolved'})
        groups.append({'group_key': key, 'scope': 'program_area', 'period': 'calendar_month_kst',
            'shared_across_brands': True, 'caps_by_spend_tier_krw': dict(zip(('standard', 'higher'), area_caps[key]))})
    groups.append({'group_key': 'program_total', 'scope': 'program_all_eight_areas',
        'period': 'calendar_month_kst', 'shared_across_brands': True,
        'caps_by_spend_tier_krw': dict(zip(('standard', 'higher'), integrated_caps)),
        'includes_unextracted_areas': True})
    return {'program_key': 'hana_day_offline', 'benefit_kind': 'base', 'season_id': None,
        'period_status': 'end_not_stated', 'valid_from': None, 'valid_until_exclusive': None,
        'product_refs': [{'namespace': 'hanacard', 'code': '18669'}], 'selection': None,
        'rules': rules, 'limit_groups': groups,
        'spend_tiers': [{'tier_key': 'standard', 'min_prior_spend_krw': thresholds[0], 'max_prior_spend_exclusive_krw': thresholds[1]},
            {'tier_key': 'higher', 'min_prior_spend_krw': thresholds[1], 'max_prior_spend_exclusive_krw': None}],
        'prior_spend_basis': 'previous_calendar_month_approval_kst', 'spend_exclusions_raw': spend_exclusions,
        'new_card_grace': {'basis': 'first_card_use_registration', 'until': 'end_of_following_month_kst',
            'applied_tier': 'standard', 'requires_user_confirmation': True},
        'all_area_caps_by_spend_tier_krw': area_caps, 'policy_raw': policy,
        'exclusions_raw': coffee_policy + ' ' + shopping_policy + ' ' + policy,
        'outside_scope': ['insurance', 'telecom', 'apartment', 'academy', 'hospital', 'golf', 'toss_shopping_online'],
        'unknown_user_conditions': ['prior_spend_after_exclusions', 'grace_eligibility', 'all_area_remaining_caps',
            'issuer_merchant_classification', 'excluded_branch', 'payment_path'],
        'issues': ['manual_policy_review_required', 'rounding_rule_unresolved', 'other_areas_not_extracted', 'period_policy_not_supported'],
        'evidence': list(q.evidence.values())}


def parse_deep_oil(text):
    """Living services only; cap denominates eligible purchases, never rewards."""
    q = Quotes(text)
    q.find('identity', r'상품명:\s*신한카드 Deep Oil')
    living = between(text, '생활 할인 생활서비스 ', ' 영화 할인 영화서비스 ')
    summary = q.find('living_rate', r'생활서비스 (\d+)% 할인')
    rate = int(summary[1]) * 100
    if not 0 < rate <= 10000:
        raise ValueError('benefit_rate_invalid')
    rules = []
    for key, label in [('convenience', '편의점'), ('coffee', '커피')]:
        row = q.find('rules.' + key, label + r' (.{1,150}?) (\d+)% 결제일 할인', living)
        banner = q.find('rules.' + key + '.crosscheck', label + r'\(([^)]+)\) (\d+)% 결제일 할인')
        brands = [name.strip() for name in row[1].split(',')]
        if (int(row[2]) * 100 != rate or row[2] != banner[2]
                or brands != [name.strip() for name in banner[1].split(',')] or any(not name for name in brands)):
            raise ValueError('benefit_living_summary_conflict')
        rules.append({'rule_key': key, 'reward_method': 'statement_discount', 'calculation_type': 'percent',
            'rate_bps': rate, 'scope': {'type': 'brands', 'brands': brands},
            'limit_group': 'living_eligible_payment', 'channel': 'unresolved', 'rounding_rule': 'unresolved'})
    coffee_exclusions = q.find('coffee_exclusions', r'상품권 구입, 선불카드 충전은 할인 제외됩니다\.', living)
    rules[1]['additional_exclusions_raw'] = coffee_exclusions.group()
    taxi = q.find('unextracted_taxi', r'택시 (\d+)% 결제일 할인', living)
    if int(taxi[1]) * 100 != rate:
        raise ValueError('benefit_living_summary_conflict')
    minimum = q.find('minimum_spend', r'생활서비스는 신한카드 Deep Oil 전월 이용금액\(1일~말일, 일시불\+할부, 모든 주유소 주유 및 LPG 이용금액 제외\)이 (\d+만원) 이상인 경우 제공됩니다\.')
    q.find('living_shared_scope', r'생활서비스 월 이용금액 한도’ 내\(할인대상 편의점\+커피\+택시 통합\)에서 거래 순서에 따라 할인 제공됩니다\.')
    q.find('cap_units', r'주유서비스 월 이용금액 한도 차량서비스 월 이용금액 한도 생활서비스 월 이용금액 한도 영화서비스 월 제공횟수 한도')
    table = q.find('usage_cap_table', r'전월 이용 금액 (\d+만원) 이상 (\d+만원) 미만 (\d+만원) (\d+만원) (\d+만원) (\d+)회 (\d+만원) 이상 (\d+만원) (\d+만원) (\d+만원) (\d+)회')
    low, high = money(table[1]), money(table[2])
    low_cap, high_cap = money(table[5]), money(table[10])
    if (low != money(minimum[1]) or high != money(table[7]) or not 0 < low < high
            or not 0 < low_cap <= high_cap or not 0 < int(table[6]) <= int(table[11])):
        raise ValueError('benefit_usage_cap_table_conflict')
    q.find('family_sharing', r'월 이용금액 및 제공횟수 한도는 할인대상 가맹점의 이용금액 및 횟수 기준이며, 본인\+가족카드 통합 적용됩니다\.')
    q.find('living_aggregation', r'차량서비스, 생활서비스의 경우 각 영역별 이용금액은 통합하여 한도 내에서 제공됩니다\.')
    exclusions = between(text, '아래 거래는 할인 서비스 적용 대상에서 제외됩니다. ', ' 전월 이용금액 유의 사항')
    if not all(word in exclusions for word in ('무이자', '당사 할인', '포인트 사용 금액')):
        raise ValueError('benefit_exclusions_missing')
    q.add('discount_exclusions', exclusions)
    spend_policy = between(text, '전월 이용금액 유의 사항 ', ' 1 / 10')
    q.add('spend_policy', spend_policy)
    q.find('spend_basis', r'전월 이용금액은 신한카드 Deep Oil로 전월 1일~말일까지의 거래시점 이용금액\(일시불\+할부, 가족카드 포함\)으로 반영됩니다\.')
    q.find('transport_spend_basis', r'교통카드 이용금액은 전전월 이용금액이 반영됩니다\.')
    q.find('overseas_spend_basis', r'해외 이용금액은 매입일자 기준으로 반영됩니다\.')
    grace = q.find('new_card_grace', r'신규발급 회원의 경우 카드 사용 등록월의 익월말\(등록월\+1개월\)까지 ’(\d+만원) 이상~(\d+만원) 미만’ 구간의 이용금액 한도/제공횟수가 적용됩니다\.')
    upgrade = q.find('grace_upgrade', r'해당 기간 중 (\d+만원) 이상 이용 시엔 다음달에 해당 테이블에 상응하는 이용금액 한도/제공횟수가 적용됩니다\.')
    if [money(grace[1]), money(grace[2]), money(upgrade[1])] != [low, high, high]:
        raise ValueError('benefit_grace_tier_conflict')
    spend_exclusions = spend_policy.split('아래 거래는 전월 이용금액에 제외됩니다. ', 1)
    if len(spend_exclusions) != 2 or '모든 주유소 주유 및 LPG 이용금액' not in spend_exclusions[1]:
        raise ValueError('benefit_spend_exclusions_missing')
    q.add('spend_exclusions', spend_exclusions[1])
    return {'program_key': 'shinhan_deep_oil_living', 'benefit_kind': 'base', 'season_id': None,
        'period_status': 'end_not_stated', 'valid_from': None, 'valid_until_exclusive': None,
        'product_refs': [{'namespace': 'shinhan', 'code': '1188274_2207'}], 'selection': None,
        'rules': rules, 'limit_groups': [{'group_key': 'living_eligible_payment',
            'scope': 'living_services_cardholder_and_family', 'period': 'calendar_month_kst',
            'cap_unit': 'eligible_payment_krw', 'caps_by_spend_tier_krw': {'standard': low_cap, 'higher': high_cap},
            'shared_across_brands': True, 'shared_with_family_cards': True,
            'includes_unextracted_services': ['taxi'], 'allocation': 'transaction_order'}],
        'spend_tiers': [{'tier_key': 'standard', 'min_prior_spend_krw': low, 'max_prior_spend_exclusive_krw': high},
            {'tier_key': 'higher', 'min_prior_spend_krw': high, 'max_prior_spend_exclusive_krw': None}],
        'prior_spend_basis': {'default': 'previous_calendar_month_transaction_kst', 'family_cards_included': True,
            'transport': 'two_months_before', 'overseas': 'acquisition_date'},
        'new_card_grace': {'basis': 'first_card_use_registration', 'until': 'end_of_following_month_kst',
            'applied_tier': 'standard', 'higher_spend_threshold_krw': high,
            'higher_tier_effective': 'following_month', 'requires_user_confirmation': True},
        'exclusions_raw': exclusions, 'spend_exclusions_raw': spend_exclusions[1], 'policy_raw': spend_policy,
        'outside_scope': ['fuel', 'vehicle_maintenance', 'parking', 'taxi', 'cinema'],
        'unknown_user_conditions': ['family_shared_remaining_eligible_payment_cap', 'prior_spend_after_exclusions',
            'grace_eligibility', 'transaction_exclusions', 'merchant_channel'],
        'issues': ['manual_policy_review_required', 'rounding_rule_unresolved', 'period_policy_not_supported',
            'eligible_payment_cap_engine_not_supported', 'merchant_channel_policy_unresolved', 'other_services_not_extracted'],
        'evidence': list(q.evidence.values())}


PARSERS = {TOSS_URL: ('toss_offline_v3', parse_toss), JOOPJOOP_URL: ('shinhan_joopjoop_v2', parse_joopjoop),
           HANA_DAY_URL: ('hana_day_offline_v1', parse_hana_day), DEEP_OIL_URL: ('shinhan_deep_oil_living_v1', parse_deep_oil)}

from app.services.card_mr_life import MR_LIFE_URL, parse_mr_life
PARSERS[MR_LIFE_URL] = ('shinhan_mr_life_time_v1', parse_mr_life)
