"""Shared source validation guard for administrative and scheduled publication."""
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.models.map_verification import MapEvidence, MapReviewEvent
from app.services.map_verification import MODELS, BenefitRules, evidence_status, item_hash, aware


class Review(BaseModel):
    expected_hash: str = Field(min_length=64, max_length=64)
    action: Literal['approved', 'revoked']
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=10, max_length=2000)
    rules: BenefitRules | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    indefinite: bool = False
    recheck_days: int = Field(default=7, ge=1, le=30)
    evidence_quotes: dict[str, str] = Field(default_factory=dict)


def apply_benefit_review(kind: str, item_id: int, body: Review, db: Session, *, commit=True):
    item = db.get(MODELS[kind], item_id) if kind in MODELS else None
    row = db.get(MapEvidence, (kind, item_id))
    if not item or not row: raise HTTPException(404, '자료 또는 근거 없음')
    if row.content_hash != body.expected_hash or row.item_hash != item_hash(item):
        raise HTTPException(409, '원문 또는 혜택이 변경되어 다시 수집·검토해야 합니다')
    if body.action == 'approved':
        # Bind each core decision to the fetched version, rather than accepting
        # a generic official URL and unchecked numbers entered by a reviewer.
        from app.services.map_verification import normalize_location_text
        required = {'product', 'value', 'minimum', 'prior_spend', 'scope', 'period'}
        if body.rules and body.rules.per_transaction_cap is not None: required.add('transaction_cap')
        if body.rules and (body.rules.monthly_cap is not None or body.rules.monthly_cap_tiers): required.add('monthly_cap')
        if body.rules and body.rules.grace_monthly_cap is not None: required.add('grace')
        source_text = normalize_location_text(row.source_text)
        if any(not body.evidence_quotes.get(key, '').strip() or len(body.evidence_quotes[key]) > 500
               or normalize_location_text(body.evidence_quotes[key]) not in source_text for key in required):
            raise HTTPException(422, '상품·할인·최소 금액·실적·범위·기간·한도의 원문 근거 구절이 필요합니다')
        value_quote = body.evidence_quotes['value']
        if not any(word in value_quote for word in ('할인', '환급', '캐시백')):
            raise HTTPException(422, '적립·가맹·가격 정보를 할인 근거로 승인할 수 없습니다')
        if body.rules and body.rules.type == 'percent':
            import re
            from decimal import Decimal
            rates = re.findall(r'(\d+(?:\.\d+)?)\s*(?:[~～–-]\s*\d+(?:\.\d+)?)?\s*%', value_quote)
            if body.rules.value not in [Decimal(rate) for rate in rates]:
                raise HTTPException(422, '승인 할인율이 근거 구절의 할인율과 다릅니다')
        if kind == 'card':
            expected_product = normalize_location_text(item.card.card_name)
            quoted_product = normalize_location_text(body.evidence_quotes['product'])
            if expected_product not in quoted_product and expected_product.removeprefix('kb국민') not in quoted_product:
                raise HTTPException(422, '원문에 명시된 카드 상품과 승인 대상 상품이 다릅니다')
        if aware(row.fetched_at) < datetime.now(timezone.utc) - timedelta(days=30):
            raise HTTPException(422, '원문을 다시 수집한 뒤 승인해 주세요')
        if not body.rules or not body.rules.scope_confirmed:
            raise HTTPException(422, '적용 범위를 확인한 구조화 조건이 필요합니다')
        if body.indefinite == (body.valid_to is not None):
            raise HTTPException(422, '종료일 또는 무기한 근거 중 하나가 필요합니다')
        if body.valid_to and body.valid_from and body.valid_to < body.valid_from:
            raise HTTPException(422, '기간 오류')
        # Currency acceptance and community menu prices cannot become discount rules.
        if kind == 'local' and item.discount_type in ('currency', 'menu_price'):
            raise HTTPException(422, '가맹 여부·제보 가격은 할인 승인 대상이 아닙니다')
        row.rules = body.rules.model_dump(mode='json')
        row.valid_from, row.valid_to, row.indefinite = body.valid_from, body.valid_to, body.indefinite
        row.verified_at = datetime.now(timezone.utc)
        row.recheck_at = row.verified_at + timedelta(days=body.recheck_days)
    row.status, row.reviewer, row.reason = body.action, body.reviewer, body.reason
    db.add(MapReviewEvent(kind=kind, item_id=item_id, action=body.action, at=datetime.now(timezone.utc),
                         payload=body.model_dump(mode='json')))
    if commit:
        db.commit()
    return {'status': evidence_status(row, item)}


