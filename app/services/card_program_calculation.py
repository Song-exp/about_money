"""Day arithmetic diagnostics. This module never approves or publishes a program."""
from calendar import monthrange
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator
from app.services.card_benefit_parsers import KST


class DayScenario(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: int = Field(strict=True, gt=0, le=100000000)
    brand: str = Field(min_length=1, max_length=100)
    purchase_at: AwareDatetime
    payment_path: Literal['card', 'kakao', 'naver', 'toss', 'cash', 'currency']
    channel: Literal['offline', 'online']
    prior_spend_krw: int | None = Field(default=None, strict=True, ge=0)
    prior_spend_month: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    prior_spend_exclusions_confirmed: bool = False
    first_use_registered_on: date | None = None
    registration_date_confirmed: bool = False
    remaining_caps: dict[str, StrictInt] = Field(default_factory=dict)
    excluded_item_present: bool | None = None
    excluded_branch: bool | None = None
    issuer_classification_confirmed: bool = False
    other_conditions_confirmed: bool = False

    @model_validator(mode='after')
    def nonnegative_caps(self):
        if any(value < 0 for value in self.remaining_caps.values()):
            raise ValueError('negative_remaining_cap')
        return self


class SpendTier(BaseModel):
    tier_key: str
    min_prior_spend_krw: int = Field(strict=True, ge=0)
    max_prior_spend_exclusive_krw: int | None = Field(default=None, strict=True, gt=0)


class AreaRule(BaseModel):
    rule_key: Literal['coffee', 'shopping']
    reward_method: Literal['statement_discount']
    calculation_type: Literal['percent']
    rate_bps: int = Field(strict=True, gt=0, le=10000)
    scope: dict
    channel: Literal['offline']
    limit_groups: list[str] = Field(min_length=2, max_length=2)
    rounding_rule: Literal['unresolved', 'floor_per_transaction', 'half_up_per_transaction']


class LimitGroup(BaseModel):
    group_key: str
    period: Literal['calendar_month_kst']
    caps_by_spend_tier_krw: dict[str, StrictInt]
    shared_across_brands: Literal[True]


class DayPolicy(BaseModel):
    program_key: Literal['hana_day_offline']
    rules: list[AreaRule]
    limit_groups: list[LimitGroup]
    spend_tiers: list[SpendTier]
    new_card_grace: dict

    @model_validator(mode='after')
    def consistent(self):
        tiers = self.spend_tiers
        if len(tiers) != 2 or [tier.tier_key for tier in tiers] != ['standard', 'higher']:
            raise ValueError('unsupported_day_tiers')
        if (tiers[0].min_prior_spend_krw <= 0 or tiers[0].max_prior_spend_exclusive_krw != tiers[1].min_prior_spend_krw
                or tiers[1].min_prior_spend_krw <= tiers[0].min_prior_spend_krw or tiers[1].max_prior_spend_exclusive_krw is not None):
            raise ValueError('ambiguous_day_tiers')
        if len(self.rules) != 2 or {rule.rule_key for rule in self.rules} != {'coffee', 'shopping'}:
            raise ValueError('unsupported_day_rules')
        groups = {group.group_key: group for group in self.limit_groups}
        if len(groups) != len(self.limit_groups) or set(groups) != {'coffee', 'shopping', 'program_total'}:
            raise ValueError('invalid_day_cap_groups')
        for group in groups.values():
            caps = group.caps_by_spend_tier_krw
            if set(caps) != {'standard', 'higher'} or not all(type(value) is int and value >= 0 for value in caps.values()):
                raise ValueError('invalid_day_caps')
            if caps['standard'] > caps['higher']:
                raise ValueError('invalid_day_cap_order')
        for rule in self.rules:
            if rule.limit_groups != [rule.rule_key, 'program_total']:
                raise ValueError('missing_day_shared_cap')
            if rule.scope.get('type') != 'brands' or not isinstance(rule.scope.get('brands'), list) or not rule.scope['brands']:
                raise ValueError('invalid_day_brand_scope')
            if not all(isinstance(brand, str) and brand.strip() for brand in rule.scope['brands']):
                raise ValueError('invalid_day_brands')
            if any(groups[rule.rule_key].caps_by_spend_tier_krw[key] > groups['program_total'].caps_by_spend_tier_krw[key]
                    for key in ('standard', 'higher')):
                raise ValueError('area_cap_exceeds_program_cap')
        if (self.new_card_grace.get('basis') != 'first_card_use_registration'
                or self.new_card_grace.get('until') != 'end_of_following_month_kst'
                or self.new_card_grace.get('applied_tier') != 'standard'):
            raise ValueError('unsupported_day_grace')
        return self


def grace_end(registered_on):
    month = registered_on.month % 12 + 1
    year = registered_on.year + (registered_on.month == 12)
    return date(year, month, monthrange(year, month)[1])


def calculate_day(payload, scenario):
    """Independent arithmetic; callers must separately gate publication and ownership."""
    policy = DayPolicy.model_validate(payload)
    at = scenario.purchase_at.astimezone(KST)
    current_month = at.strftime('%Y-%m')
    previous = at.date().replace(day=1)
    previous = previous.replace(year=previous.year-1, month=12) if previous.month == 1 else previous.replace(month=previous.month-1)
    failed, unknown = [], []
    rule = next((rule for rule in policy.rules if scenario.brand in rule.scope['brands']), None)
    if rule is None:
        return {'status': 'ineligible', 'estimated_saving': None, 'failed': ['지원하는 오프라인 브랜드 아님'], 'unknown': [], 'tier': None, 'cap_keys': []}
    if scenario.channel != rule.channel:
        failed.append('지원하는 오프라인 결제 아님')
    if scenario.payment_path != 'card':
        unknown.append('해당 결제 경로의 가맹점 인식 조건 미확인')
    for value, label in [(scenario.excluded_item_present, '제외 품목'), (scenario.excluded_branch, '제외 지점')]:
        if value is None:
            unknown.append(label + ' 여부 확인 필요')
        elif value:
            failed.append(label + ' 해당')
    if not scenario.issuer_classification_confirmed:
        unknown.append('카드사 가맹점 분류 확인 필요')
    if not scenario.other_conditions_confirmed:
        unknown.append('나머지 적용 조건 확인 필요')
    spend = scenario.prior_spend_krw
    spend_known = (spend is not None and scenario.prior_spend_exclusions_confirmed
        and scenario.prior_spend_month == previous.strftime('%Y-%m'))
    if not spend_known:
        unknown.append('올바른 전월의 실적 제외 반영 금액 확인 필요')
    tier = next((tier.tier_key for tier in policy.spend_tiers if spend_known and spend >= tier.min_prior_spend_krw
        and (tier.max_prior_spend_exclusive_krw is None or spend < tier.max_prior_spend_exclusive_krw)), None)
    grace = None
    if scenario.registration_date_confirmed and scenario.first_use_registered_on is not None:
        registered = scenario.first_use_registered_on
        if registered > at.date():
            failed.append('결제일보다 늦은 최초 사용등록일')
        grace = registered <= at.date() <= grace_end(registered)
    if tier is None and spend_known:
        if grace is True:
            tier = policy.new_card_grace['applied_tier']
        elif grace is False:
            failed.append('실적 미달·신규 발급 유예 종료')
        else:
            unknown.append('실적 미달·신규 발급 유예 자격 확인 필요')
    cap_keys = [f'program:{policy.program_key}:{group}:{current_month}' for group in rule.limit_groups]
    groups = {group.group_key: group for group in policy.limit_groups}
    available = []
    for group, key in zip(rule.limit_groups, cap_keys):
        remaining = scenario.remaining_caps.get(key)
        if remaining is None:
            unknown.append(group + ' 잔여 한도 확인 필요')
        elif remaining == 0:
            failed.append(group + ' 잔여 한도 소진')
        elif tier:
            cap = groups[group].caps_by_spend_tier_krw[tier]
            if remaining > cap:
                unknown.append(group + ' 잔여 한도가 해당 실적 구간의 최대 한도를 초과함')
            available.append(min(remaining, cap))
    numerator = scenario.amount * rule.rate_bps
    rounded = None
    if rule.rounding_rule == 'unresolved':
        unknown.append('원 단위 계산 정책 미확정')
    elif rule.rounding_rule == 'floor_per_transaction':
        rounded = numerator // 10000
    else:
        rounded = (numerator + 5000) // 10000
    estimated = min(rounded, scenario.amount, *available) if rounded is not None and len(available) == 2 else None
    return {'status': 'ineligible' if failed else 'conditional' if unknown else 'eligible',
        'estimated_saving': None if failed or unknown else estimated, 'failed': failed, 'unknown': unknown,
        'tier': tier, 'grace_eligible': grace, 'cap_keys': cap_keys,
        'arithmetic': {'numerator': numerator, 'denominator': 10000, 'rounding_rule': rule.rounding_rule},
        'basis': '단일 결제 시나리오의 계산 진단. 승인·보유·문서 적용 관계는 별도 검증 필요'}
