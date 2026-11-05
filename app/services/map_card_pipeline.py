"""Official capture -> deterministic extraction -> validation -> atomic publication."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import select
from app.core.config import settings
from app.models.card import Card, CardBenefit, BenefitType
from app.models.map_verification import MapEvidence, MapReviewEvent
from app.services.map_card_extractors import KB_URL, KB_CARD, BRANDS, extract_pengsoo
from app.services.map_verification import aware, item_hash, today_kst, evidence_status

POLICY_PATH = Path(__file__).resolve().parents[2] / 'data/map_card_pipeline_policies.json'


def policy_sources(db):
    if not settings.map_auto_validate_enabled:
        return []
    items = db.scalars(select(CardBenefit).join(Card).where(Card.card_key == KB_CARD,
        CardBenefit.brand_key.in_(BRANDS), CardBenefit.is_active.is_(True))).all()
    return [{'kind': 'card', 'item_id': item.id, 'url': KB_URL} for item in items]


def automatic_target(source):
    return settings.map_auto_validate_enabled and source.get('target_kind') == 'card' and source['url'] == KB_URL


def needs_full_validation(db, source):
    if not automatic_target(source):
        return False
    row = db.get(MapEvidence, ('card', source['item_id']))
    if row and row.status == 'revoked':
        return False
    return not row or row.status != 'approved' or not row.verified_at or (
        aware(row.verified_at) <= datetime.now(timezone.utc)-timedelta(hours=settings.map_auto_recheck_hours))


def _publish_card(db, source, content):
    if not automatic_target(source):
        return {'status': 'manual_review', 'reason': 'issuer_adapter_not_supported'}
    item = db.get(CardBenefit, source['item_id'])
    row = db.get(MapEvidence, ('card', source['item_id']))
    if row.status == 'revoked':
        return {'status': 'revoked', 'reason': 'manual_revocation_preserved'}
    try:
        if item.card.card_key != KB_CARD or item.brand_key not in BRANDS:
            raise ValueError('product_target_mismatch')
        parsed = extract_pengsoo(content)
        policy = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
        if parsed['policy_hash'] != policy['policy_hash'] or policy['source_url'] != source['url']:
            raise ValueError('unreviewed_policy_or_layout_change')
        if item.valid_from or item.valid_to:
            raise ValueError('legacy_period_requires_review')
        if not needs_full_validation(db, source) and row.item_hash == item_hash(item):
            return {'status': evidence_status(row, item), 'reason': 'validation_still_current'}
        value = parsed['benefits'][item.brand_key]
        from app.services.map_approval import Review, apply_benefit_review
        rules = {**policy['rules'], 'value': value['value'], 'min_payment': value['minimum'],
                 'per_transaction_cap': value['cap'], 'monthly_cap_tiers': parsed['monthly_cap_tiers'],
                 'prior_spend_required': parsed['monthly_cap_tiers'][0]['min_spend'],
                 'grace_monthly_cap': parsed['grace_monthly_cap']}
        body = Review(expected_hash=row.content_hash, action='approved', reviewer='issuer_adapter:kb_pengsoo_v1',
            reason='자동 원문 검증: 검토된 정책/제외조건 지문, 상품, 요약·상세 숫자 일치 및 한도 계산 대조를 통과함.',
            indefinite=True, recheck_days=7, rules=rules, evidence_quotes={**policy['quotes'],
                'value': value['value_quote'], 'scope': value['value_quote'],
                'minimum': value['minimum_quote'], 'transaction_cap': value['cap_quote']})
        # Publish native display fields and structured rules in the same transaction.
        item.benefit_type = BenefitType.PERCENT
        item.value_min = item.value_max = float(value['value'])
        item.benefit_text = f"{value['value']}% 환급할인"
        item.min_payment_krw, item.min_payment_raw = value['minimum'], f"{value['minimum']:,}원 이상"
        item.cap_text = f"건별 최대 {value['cap']:,}원 · 실적 구간별 계열 월 통합 한도"
        item.source_url, item.source_as_of = source['url'], today_kst()
        row.item_hash = item_hash(item)
        db.flush()
        result = apply_benefit_review('card', item.id, body, db, commit=False)
        db.add(MapReviewEvent(kind='card', item_id=item.id, action='auto_validation', at=datetime.now(timezone.utc),
            payload={'adapter': 'kb_pengsoo_v1', 'policy_hash': parsed['policy_hash'],
                     'content_hash': row.content_hash, 'item_hash': row.item_hash, 'result': result}))
        return {**result, 'reason': 'validated_and_published'}
    except (ValueError, KeyError) as exc:
        row.status, row.verified_at = 'pending', None
        row.reason = str(exc)[:150]
        return {'status': 'pending', 'reason': row.reason}


def publish_card(db, source, content):
    from fastapi import HTTPException
    try:
        with db.begin_nested():
            return _publish_card(db, source, content)
    except HTTPException as exc:
        row = db.get(MapEvidence, ('card', source['item_id']))
        row.status, row.verified_at, row.reason = 'pending', None, 'approval_guard_rejected'
        return {'status': 'pending', 'reason': row.reason, 'detail': exc.detail}
