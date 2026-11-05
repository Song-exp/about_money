"""Fail-closed source approval, identity matching and payment estimation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import re
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator, AwareDatetime
from app.models.card import CardBenefit
from app.models.store import Store, StoreOffer
from app.models.local_benefit import LocalBenefitMerchant
from app.models.map_verification import MapEvidence, MapLocation

MODELS = {'card': CardBenefit, 'offer': StoreOffer, 'local': LocalBenefitMerchant}


def normalize_location_text(value):
    return ''.join((value or '').replace('서울특별시', '서울').replace('서울시', '서울').split()).casefold()


def location_address_key(value):
    text = re.sub(r'\([^)]*\)', '', value or '').strip()
    # Pin identity is the road and building number. Preserve shop/floor text in
    # the original address; do not mistake it for part of a different building.
    road = re.search(r'^((?:서울특별시|서울시|서울)\s+[가-힣]+구\s+[가-힣A-Za-z0-9·]+(?:로|길)\s+\d+(?:-\d+)?)(?=\s|$)', text)
    if road:
        suffix = text[road.end():].strip()
        if not suffix or re.search(r'(?:\d+층|지하\s*\d*층|B\d+|\d+호)(?:\s|$)', suffix, re.I):
            return normalize_location_text(road[1])
    return normalize_location_text(text)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def item_snapshot(item):
    # Ignore confidence flags, but bind approval to every benefit condition and identity.
    snapshot = {c.name: getattr(item, c.name) for c in item.__table__.columns
                if c.name not in {'created_at', 'confidence'}}
    if isinstance(item, CardBenefit) and item.card:
        snapshot['card_identity'] = {'key': item.card.card_key, 'name': item.card.card_name, 'issuer': item.card.issuer}
    if isinstance(item, StoreOffer) and item.store:
        snapshot['store_identity'] = {'brand': item.store.brand, 'branch': item.store.branch, 'address': item.store.address}
    return json.loads(json.dumps(snapshot, default=str))


def item_hash(item):
    return digest(item_snapshot(item))


def location_hash(item):
    return digest({'name': getattr(item, 'merchant_name', getattr(item, 'brand', '')),
                   'branch': getattr(item, 'branch', ''), 'address': item.address})


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def today_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def evidence_status(evidence, item, now=None):
    now = now or datetime.now(timezone.utc)
    today = aware(now).astimezone(ZoneInfo('Asia/Seoul')).date()
    if not evidence:
        return 'unverified'
    if evidence.status != 'approved':
        return evidence.status
    if evidence.item_hash != item_hash(item):
        return 'changed'
    if aware(evidence.recheck_at) <= now:
        return 'recheck_required'
    if evidence.valid_from and evidence.valid_from > today:
        return 'not_started'
    if evidence.valid_to and evidence.valid_to < today:
        return 'expired'
    if not evidence.verified_at or not (evidence.valid_to or evidence.indefinite):
        return 'unverified'
    return 'approved'


class SpendCapTier(BaseModel):
    model_config = ConfigDict(extra='forbid')
    min_spend: int = Field(ge=0)
    cap: int = Field(ge=0)


class BenefitRules(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['percent', 'amount']
    value: Decimal = Field(gt=0, le=10000000)
    min_payment: int = Field(ge=0)
    per_transaction_cap: int | None = Field(default=None, ge=0)
    monthly_cap: int | None = Field(default=None, ge=0)
    monthly_cap_tiers: list[SpendCapTier] = Field(default_factory=list)
    monthly_cap_group: str | None = Field(default=None, min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    prior_spend_group: str | None = Field(default=None, min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    grace_monthly_cap: int | None = Field(default=None, ge=0)
    prior_spend_required: int = Field(ge=0)
    payment_paths: list[str] = Field(min_length=1)
    branches: list[str] = Field(default_factory=list)
    excluded_branches: list[str] = Field(default_factory=list)
    weekdays: list[int] = Field(default_factory=list)
    start_hour: int | None = Field(default=None, ge=0, le=23)
    end_hour: int | None = Field(default=None, ge=1, le=24)
    credentials: list[str] = Field(default_factory=list)
    excluded_items: list[str] = Field(default_factory=list)
    other_conditions: list[str] = Field(default_factory=list)
    scope_confirmed: bool = False

    @model_validator(mode='after')
    def bounds(self):
        if self.grace_monthly_cap is not None and (not self.prior_spend_group or not self.monthly_cap_group):
            raise ValueError('grace_group_required')
        if self.monthly_cap_tiers:
            if self.monthly_cap is not None:
                raise ValueError('ambiguous_monthly_cap')
            thresholds = [tier.min_spend for tier in self.monthly_cap_tiers]
            if thresholds != sorted(set(thresholds)) or thresholds[0] < self.prior_spend_required:
                raise ValueError('invalid_spend_tiers')
            if not self.monthly_cap_group:
                raise ValueError('shared_cap_group_required')
        if self.type == 'percent' and self.value > 100:
            raise ValueError('invalid_rate')
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError('invalid_weekday')
        if (self.start_hour is None) != (self.end_hour is None):
            raise ValueError('incomplete_hours')
        if self.start_hour is not None and self.start_hour >= self.end_hour:
            raise ValueError('invalid_hours')
        if not set(self.payment_paths) <= {'card', 'kakao', 'naver', 'toss', 'cash', 'currency'}:
            raise ValueError('invalid_payment_path')
        return self


class PaymentContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: int = Field(gt=0, le=100000000)
    path: Literal['card', 'kakao', 'naver', 'toss', 'cash', 'currency']
    prior_spend: dict[int, int] = Field(default_factory=dict)
    prior_spend_groups: dict[str, int] = Field(default_factory=dict)
    grace_status_groups: dict[str, Literal['eligible', 'not_eligible']] = Field(default_factory=dict)
    remaining_caps: dict[str, int] = Field(default_factory=dict)
    credentials: list[str] = Field(default_factory=list)
    credentials_confirmed: bool = False
    excluded_items_confirmed_absent: bool = False
    other_conditions_confirmed: bool = False
    program_modes: dict[str, Literal['offline', 'online', 'anywhere', 'donation']] = Field(default_factory=dict)
    program_effective_at: dict[str, AwareDatetime] = Field(default_factory=dict)
    program_daily_uses: dict[str, int] = Field(default_factory=dict)
    program_monthly_uses: dict[str, int] = Field(default_factory=dict)
    program_first_transaction: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode='after')
    def nonnegative(self):
        if any(v < 0 for v in [*self.prior_spend.values(), *self.prior_spend_groups.values(), *self.remaining_caps.values(),
                              *self.program_daily_uses.values(), *self.program_monthly_uses.values()]):
            raise ValueError('negative_context')
        return self


def estimate(rules, context, branch, key, card_id=None, now=None):
    now = now or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Seoul'))
    failed, unknown = [], []
    if not rules.scope_confirmed: unknown.append('적용 지점·업종 확인 필요')
    if context.path not in rules.payment_paths: failed.append('결제 경로 불일치')
    if context.amount < rules.min_payment: failed.append('최소 결제금액 미달')
    if rules.branches and branch not in rules.branches: failed.append('대상 지점 아님')
    if branch in rules.excluded_branches: failed.append('제외 지점')
    if rules.weekdays and now.weekday() not in rules.weekdays: failed.append('적용 요일 아님')
    if rules.start_hour is not None and not rules.start_hour <= now.hour < rules.end_hour: failed.append('적용 시간 아님')
    spend = context.prior_spend_groups.get(rules.prior_spend_group) if rules.prior_spend_group else context.prior_spend.get(card_id)
    grace = context.grace_status_groups.get(rules.prior_spend_group) if rules.grace_monthly_cap is not None else 'not_eligible'
    if grace is None: unknown.append('신규 발급 실적 유예 여부 확인 필요')
    if (rules.prior_spend_required or rules.monthly_cap_tiers) and grace != 'eligible':
        if spend is None: unknown.append('동일계열 합산 전월 실적 확인 필요' if rules.prior_spend_group else '전월 실적 확인 필요')
        elif spend < rules.prior_spend_required:
            (unknown if grace is None else failed).append('실적 미달·유예 적용 여부 확인 필요' if grace is None else '전월 실적 미달')
    if rules.credentials:
        if not set(rules.credentials) <= set(context.credentials): failed.append('필요 자격 없음')
        elif not context.credentials_confirmed: unknown.append('자격 확인 필요')
    if rules.excluded_items and not context.excluded_items_confirmed_absent: unknown.append('제외 품목 확인 필요')
    if rules.other_conditions and not context.other_conditions_confirmed: unknown.append('추가 조건 확인 필요')
    saved = Decimal(context.amount) * rules.value / 100 if rules.type == 'percent' else rules.value
    saved = min(saved, Decimal(context.amount))
    if rules.per_transaction_cap is not None: saved = min(saved, rules.per_transaction_cap)
    monthly_cap = rules.monthly_cap
    if rules.monthly_cap_tiers:
        if spend is None:
            monthly_cap = min(tier.cap for tier in rules.monthly_cap_tiers)
        else:
            applicable = [tier for tier in rules.monthly_cap_tiers if spend >= tier.min_spend]
            monthly_cap = applicable[-1].cap if applicable else 0
            if not applicable and grace == 'not_eligible': failed.append('실적 구간 미달')
    if rules.grace_monthly_cap is not None and grace != 'not_eligible':
        if grace == 'eligible' and spend is not None and spend >= rules.prior_spend_required:
            unknown.append('유예 한도와 일반 실적 한도의 적용 우선순위 확인 필요')
        monthly_cap = min(monthly_cap, rules.grace_monthly_cap) if monthly_cap else rules.grace_monthly_cap
    cap_key = f'group:{rules.monthly_cap_group}' if rules.monthly_cap_group else key
    if monthly_cap is not None:
        remaining = context.remaining_caps.get(cap_key)
        if remaining is None:
            unknown.append('이번 달 잔여 한도 확인 필요')
            saved = min(saved, monthly_cap)
        else: saved = min(saved, monthly_cap, remaining)
    status = 'ineligible' if failed else 'conditional' if unknown else 'eligible'
    return {'status': status, 'estimated_saving': 0 if failed else int(Decimal(saved).quantize(Decimal('1'), rounding=ROUND_DOWN)),
            'cap_key': cap_key, 'prior_spend_group': rules.prior_spend_group,
            'monthly_cap_limit': monthly_cap,
            'grace_group': rules.prior_spend_group if rules.grace_monthly_cap is not None else None,
            'reasons': failed + unknown, 'basis': '사용자 입력 기준 예상값'}


def verified_location(db, kind, item):
    location = db.get(MapLocation, (kind, item.id))
    return location if location and location.identity_hash == location_hash(item) and aware(location.checked_at) >= datetime.now(timezone.utc)-timedelta(days=90) else None
