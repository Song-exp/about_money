"""Fail-closed approval, store matching and exact amount-tier estimation."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models.card import Card
from app.models.card_collection import CardCollectionSource, CardCollectionAttempt, CardSourceSnapshot, CardProductSource, CardIssuer
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence
from app.models.card_publication import CardProgramPublication
from app.models.map_verification import MapReviewEvent
from app.services.card_benefit_parsers import PARSERS, KST
from app.services.map_verification import aware, verified_location


class ProgramReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['approved', 'revoked']
    expected_payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=10, max_length=2000)
    policy_quote: str = Field(default='', max_length=20000)
    acknowledged_issues: list[str] = Field(default_factory=list)
    recheck_hours: int = Field(default=24, ge=1, le=24)


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def source_binding(db, version, now):
    source = db.get(CardCollectionSource, version.source_id)
    snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
    if not source or not source.enabled or source.last_error or not snapshot or snapshot.source_id != source.id:
        return 'source_unavailable'
    if hashlib.sha256(snapshot.extracted_text.encode()).hexdigest() != snapshot.semantic_sha256:
        return 'snapshot_changed'
    parser = PARSERS.get(source.canonical_url)
    if not parser or version.extractor_version != parser[0]:
        return 'extractor_changed'
    if payload_hash(version.payload) != version.payload_hash:
        return 'payload_changed'
    observed = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
        CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
    if not observed or observed.snapshot_id != snapshot.id:
        return 'source_changed'
    full = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == source.id,
        CardCollectionAttempt.snapshot_id == snapshot.id, CardCollectionAttempt.status == 'captured',
        CardCollectionAttempt.http_status == 200).order_by(CardCollectionAttempt.id.desc()).limit(1))
    if not full or aware(full.attempted_at) < now - timedelta(hours=24) or aware(full.attempted_at) > now:
        return 'source_stale'
    return None


def publication_status(db, publication, now=None):
    now = aware(now or datetime.now(timezone.utc))
    version = db.get(CardBenefitVersion, publication.version_id)
    if publication.status != 'approved' or not version or version.review_state != 'approved':
        return 'revoked'
    if version.publication_state not in ('live', 'scheduled'):
        return 'suspended'
    if aware(publication.recheck_at) <= now:
        return 'recheck_required'
    issue = source_binding(db, version, now)
    if issue:
        return issue
    snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
    if publication.payload_hash != version.payload_hash or publication.snapshot_hash != snapshot.raw_sha256:
        return 'changed'
    payload = version.payload
    try:
        start = aware(datetime.fromisoformat(payload['valid_from']))
        end = aware(datetime.fromisoformat(payload['valid_until_exclusive']))
    except (ValueError, TypeError, KeyError):
        return 'period_unresolved'
    if now < start:
        return 'not_started'
    if now >= end:
        return 'expired'
    return 'approved'


def review_program(db, version_id, body, *, now=None, validate_only=False):
    """Validate with the same guards used by publication; preflight never writes."""
    now = aware(now or datetime.now(timezone.utc))
    query = select(CardBenefitVersion).where(CardBenefitVersion.id == version_id)
    version = db.scalar(query if validate_only else query.with_for_update())
    if not version:
        raise HTTPException(404, '혜택 버전 없음')
    snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
    if not snapshot:
        raise HTTPException(409, 'source_unavailable')
    if body.expected_payload_hash != version.payload_hash or body.expected_snapshot_hash != snapshot.raw_sha256:
        raise HTTPException(409, '검토한 원문 또는 조건이 변경되었습니다')
    publication = db.get(CardProgramPublication, version.program_key)
    if body.action == 'revoked':
        if validate_only:
            raise ValueError('preflight_requires_approval')
        if publication and publication.version_id != version.id:
            raise HTTPException(409, '현재 게시 버전이 아닙니다')
        if publication:
            publication.status = 'revoked'
        version.review_state, version.publication_state = 'revoked', 'suspended'
    else:
        issue = source_binding(db, version, now)
        if issue:
            raise HTTPException(409, issue)
        source = db.get(CardCollectionSource, version.source_id)
        parser = PARSERS[source.canonical_url][1]
        try:
            parsed = parser(snapshot.extracted_text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        expected_evidence = parsed.pop('evidence')
        if parsed != version.payload:
            raise HTTPException(409, '추출 조건을 다시 검증해야 합니다')
        if version.program_key != parsed['program_key']:
            raise HTTPException(409, '프로그램 식별자가 다릅니다')
        actual = db.scalars(select(CardBenefitFieldEvidence).where(CardBenefitFieldEvidence.version_id == version.id)).all()
        actual = {row.field_path: (row.quote, row.start_offset, row.end_offset) for row in actual}
        expected = {row['field_path']: (row['quote'], row['start_offset'], row['end_offset']) for row in expected_evidence}
        if actual != expected:
            raise HTTPException(409, '원문 필드 근거가 변경되었습니다')
        from app.services.card_document_crosscheck import document_checks
        comparison = document_checks(db, version, now=now)
        if comparison['required'] and comparison['status'] != 'partial_match':
            raise HTTPException(409, 'supporting_document_' + comparison['status'])
        issues = set(parsed['issues'])
        supported_issues = {'manual_policy_review_required', 'other_modes_not_extracted'}
        if issues - supported_issues or set(body.acknowledged_issues) != issues:
            raise HTTPException(422, '미해결 조건은 승인할 수 없습니다')
        if parsed['period_status'] != 'explicit' or parsed['program_key'] != 'toss_switch_offline':
            raise HTTPException(422, '지원하지 않는 기간 또는 계산 정책입니다')
        start, end = (datetime.fromisoformat(parsed[key]) for key in ('valid_from', 'valid_until_exclusive'))
        if end <= start or end <= now:
            raise HTTPException(422, '종료된 혜택은 게시할 수 없습니다')
        if body.policy_quote != parsed['exclusions_raw']:
            raise HTTPException(422, '전체 제외 조건의 원문 대조가 필요합니다')
        refs = parsed['product_refs']
        if len(refs) != 1:
            raise HTTPException(422, '상품 연결을 확인해야 합니다')
        query = select(CardProductSource).where(CardProductSource.source_namespace == refs[0]['namespace'],
            CardProductSource.external_product_code == refs[0]['code'])
        product = db.scalar(query if validate_only else query.with_for_update())
        issuer = db.get(CardIssuer, product.issuer_id) if product else None
        if (not product or not issuer or issuer.issuer_key != 'tossbank' or source.issuer_id != issuer.id
                or product.product_name not in snapshot.extracted_text):
            raise HTTPException(422, '카드 상품 식별 근거 없음')
        # Stable source identity, never fuzzy merge a user's existing cards.
        key = product.source_namespace + ':' + product.external_product_code
        card = db.get(Card, product.card_id) if product.card_id else db.scalar(select(Card).where(Card.card_key == key))
        if card and (card.card_name != product.product_name or card.issuer != issuer.name):
            raise HTTPException(409, '연결된 카드 정보가 다릅니다')
        if validate_only:
            return {'can_approve': True, 'blocker': None}
        if card is None:
            card = Card(card_key=key, card_name=product.product_name, issuer=issuer.name)
            db.add(card)
            db.flush()
        if publication and publication.version_id != version.id:
            old = db.get(CardBenefitVersion, publication.version_id)
            old.publication_state = 'superseded'
        if publication is None:
            publication = CardProgramPublication(program_key=version.program_key, version_id=version.id,
                card_id=card.id, status='approved', payload_hash=version.payload_hash,
                snapshot_hash=snapshot.raw_sha256, reviewed_at=now, recheck_at=now)
            db.add(publication)
        publication.version_id, publication.card_id, publication.status = version.id, card.id, 'approved'
        publication.payload_hash, publication.snapshot_hash = version.payload_hash, snapshot.raw_sha256
        publication.reviewed_at = now
        publication.recheck_at = min(now + timedelta(hours=body.recheck_hours), end)
        product.card_id = card.id
        product.review_state = 'approved'
        version.review_state = 'approved'
        version.publication_state = 'scheduled' if now < start else 'live'
        # Only a reviewed explicit period may influence collection cadence.
        from app.services.card_collection import next_check
        source.schedule_policy = {**(source.schedule_policy or {}), 'reviewed_end_at': end.isoformat(),
            'early_end_possible': parsed.get('early_end_possible', False)}
        suggested = next_check(source, now)
        source.next_check_at = min(aware(source.next_check_at), suggested) if source.next_check_at else suggested
    db.add(MapReviewEvent(kind='card_program', item_id=version.id, action=body.action, at=now,
        payload=body.model_dump(mode='json')))
    db.commit()
    return {'version_id': version.id, 'status': publication_status(db, publication, now) if publication else 'revoked'}


def program_review_status(db, version, *, now=None):
    """Read-only technical readiness, distinct from a human's policy decision."""
    now = aware(now or datetime.now(timezone.utc))
    snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
    source = db.get(CardCollectionSource, version.source_id)
    body = ProgramReview(action='approved', expected_payload_hash=version.payload_hash,
        expected_snapshot_hash=snapshot.raw_sha256 if snapshot else '0' * 64,
        reviewer='preflight', reason='기술적 승인 가능 여부를 조회합니다.',
        policy_quote=version.payload.get('exclusions_raw', ''), acknowledged_issues=version.payload.get('issues', []))
    try:
        result = review_program(db, version.id, body, now=now, validate_only=True)
    except HTTPException as exc:
        result = {'can_approve': False, 'blocker': str(exc.detail)}
    publication = db.get(CardProgramPublication, version.program_key)
    current = publication is not None and publication.version_id == version.id
    full = db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id == version.source_id,
        CardCollectionAttempt.snapshot_id == version.snapshot_id, CardCollectionAttempt.status == 'captured',
        CardCollectionAttempt.http_status == 200).order_by(CardCollectionAttempt.id.desc()).limit(1))
    from app.services.card_document_crosscheck import document_checks
    comparison = document_checks(db, version, now=now)
    return {**result, 'supporting_document': comparison, 'checked_at': now.isoformat(), 'human_review_required': True,
        'is_current_publication': current,
        'public_status': publication_status(db, publication, now) if current else 'not_published',
        'reviewed_at': aware(publication.reviewed_at).isoformat() if current else None,
        'recheck_at': aware(publication.recheck_at).isoformat() if current else None,
        'full_body_checked_at': aware(full.attempted_at).isoformat() if full else None,
        'next_check_at': aware(source.next_check_at).isoformat() if source and source.next_check_at else None,
        'source_error': source.last_error if source else 'source_missing',
        'unresolved_issues': [issue for issue in version.payload.get('issues', [])
            if issue not in {'manual_policy_review_required', 'other_modes_not_extracted'}]}


