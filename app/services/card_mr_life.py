"""Mr.Life TIME services only; issuer categories cannot imply brand eligibility."""

MR_LIFE_URL = 'https://www.shinhancard.com/pconts/html/card/apply/credit/1187937_2207.html'


def parse_mr_life(text):
    from app.services.card_benefit_parsers import Quotes, between, money
    q = Quotes(text)
    q.find('identity', r'상품명:\s*신한카드 Mr\.Life')
    detail = text.split('혜택 안내 ', 1)[-1]
    block = between(detail, 'TIME 할인 할인한도 ', '주말 할인 할인마트 ')
    table = q.find('time.spend_cap_table', r'전월 이용금액 할인한도 '
        r'(\d+만원) 이상 (\d+만원) 미만 (\d+만원) '
        r'(\d+만원) 이상 (\d+만원) 미만 (\d+만원) (\d+만원) 이상 (\d+만원)', block)
    low, mid, cap_low, mid2, high, cap_mid, high2, cap_high = [money(table[i]) for i in range(1, 9)]
    if mid != mid2 or high != high2 or not 0 < low < mid < high or not 0 < cap_low <= cap_mid <= cap_high:
        raise ValueError('mr_life_time_cap_table_conflict')
    rate = q.find('time.rate', r'TIME 할인서비스 (\d+)% 할인 All Day: 편의점, 병원/약국, 세탁비 (\d+)% 할인', block)
    night = q.find('time.night_rate', r'Night: 오후 (\d+)시~오전 (\d+)시까지 온라인쇼핑, 택시, 식음료 (\d+)% 할인', block)
    if rate[1] != rate[2] or rate[1] != night[3] or not 0 < int(rate[1]) <= 100:
        raise ValueError('mr_life_time_rate_conflict')
    if (int(night[1]), int(night[2])) != (9, 9):
        raise ValueError('mr_life_time_window_changed')
    day = q.find('time.day_limits', r'편의점 편의점 업종 구분 영역별 각각 일 (\d+)회/월 (\d+)회 할인 적용 '
        r'1회 승인금액 (\d+만원)까지 할인 적용\(1회 최대 (\d+천원) 할인\)', block)
    night_limits = q.find('time.night_limits', r'구분영역별 각각 일 (\d+)회/월 (\d+)회 할인 적용 '
        r'1회 승인금액 (\d+만원)까지 할인 적용\(1회 최대 (\d+천원) 할인\)', block)
    for match in (day, night_limits):
        if (not 0 < int(match[1]) <= int(match[2])
                or money(match[3])*int(rate[1]) != money(match[4])*100):
            raise ValueError('mr_life_time_transaction_cap_conflict')
    categories = q.find('time.food_categories', r'식음료 (한식, 양식, 일식, 중식, 뷔페, 일반대중음식, 패스트푸드, 커피전문점) 업종', block)
    q.find('time.issuer_category', r'편의점, 병원/약국, 세탁소, 식음료는 신한카드 가맹점 업종 기준으로 할인이 제공됩니다\.', block)
    q.find('time.approval_clock', r'Time할인서비스는 주중/주말 상관없이 제공되는 서비스이며, Night Time 할인서비스는 승인시간 기준으로 오후 9시부터 오전 9시까지 제공됩니다\.', block)
    q.find('time.shared_cap', r'할인서비스는 전월 이용금액 구간에 따라 제공된 서비스별 월 할인 한도 내에서 제공됩니다\.', block)
    q.find('time.allocation', r'서비스 대상 거래건 중 신한카드 전표 매입 순서대로 결제일 할인이 적용됩니다\.', block)
    grace = q.find('time.grace', r'신규 발급 회원에 대해서는 카드사용 등록월의 익월말\(등록월\+1개월\)까지 전월실적 (\d+)~(\d+)만원 구간의 할인한도가 제공됩니다\.', block)
    if [int(grace[1])*10000, int(grace[2])*10000] != [low, mid]:
        raise ValueError('mr_life_time_grace_conflict')
    q.find('time.spend_basis', r'교통이용금액은 전전월 이용금액 기준이며, 해외이용금액은 매입일자를 기준으로 적용됩니다\.', block)
    exclusions = q.find('time.spend_exclusions', r'장·단기 카드 대출/연회비/각종수수료/이자/기프트카드 구매금액/선불카드 충전금액/거래취소금액은 전월 이용금액에서 제외됩니다\.', block)
    q.add('time.full_policy', block)
    rules = []
    for key, match, scope in [('convenience', day, ['편의점']),
                              ('night_food', night_limits, categories[1].split(', '))]:
        rules.append({'rule_key': key, 'reward_method': 'statement_discount', 'calculation_type': 'percent',
            'rate_bps': int(rate[1])*100, 'scope': {'type': 'issuer_categories', 'categories': scope},
            'limit_group': 'time_shared', 'daily_count_cap': int(match[1]), 'monthly_count_cap': int(match[2]),
            'count_scope': 'service_category', 'max_eligible_payment_per_transaction_krw': money(match[3]),
            'max_reward_per_transaction_krw': money(match[4]),
            'time_window': None if key == 'convenience' else {'timezone': 'Asia/Seoul', 'basis': 'approval_time',
                'start_hour': 21, 'end_hour': 9, 'crosses_midnight': True, 'boundary_policy': 'unresolved'},
            'channel': 'issuer_category_confirmation_required', 'rounding_rule': 'unresolved'})
    return {'program_key': 'shinhan_mr_life_time', 'benefit_kind': 'base', 'season_id': None,
        'period_status': 'end_not_stated', 'valid_from': None, 'valid_until_exclusive': None,
        'product_refs': [{'namespace': 'shinhan', 'code': '1187937_2207'}], 'selection': None,
        'rules': rules, 'limit_groups': [{'group_key': 'time_shared', 'scope': 'all_time_services',
            'period': 'calendar_month_kst', 'cap_unit': 'reward_krw',
            'caps_by_spend_tier_krw': {'standard': cap_low, 'middle': cap_mid, 'higher': cap_high},
            'shared_across_brands': True, 'includes_unextracted_services': ['hospital_pharmacy', 'laundry', 'online_shopping', 'taxi'],
            'allocation': 'issuer_acquisition_order'}],
        'spend_tiers': [{'tier_key': 'standard', 'min_prior_spend_krw': low, 'max_prior_spend_exclusive_krw': mid},
            {'tier_key': 'middle', 'min_prior_spend_krw': mid, 'max_prior_spend_exclusive_krw': high},
            {'tier_key': 'higher', 'min_prior_spend_krw': high, 'max_prior_spend_exclusive_krw': None}],
        'prior_spend_basis': {'transport': 'two_months_before', 'overseas': 'acquisition_date', 'default': 'unresolved'},
        'new_card_grace': {'basis': 'first_card_use_registration', 'until': 'end_of_following_month_kst',
            'applied_tier': 'standard', 'requires_user_confirmation': True},
        'spend_exclusions_raw': exclusions.group(), 'policy_raw': block, 'exclusions_raw': '',
        'outside_scope': ['utilities', 'weekend_mart', 'weekend_fuel', 'intake', 'hospital_pharmacy', 'laundry', 'online_shopping', 'taxi'],
        'unknown_user_conditions': ['prior_spend_after_exclusions', 'time_shared_remaining_cap',
            'service_category_remaining_counts', 'issuer_merchant_classification', 'approval_time', 'grace_eligibility'],
        'issues': ['manual_policy_review_required', 'period_policy_not_supported', 'issuer_category_mapping_not_supported',
            'time_window_engine_not_supported', 'time_boundary_policy_unresolved', 'rounding_rule_unresolved',
            'transaction_exclusion_policy_unresolved', 'other_services_not_extracted'], 'evidence': list(q.evidence.values())}
