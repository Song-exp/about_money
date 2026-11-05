"""Deep Oil diagnostics: eligible purchase principal is distinct from reward money.

This module has no persistence or publication effects. Unresolved official policies
remain unresolved; request data cannot supply a rounding or boundary policy.
"""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.services.card_benefit_parsers import KST
from app.services.card_program_calculation import SpendTier, grace_end


class DeepOilScenario(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: int = Field(strict=True, gt=0, le=100000000)
    brand: str = Field(min_length=1, max_length=100)
    purchase_at: AwareDatetime
    payment_path: Literal['card', 'kakao', 'naver', 'toss', 'cash', 'currency']
    channel: Literal['offline', 'online']
    prior_spend_krw: int | None = Field(default=None, strict=True, ge=0)
    prior_spend_month: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    prior_spend_basis_confirmed: bool = False
    first_use_registered_on: date | None = None
    registration_date_confirmed: bool = False
    remaining_eligible_payment_caps: dict[str, StrictInt] = Field(default_factory=dict)
    family_and_all_living_services_included: bool = False
    excluded_item_present: bool | None = None
    excluded_branch: bool | None = None
    issuer_classification_confirmed: bool = False
    other_conditions_confirmed: bool = False

    @model_validator(mode='after')
    def nonnegative_caps(self):
        if any(value < 0 for value in self.remaining_eligible_payment_caps.values()):
            raise ValueError('negative_remaining_eligible_payment_cap')
        return self


class LivingRule(BaseModel):
    rule_key: Literal['convenience', 'coffee']
    reward_method: Literal['statement_discount']
    calculation_type: Literal['percent']
    rate_bps: int = Field(strict=True, gt=0, le=10000)
    scope: dict
    limit_group: Literal['living_eligible_payment']
    channel: Literal['unresolved', 'offline', 'any']
    rounding_rule: Literal['unresolved', 'floor_per_transaction', 'half_up_per_transaction']


class EligiblePaymentGroup(BaseModel):
    group_key: Literal['living_eligible_payment']
    scope: Literal['living_services_cardholder_and_family']
    period: Literal['calendar_month_kst']
    cap_unit: Literal['eligible_payment_krw']
    caps_by_spend_tier_krw: dict[str, StrictInt]
    shared_across_brands: Literal[True]
    shared_with_family_cards: Literal[True]
    includes_unextracted_services: list[Literal['taxi']]
    allocation: Literal['transaction_order']
    # The current official snapshot does not specify how to treat a transaction
    # straddling the remaining principal cap. Never assume partial allocation.
    boundary_policy: Literal['unresolved', 'partial_payment', 'whole_transaction_only'] = 'unresolved'


class DeepOilPolicy(BaseModel):
    program_key: Literal['shinhan_deep_oil_living']
    rules: list[LivingRule]
    limit_groups: list[EligiblePaymentGroup] = Field(min_length=1, max_length=1)
    spend_tiers: list[SpendTier]
    prior_spend_basis: dict
    new_card_grace: dict

    @model_validator(mode='after')
    def consistent(self):
        tiers = self.spend_tiers
        if (len(tiers) != 2 or [t.tier_key for t in tiers] != ['standard', 'higher']
                or tiers[0].min_prior_spend_krw <= 0
                or tiers[0].max_prior_spend_exclusive_krw != tiers[1].min_prior_spend_krw
                or tiers[1].min_prior_spend_krw <= tiers[0].min_prior_spend_krw
                or tiers[1].max_prior_spend_exclusive_krw is not None):
            raise ValueError('unsupported_living_tiers')
        if len(self.rules) != 2 or {r.rule_key for r in self.rules} != {'convenience', 'coffee'}:
            raise ValueError('unsupported_living_rules')
        for rule in self.rules:
            brands = rule.scope.get('brands')
            if (rule.scope.get('type') != 'brands' or not isinstance(brands, list) or not brands
                    or not all(isinstance(b, str) and b.strip() for b in brands)):
                raise ValueError('invalid_living_brands')
        caps = self.limit_groups[0].caps_by_spend_tier_krw
        if (set(caps) != {'standard', 'higher'} or not 0 < caps['standard'] <= caps['higher']
                or self.limit_groups[0].includes_unextracted_services != ['taxi']):
            raise ValueError('invalid_living_caps')
        if self.prior_spend_basis != {
                'default': 'previous_calendar_month_transaction_kst', 'family_cards_included': True,
                'transport': 'two_months_before', 'overseas': 'acquisition_date'}:
            raise ValueError('unsupported_living_spend_basis')
        grace = self.new_card_grace
        if (grace.get('basis') != 'first_card_use_registration'
                or grace.get('until') != 'end_of_following_month_kst'
                or grace.get('applied_tier') != 'standard'
                or grace.get('higher_tier_effective') != 'following_month'
                or grace.get('higher_spend_threshold_krw') != tiers[1].min_prior_spend_krw):
            raise ValueError('unsupported_living_grace')
        return self


def calculate_deep_oil(payload, scenario: DeepOilScenario):
    policy = DeepOilPolicy.model_validate(payload)
    at = scenario.purchase_at.astimezone(KST)
    previous = at.date().replace(day=1)
    previous = previous.replace(year=previous.year-1, month=12) if previous.month == 1 else previous.replace(month=previous.month-1)
    failed, unknown = [], []
    group = policy.limit_groups[0]
    key = f'program:{policy.program_key}:{group.group_key}:{at:%Y-%m}'
    rule = next((r for r in policy.rules if scenario.brand in r.scope['brands']), None)
    if rule is None:
        return {'status': 'ineligible', 'estimated_saving': None, 'failed': ['지원하지 않는 브랜드·서비스'],
                'unknown': [], 'tier': None, 'cap_keys': [key], 'cap_unit': group.cap_unit}
    if rule.channel == 'unresolved':
        unknown.append('가맹점 채널 공식 적용 정책 미확인')
    elif rule.channel != 'any' and scenario.channel != rule.channel:
        failed.append('지원하지 않는 결제 채널')
    if scenario.payment_path != 'card':
        unknown.append('간편결제 경로의 카드사 가맹점 인식 조건 미확인')
    for value, label in [(scenario.excluded_item_present, '제외 항목'), (scenario.excluded_branch, '제외 지점')]:
        if value is None:
            unknown.append(label + ' 여부 확인 필요')
        elif value:
            failed.append(label + ' 해당')
    if not scenario.issuer_classification_confirmed:
        unknown.append('카드사 가맹점 분류 확인 필요')
    if not scenario.other_conditions_confirmed:
        unknown.append('나머지 적용 조건 확인 필요')
    spend = scenario.prior_spend_krw
    known = (spend is not None and scenario.prior_spend_basis_confirmed
             and scenario.prior_spend_month == previous.strftime('%Y-%m'))
    if not known:
        unknown.append('전월 실적의 가족 합산·제외 항목·교통 및 해외 귀속 기준 확인 필요')
    tier = next((t.tier_key for t in policy.spend_tiers if known and spend >= t.min_prior_spend_krw
                 and (t.max_prior_spend_exclusive_krw is None or spend < t.max_prior_spend_exclusive_krw)), None)
    grace = None
    if scenario.registration_date_confirmed and scenario.first_use_registered_on is not None:
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
            unknown.append('실적 미달·신규 유예 자격 확인 필요')
    remaining = scenario.remaining_eligible_payment_caps.get(key)
    if not scenario.family_and_all_living_services_included:
        unknown.append('가족카드·편의점·커피·택시 통합 잔여 이용금액 확인 필요')
    principal = None
    if remaining is None:
        unknown.append('할인 대상 잔여 이용금액 확인 필요')
    elif remaining == 0:
        failed.append('할인 대상 이용금액 한도 소진')
    elif tier:
        nominal = group.caps_by_spend_tier_krw[tier]
        if remaining > nominal:
            unknown.append('잔여 이용금액이 실적 구간의 최대 한도를 초과함')
        elif scenario.amount <= remaining:
            principal = scenario.amount
        elif group.boundary_policy == 'partial_payment':
            principal = remaining
        elif group.boundary_policy == 'whole_transaction_only':
            failed.append('잔여 이용금액 한도를 넘는 결제')
        else:
            unknown.append('잔여 이용금액 한도를 걸치는 결제의 부분 적용 정책 미확인')
    numerator = None if principal is None else principal * rule.rate_bps
    saving = None
    if rule.rounding_rule == 'unresolved':
        unknown.append('원 단위 계산 정책 미확인')
    elif numerator is not None:
        saving = (numerator + (5000 if rule.rounding_rule == 'half_up_per_transaction' else 0)) // 10000
    return {'status': 'ineligible' if failed else 'conditional' if unknown else 'eligible',
            'estimated_saving': None if failed or unknown else saving, 'failed': failed, 'unknown': unknown,
            'tier': tier, 'grace_eligible': grace, 'cap_keys': [key], 'cap_unit': group.cap_unit,
            'arithmetic': {'eligible_payment_krw': principal, 'numerator': numerator, 'denominator': 10000,
                           'rounding_rule': rule.rounding_rule, 'boundary_policy': group.boundary_policy},
            'basis': '관리자 가상 시나리오 진단. 거래·한도 차감·게시·사용자 보유 정보는 변경하지 않음'}