def load_public_programs(db, *, card_ids=None, now=None):
    now = aware(now or datetime.now(timezone.utc))
    rows = []
    for publication in db.scalars(select(CardProgramPublication).where(CardProgramPublication.status == 'approved')):
        if card_ids is not None and publication.card_id not in card_ids:
            continue
        if publication_status(db, publication, now) == 'approved':
            version = db.get(CardBenefitVersion, publication.version_id)
            rows.append((publication, version, db.get(Card, publication.card_id),
                         db.get(CardCollectionSource, version.source_id)))
    return rows


def store_programs(db, store, *, card_ids=None, now=None, prepared=None):
    from app.services.card_normalize import store_brand_key
    now = aware(now or datetime.now(timezone.utc))
    if not store.is_active or not verified_location(db, 'store', store):
        return []
    brand = store_brand_key(store.brand)
    matches = []
    for publication, version, card, source in (prepared if prepared is not None else load_public_programs(db, card_ids=card_ids, now=now)):
        for rule in version.payload['rules']:
            if brand not in {store_brand_key(name) for name in rule['scope']['brands']}:
                continue
            group = next(group for group in version.payload['limit_groups'] if group['group_key'] == rule['limit_group'])
            group_key = f'program:{publication.program_key}:{group["group_key"]}'
            matches.append({'program_key': publication.program_key, 'version_id': version.id,
                'card_id': card.id, 'card_name': card.card_name, 'rule_key': rule['rule_key'],
                'tiers': rule['tiers'], 'limit_group': group, 'selection': version.payload['selection'],
                'monthly_key': group_key + ':' + now.astimezone(KST).strftime('%Y-%m'),
                'daily_key': group_key + ':' + now.astimezone(KST).strftime('%Y-%m-%d'),
                'source_url': source.canonical_url, 'valid_until_exclusive': version.payload['valid_until_exclusive'],
                'verified_at': aware(publication.reviewed_at).isoformat(),
                'exclusions': version.payload['exclusions_raw'], 'status': 'conditional',
                'conditions': ['오프라인 모드 선택 후 익일 0시부터 적용', '영역 내 브랜드가 횟수·금액 한도 공유',
                    '간편결제·제외 점포·품목 및 가맹점 분류 확인 필요']})
    return matches


