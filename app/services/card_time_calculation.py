"""Read-only TIME diagnostics; classification and unresolved policies stay explicit."""
from datetime import date
from typing import Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from app.services.card_benefit_parsers import KST
from app.services.card_program_calculation import grace_end


class MrLifeScenario(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: int = Field(strict=True, gt=0, le=100000000)
    issuer_category: str = Field(min_length=1, max_length=100)
    issuer_classification_confirmed: StrictBool = False
    purchase_at: AwareDatetime
    payment_path: Literal['card', 'kakao', 'naver', 'toss', 'cash', 'currency']
    prior_spend_krw: int | None = Field(default=None, strict=True, ge=0)
    prior_spend_month: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    prior_spend_basis_confirmed: StrictBool = False
    first_use_registered_on: date | None = None
    registration_date_confirmed: StrictBool = False
    remaining_reward_caps: dict[str, StrictInt] = Field(default_factory=dict)
    remaining_counts: dict[str, StrictInt] = Field(default_factory=dict)
    all_time_services_included: StrictBool = False
    count_periods_confirmed: StrictBool = False
    excluded_transaction: StrictBool | None = None

    @model_validator(mode='after')
    def nonnegative(self):
        if any(v < 0 for v in [*self.remaining_reward_caps.values(), *self.remaining_counts.values()]):
            raise ValueError('negative_time_remaining_limit')
        return self


def validate_policy(payload):
    """Accept the supported parser contract, including its unresolved policies."""
    from app.services.card_mr_life import parse_mr_life
    # Rebuild from the bound full policy plus identity and section boundaries.
    # This checks every numeric field, category, time window, and shared scope.
    if not isinstance(payload, dict) or not isinstance(payload.get('policy_raw'), str):
        raise ValueError('unsupported_time_policy')
    original = parse_mr_life('상품명: 신한카드 Mr.Life 혜택 안내 TIME 할인 할인한도 '
        + payload['policy_raw'] + ' 주말 할인 할인마트 ')
    original.pop('evidence')
    if original != payload:
        raise ValueError('time_policy_contract_changed')
    return payload


def calculate_mr_life(payload, scenario: MrLifeScenario):
    policy = validate_policy(payload)
    at = scenario.purchase_at.astimezone(KST)
    group = policy['limit_groups'][0]
    prefix = 'program:shinhan_mr_life_time'
    cap_key = f'{prefix}:time_shared:{at:%Y-%m}'
    rule = next((r for r in policy['rules'] if scenario.issuer_category in r['scope']['categories']), None)
    failed, unknown = [], []
    result = {'status': 'conditional', 'estimated_saving': None, 'failed': failed, 'unknown': unknown,
        'tier': None, 'grace_eligible': None, 'cap_keys': [cap_key], 'count_keys': [], 'cap_unit': 'reward_krw',
        'arithmetic': None, 'basis': '관리자 가상 시나리오 진단. 거래·잔여 한도·보유 카드·게시 상태를 변경하지 않음'}
    if not rule:
        failed.append('지원하지 않는 카드사 업종')
        result['status'] = 'ineligible'
        return result
    if not scenario.issuer_classification_confirmed:
        unknown.append('해당 매장의 카드사 업종 확인 필요')
    if scenario.payment_path in ('cash', 'currency'):
        failed.append('카드 결제 아님')
    elif scenario.payment_path != 'card':
        unknown.append('간편결제 경로의 카드사 업종 인식 미확인')
    if scenario.excluded_transaction is None:
        unknown.append('거래 제외 조건 확인 필요')
    elif scenario.excluded_transaction:
        failed.append('할인 제외 거래')
    window = rule['time_window']
    if window:
        seconds = at.hour*3600 + at.minute*60 + at.second + at.microsecond/1000000
        if seconds in (window['start_hour']*3600, window['end_hour']*3600):
            unknown.append('야간 시간 경계의 포함 정책 미확인')
        elif not (seconds > window['start_hour']*3600 or seconds < window['end_hour']*3600):
            failed.append('야간 할인 시간 밖')
    previous = at.date().replace(day=1)
    previous = previous.replace(year=previous.year-1, month=12) if previous.month == 1 else previous.replace(month=previous.month-1)
    known = (scenario.prior_spend_krw is not None and scenario.prior_spend_basis_confirmed
        and scenario.prior_spend_month == previous.strftime('%Y-%m'))
    if not known:
        unknown.append('전월 실적 및 제외 항목·교통·해외 귀속 확인 필요')
    tier = next((t['tier_key'] for t in policy['spend_tiers'] if known
        and scenario.prior_spend_krw >= t['min_prior_spend_krw']
        and (t['max_prior_spend_exclusive_krw'] is None
             or scenario.prior_spend_krw < t['max_prior_spend_exclusive_krw'])), None)
    grace = None
    if scenario.registration_date_confirmed and scenario.first_use_registered_on:
        registered = scenario.first_use_registered_on
        grace = registered <= at.date() <= grace_end(registered)
        if registered > at.date():
            failed.append('결제일보다 늦은 최초 사용등록일')
    if tier is None and known:
        if grace is True:
            tier = 'standard'
        elif grace is False:
            failed.append('실적 미달·신규 유예 종료')
        else:
            unknown.append('실적 미달·신규 유예 확인 필요')
    result.update(tier=tier, grace_eligible=grace)
    daily_key = f'{prefix}:{rule["rule_key"]}:day:{at:%Y-%m-%d}'
    monthly_key = f'{prefix}:{rule["rule_key"]}:month:{at:%Y-%m}'
    result['count_keys'] = [daily_key, monthly_key]
    if not scenario.count_periods_confirmed:
        unknown.append('KST 일·월 기준 서비스 영역별 잔여 횟수 확인 필요')
    for key, maximum in [(daily_key, rule['daily_count_cap']), (monthly_key, rule['monthly_count_cap'])]:
        count = scenario.remaining_counts.get(key)
        if count is None:
            unknown.append('서비스 영역별 잔여 횟수 누락: '+key)
        elif count > maximum:
            unknown.append('잔여 횟수가 공식 최대 횟수를 초과함: '+key)
        elif count == 0 and scenario.count_periods_confirmed:
            failed.append('서비스 영역별 할인 횟수 소진: '+key)
    if not scenario.all_time_services_included:
        unknown.append('편의점·식음료·병원·약국·세탁·온라인쇼핑·택시 통합 잔여 할인액 확인 필요')
    remaining = scenario.remaining_reward_caps.get(cap_key)
    usable = None
    if remaining is None:
        unknown.append('TIME 공유 월 잔여 할인액 누락')
    elif remaining == 0:
        failed.append('TIME 공유 월 할인 한도 소진')
    elif tier:
        if remaining > group['caps_by_spend_tier_krw'][tier]:
            unknown.append('잔여 할인액이 실적 구간의 최대 한도를 초과함')
        elif scenario.all_time_services_included:
            usable = remaining
    principal = min(scenario.amount, rule['max_eligible_payment_per_transaction_krw'])
    numerator = principal*rule['rate_bps']
    result['arithmetic'] = {'eligible_payment_krw': principal, 'numerator': numerator, 'denominator': 10000,
        'transaction_reward_cap_krw': rule['max_reward_per_transaction_krw'],
        'shared_remaining_reward_krw': usable,
        'conditional_reward_numerator': None if usable is None else min(
            numerator, rule['max_reward_per_transaction_krw']*10000, usable*10000),
        'rounding_rule': 'unresolved'}
    # User assertions cannot complete a missing official policy.
    unknown.extend(['원 단위 계산 정책 미확인', '전체 거래 제외 정책 미확인',
        '기본 실적 귀속 정책 미확인', '할인 적용 기간 정책 미확인'])
    result['status'] = 'ineligible' if failed else 'conditional'
    return result