def estimate_program(offer, context, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    failed, unknown = [], []
    program, daily, monthly = offer['program_key'], offer['daily_key'], offer['monthly_key']
    mode = context.program_modes.get(program)
    effective = context.program_effective_at.get(program)
    if mode is None:
        unknown.append('선택 모드 확인 필요')
    elif mode != offer['selection']['mode']:
        failed.append('선택 모드 불일치')
    if effective is None:
        unknown.append('선택 모드 적용 시각 확인 필요')
    elif aware(effective) > now:
        failed.append('선택 모드 적용 전')
    elif aware(effective).astimezone(KST).time().isoformat() != '00:00:00':
        unknown.append('선택 모드 적용일의 한국 시간 0시 확인 필요')
    if context.path != 'card':
        failed.append('카드 직접 결제 조건 확인 필요')
    for key, values, limit, label in (
        (daily, context.program_daily_uses, offer['limit_group']['daily_count_cap'], '오늘 영역 이용 횟수'),
        (monthly, context.program_monthly_uses, offer['limit_group']['monthly_count_cap'], '이번 달 영역 이용 횟수'),
    ):
        if key not in values:
            unknown.append(label + ' 확인 필요')
        elif values[key] >= limit:
            failed.append(label + ' 한도 소진')
    if daily not in context.program_first_transaction:
        unknown.append('오늘 영역 첫 결제 여부 확인 필요')
    elif not context.program_first_transaction[daily]:
        failed.append('오늘 영역 첫 결제 아님')
    if monthly not in context.remaining_caps:
        unknown.append('영역 잔여 금액 한도 확인 필요')
    if not context.excluded_items_confirmed_absent:
        unknown.append('제외 품목 확인 필요')
    if not context.other_conditions_confirmed:
        unknown.append('실적·제외 지점·가맹점 분류 등 나머지 조건 확인 필요')
    tier = next((tier for tier in offer['tiers'] if context.amount >= tier['min_payment_krw'] and (
        tier['max_payment_exclusive_krw'] is None or context.amount < tier['max_payment_exclusive_krw'])), None)
    if tier is None:
        failed.append('최소 결제금액 미달')
    saved = min(tier['amount_krw'], context.remaining_caps.get(monthly, 0),
        offer['limit_group']['monthly_amount_cap_krw']) if tier else 0
    if monthly in context.remaining_caps and context.remaining_caps[monthly] == 0:
        failed.append('영역 잔여 금액 한도 소진')
    return {'key': f'program:{offer["version_id"]}:{offer["rule_key"]}', 'label': offer['card_name'],
        'status': 'ineligible' if failed else 'conditional' if unknown else 'eligible',
        'estimated_saving': None if failed or unknown else saved, 'reasons': failed + unknown,
        'cap_key': monthly, 'daily_key': daily, 'monthly_key': monthly, 'program_key': program}
