"""Authenticated review queue; fetching evidence never approves a benefit."""
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.security import require_admin, current_user
from app.models.map_verification import MapEvidence, MapLocation, MapReviewEvent
from app.services.map_verification import MODELS, BenefitRules, PaymentContext, evidence_status, item_hash, location_hash, estimate, verified_location, aware
from app.services.map_verification import today_kst
from app.services.map_approval import Review, apply_benefit_review
from app.services.card_publication import ProgramReview
from app.services.card_program_calculation import DayScenario
from app.services.card_usage_cap_calculation import DeepOilScenario
from app.services.card_time_calculation import MrLifeScenario

router = APIRouter(prefix='/admin/map', tags=['admin'], dependencies=[Depends(require_admin)])


@router.get('/store-directories')
def store_directory_status(db: Session = Depends(get_db)):
    from app.services.store_directory_collection import directory_status
    return directory_status(db)


class StoreDirectoryReview(BaseModel):
    expected_identity_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    note: str = Field(min_length=1, max_length=2000)


@router.get('/store-directories/place-revalidation')
def store_place_revalidation_status(db: Session = Depends(get_db)):
    from app.services.store_place_scheduler import job_status
    return job_status(db)


@router.get('/store-directories/targets')
def store_directory_targets(provider: Literal['starbucks', 'mega', 'paris', 'outback'],
        limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db)):
    from app.models.store import Store
    from app.services.store_directory_publication import STORE_BRANDS
    rows = db.scalars(select(Store).where(Store.brand == STORE_BRANDS[provider], Store.is_active.is_(True))
        .order_by(Store.id).offset(offset).limit(limit + 1)).all()
    return {'items': [{'id': row.id, 'brand': row.brand, 'branch': row.branch, 'address': row.address}
                     for row in rows[:limit]], 'has_more': len(rows) > limit, 'offset': offset, 'limit': limit}


@router.get('/store-directories/changes')
def store_directory_changes(limit: int = Query(default=50, ge=1, le=100),
                            offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    from app.services.store_directory_review import list_changes
    return list_changes(db, limit, offset)


@router.post('/store-directories/changes/{change_id}/review')
def review_store_directory_change(change_id: int, body: StoreDirectoryReview, db: Session = Depends(get_db)):
    from app.services.store_directory_review import record_review
    try:
        return record_review(db, change_id, body.expected_identity_hash, body.note)
    except ValueError as exc:
        status = 404 if str(exc) == 'store_directory_change_not_found' else 422 if str(exc) == 'review_note_required' else 409
        raise HTTPException(status, str(exc)) from None


class StoreDirectoryPlaceCheck(BaseModel):
    expected_identity_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    official_id: str = Field(pattern=r'^[0-9]{1,30}$')


@router.post('/store-directories/changes/{change_id}/place-check')
def check_store_directory_place(change_id: int, body: StoreDirectoryPlaceCheck, db: Session = Depends(get_db)):
    from app.services.store_directory_revalidation import revalidate_change
    try:
        return revalidate_change(db, change_id, body.official_id, body.expected_identity_hash)
    except ValueError as exc:
        reason = str(exc)
        status = 404 if reason == 'store_directory_change_not_found' else 503 if reason == 'kakao_rest_api_key_required' else 409
        raise HTTPException(status, reason) from None


@router.get('/store-directories/changes/{change_id}/place-checks')
def store_directory_place_checks(change_id: int, limit: int = Query(default=50, ge=1, le=100),
                                offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    from app.services.store_directory_revalidation import list_checks
    return {'items': list_checks(db, change_id, limit, offset), 'limit': limit, 'offset': offset, 'map_publication': False}


class StoreDirectoryApply(BaseModel):
    check_id: int = Field(gt=0)
    store_id: int = Field(gt=0)
    expected_identity_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_store_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)


@router.get('/store-directories/changes/{change_id}/publication-preview')
def store_directory_publication_preview(change_id: int, check_id: int = Query(gt=0), store_id: int = Query(gt=0),
        expected_identity_hash: str = Query(pattern=r'^[0-9a-f]{64}$'), db: Session = Depends(get_db)):
    from app.services.store_directory_publication import publication_preview
    try:
        return publication_preview(db, change_id, check_id, store_id, expected_identity_hash)
    except ValueError as exc:
        raise HTTPException(404 if str(exc).endswith('_not_found') else 409, str(exc)) from None


@router.post('/store-directories/changes/{change_id}/apply')
def apply_store_directory_change(change_id: int, body: StoreDirectoryApply, db: Session = Depends(get_db)):
    from app.services.store_directory_publication import apply_publication
    try:
        return apply_publication(db, change_id, body.check_id, body.store_id, body.expected_identity_hash,
            body.expected_store_version, body.reviewer, body.note)
    except ValueError as exc:
        reason = str(exc)
        status = 422 if reason == 'store_directory_publication_note_required' else 404 if reason.endswith('_not_found') else 409
        raise HTTPException(status, reason) from None


@router.get('/issuer-merchant-captures/{capture_id}/matches')
def issuer_capture_matches(capture_id: int, db: Session = Depends(get_db)):
    from app.models.issuer_merchant_capture import IssuerMerchantCapture
    from app.services.issuer_merchant_capture import capture_matches, merchant_archive_root
    capture = db.get(IssuerMerchantCapture,capture_id)
    if capture is None:
        raise HTTPException(404,'가맹점 조회 기록 없음')
    return capture_matches(db,capture,archive_root=merchant_archive_root())


@router.get('/card-document-imports/{import_id}/review-pack')
def imported_document_review(import_id: int, db: Session = Depends(get_db)):
    from app.services.card_document_review import document_review_pack
    try:
        return document_review_pack(db, import_id)
    except ValueError as exc:
        if str(exc) == 'manual_document_import_not_found':
            raise HTTPException(404, '문서 반입 기록 없음') from None
        raise


@router.get('/card-sources/{source_id}/document')
def source_document(source_id: int, db: Session = Depends(get_db)):
    import hashlib
    from app.models.card_collection import CardCollectionSource,CardSourceSnapshot,CardCollectionAttempt
    from app.models.card_document_text import CardDocumentText
    from app.services.toss_kpass_document import KPASS_URL,KPASS_TEXT_PARSER
    source=db.get(CardCollectionSource,source_id)
    if not source:
        raise HTTPException(404,'출처 없음')
    if source.canonical_url!=KPASS_URL:
        raise HTTPException(422,'K-패스 상세 안내 검토만 지원합니다')
    observed=db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==source.id,
        CardCollectionAttempt.snapshot_id.is_not(None)).order_by(CardCollectionAttempt.id.desc()).limit(1))
    snapshot=db.get(CardSourceSnapshot,observed.snapshot_id) if observed else None
    if not snapshot or snapshot.source_id!=source.id:
        raise HTTPException(409,'보관 원문 없음')
    text=db.scalar(select(CardDocumentText).where(CardDocumentText.snapshot_id==snapshot.id,
        CardDocumentText.parser_version==KPASS_TEXT_PARSER))
    if not text:
        raise HTTPException(409,'상세 안내 재처리 필요')
    if text.raw_sha256!=snapshot.raw_sha256 or hashlib.sha256(text.text.encode()).hexdigest()!=text.text_sha256:
        raise HTTPException(409,'상세 안내 지문 불일치')
    from app.services.card_linked_documents import linked_document_status
    linked=linked_document_status(db,snapshot,text)
    return {'scope':'archived_source_text_not_public_benefit','source_id':source.id,'source_url':source.canonical_url,
        'snapshot_id':snapshot.id,'observed_at':aware(observed.attempted_at).isoformat(),
        'source_enabled':source.enabled,'source_error':source.last_error,
        'visible_text':snapshot.extracted_text,'document':{'text_id':text.id,'parser':text.parser_version,
        'raw_sha256':text.raw_sha256,'text_sha256':text.text_sha256,'text':text.text},
        'linked_documents':linked,
        'issues':['benefit_rules_not_extracted','not_publicly_approved',
            'linked_document_crosscheck_pending' if linked['status']=='ready_for_crosscheck' else 'linked_documents_not_collected']}


@router.get('/card-operations')
def operations(limit: int = Query(default=100,ge=1,le=200), offset: int = Query(default=0,ge=0),
               db: Session = Depends(get_db)):
    from app.services.card_operations import card_operations
    return card_operations(db,limit=limit,offset=offset)


@router.get('/card-programs')
def program_queue(limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0),
                  db: Session = Depends(get_db)):
    from app.models.card_benefit_version import CardBenefitVersion
    from app.services.card_benefit_extraction import review_pack
    from app.services.card_benefit_parsers import PARSERS
    from sqlalchemy import func
    from app.services.card_publication import program_review_status
    condition = CardBenefitVersion.extractor_version.in_([value[0] for value in PARSERS.values()])
    total = db.scalar(select(func.count()).select_from(CardBenefitVersion).where(condition))
    versions = db.scalars(select(CardBenefitVersion).where(condition).order_by(
        CardBenefitVersion.id.desc()).offset(offset).limit(limit)).all()
    pack = review_pack(db, [version.id for version in versions])
    for row, version in zip(pack['versions'], versions):
        row['readiness'] = program_review_status(db, version)
    return {**pack, 'total': total, 'next_offset': offset + limit if offset + limit < total else None}


@router.get('/card-programs/{version_id}')
def program_detail(version_id: int, db: Session = Depends(get_db)):
    from app.models.card_benefit_version import CardBenefitVersion
    from app.models.card_collection import CardSourceSnapshot
    from app.services.card_benefit_extraction import review_pack
    from app.services.card_publication import program_review_status
    version = db.get(CardBenefitVersion, version_id)
    if not version:
        raise HTTPException(404, '혜택 버전 없음')
    result = review_pack(db, [version.id])['versions'][0]
    result['readiness'] = program_review_status(db, version)
    snapshot = db.get(CardSourceSnapshot, version.snapshot_id)
    result['source_text'] = snapshot.extracted_text
    from app.services.card_program_changes import program_changes
    result['changes'] = program_changes(db, version)
    from app.services.card_policy_review import policy_review_packet
    result['policy_review'] = policy_review_packet(db,version,result['readiness']['supporting_document'])
    return result


@router.post('/card-programs/{version_id}/review')
def program_review(version_id: int, body: ProgramReview, db: Session = Depends(get_db)):
    from app.services.card_publication import review_program
    return review_program(db, version_id, body)


class ProgramPreview(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    scenario: DayScenario


@router.post('/card-programs/{version_id}/preview')
def program_preview(version_id: int, body: ProgramPreview, db: Session = Depends(get_db)):
    from pydantic import ValidationError
    from app.models.card_benefit_version import CardBenefitVersion
    from app.services.card_program_calculation import calculate_day
    from app.services.card_publication import program_review_status
    version = db.get(CardBenefitVersion, version_id)
    if not version:
        raise HTTPException(404, '혜택 버전 없음')
    if version.payload_hash != body.expected_payload_hash:
        raise HTTPException(409, '검토한 계산 조건이 변경되었습니다')
    if version.program_key != 'hana_day_offline':
        raise HTTPException(422, 'Day 계산 진단만 지원합니다')
    try:
        result = calculate_day(version.payload, body.scenario)
    except ValidationError:
        raise HTTPException(422, '계산 정책 구조를 지원하지 않습니다') from None
    return {'scope': 'admin_scenario_diagnostic_not_public_estimate', 'version_id': version.id,
        'payload_hash': version.payload_hash, 'scenario_only': True, 'public_estimated_saving': None,
        'scenario_result': result, 'publication_readiness': program_review_status(db, version)}


class UsageCapPreview(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    scenario: DeepOilScenario


class TimePreview(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    scenario: MrLifeScenario


@router.post('/card-programs/{version_id}/preview-time')
def time_preview(version_id: int, body: TimePreview, db: Session = Depends(get_db)):
    from app.models.card_benefit_version import CardBenefitVersion
    from app.services.card_time_calculation import calculate_mr_life
    from app.services.card_publication import payload_hash, program_review_status
    version = db.get(CardBenefitVersion, version_id)
    if not version:
        raise HTTPException(404, '혜택 버전 없음')
    if version.payload_hash != body.expected_payload_hash or payload_hash(version.payload) != version.payload_hash:
        raise HTTPException(409, '검토한 계산 조건이 변경되었습니다')
    if version.program_key != 'shinhan_mr_life_time':
        raise HTTPException(422, 'Mr.Life TIME 계산 진단만 지원합니다')
    try:
        result = calculate_mr_life(version.payload, body.scenario)
    except ValueError:
        raise HTTPException(422, '계산 정책 구조를 지원하지 않습니다') from None
    return {'scope': 'admin_scenario_diagnostic_not_public_estimate', 'version_id': version.id,
        'payload_hash': version.payload_hash, 'scenario_only': True, 'public_estimated_saving': None,
        'scenario_result': result, 'publication_readiness': program_review_status(db, version)}


@router.post('/card-programs/{version_id}/preview-usage-cap')
def usage_cap_preview(version_id: int, body: UsageCapPreview, db: Session = Depends(get_db)):
    from pydantic import ValidationError
    from app.models.card_benefit_version import CardBenefitVersion
    from app.services.card_usage_cap_calculation import calculate_deep_oil
    from app.services.card_publication import program_review_status
    version = db.get(CardBenefitVersion, version_id)
    if not version:
        raise HTTPException(404, '혜택 버전 없음')
    if version.payload_hash != body.expected_payload_hash:
        raise HTTPException(409, '검토한 계산 조건이 변경되었습니다')
    if version.program_key != 'shinhan_deep_oil_living':
        raise HTTPException(422, 'Deep Oil 이용금액 한도 진단만 지원합니다')
    try:
        result = calculate_deep_oil(version.payload, body.scenario)
    except ValidationError:
        raise HTTPException(422, '계산 정책 구조를 지원하지 않습니다') from None
    return {'scope': 'admin_scenario_diagnostic_not_public_estimate', 'version_id': version.id,
            'payload_hash': version.payload_hash, 'scenario_only': True, 'public_estimated_saving': None,
            'scenario_result': result, 'publication_readiness': program_review_status(db, version)}


@router.get('/queue')
def queue(db: Session = Depends(get_db)):
    rows = []
    for kind, model in MODELS.items():
        for item in db.scalars(select(model)).all():
            evidence = db.get(MapEvidence, (kind, item.id))
            status = evidence_status(evidence, item)
            if status != 'approved':
                rows.append({'kind': kind, 'id': item.id, 'status': status,
                             'label': getattr(item, 'benefit_text', getattr(item, 'condition_text', '')),
                             'source_url': evidence.source_url if evidence else None})
    return {'items': rows, 'total': len(rows)}


@router.get('/evidence/{kind}/{item_id}')
def evidence_detail(kind: str, item_id: int, db: Session = Depends(get_db)):
    row = db.get(MapEvidence, (kind, item_id))
    if not row: raise HTTPException(404, '수집 근거 없음')
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@router.post('/review/{kind}/{item_id}')
def review(kind: str, item_id: int, body: Review, db: Session = Depends(get_db)):
    return apply_benefit_review(kind, item_id, body, db)


@router.get('/health')
def health(db: Session = Depends(get_db)):
    from app.scripts.map_maintenance import audit
    return audit(db)


@router.get('/pipeline/runs')
def pipeline_runs(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    return [row.payload for row in db.scalars(select(MapReviewEvent).where(
        MapReviewEvent.kind == 'pipeline', MapReviewEvent.action == 'pipeline_finished')
        .order_by(MapReviewEvent.id.desc()).limit(limit)).all()]


@router.post('/pipeline/run')
def run_pipeline(db: Session = Depends(get_db)):
    from app.services.map_sync import sync_sources
    return sync_sources(db, trigger='admin')


@router.get('/history/{kind}/{item_id}')
def history(kind: str, item_id: int, db: Session = Depends(get_db)):
    return [{'action': row.action, 'at': row.at, 'payload': row.payload}
            for row in db.scalars(select(MapReviewEvent).where(MapReviewEvent.kind==kind, MapReviewEvent.item_id==item_id).order_by(MapReviewEvent.id)).all()]


class LocationReview(BaseModel):
    expected_identity: str = Field(min_length=64, max_length=64)
    place_id: str = Field(min_length=1, max_length=100)
    matched_name: str = Field(min_length=1)
    matched_address: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    source_url: str = Field(pattern=r'^https://(?:place\.map\.kakao\.com|map\.kakao\.com)/')
    reviewer: str = Field(min_length=1, max_length=100)


@router.post('/locations/{kind}/{item_id}')
def approve_location(kind: str, item_id: int, body: LocationReview, db: Session = Depends(get_db)):
    from app.models.store import Store
    model = {'local': MODELS['local'], 'store': Store}.get(kind)
    item = db.get(model, item_id) if model else None
    if not item: raise HTTPException(404, '매장 없음')
    name = getattr(item, 'merchant_name', getattr(item, 'brand', ''))
    if body.expected_identity != location_hash(item): raise HTTPException(409, '주소 변경됨')
    from app.services.map_verification import normalize_location_text as normalize, location_address_key as address_key
    expected_name = f'{name} {item.branch}' if kind == 'store' else name
    if normalize(expected_name) != normalize(body.matched_name) or address_key(item.address) != address_key(body.matched_address):
        raise HTTPException(422, '상호·주소가 일치하지 않습니다')
    if body.source_url.rstrip('/').rsplit('/', 1)[-1] != body.place_id:
        raise HTTPException(422, '장소 ID와 원문 링크가 일치하지 않습니다')
    row = db.get(MapLocation, (kind, item_id)) or MapLocation(kind=kind, item_id=item_id)
    for field in ('place_id', 'lat', 'lng', 'source_url', 'reviewer'): setattr(row, field, getattr(body, field))
    row.identity_hash, row.checked_at = location_hash(item), datetime.now(timezone.utc)
    db.add(row)
    db.add(MapReviewEvent(kind=kind, item_id=item_id, action='location_review', at=datetime.now(timezone.utc), payload=body.model_dump(mode='json')))
    db.commit()
    return {'status': 'verified'}


@router.get('/locations')
def location_queue(db: Session = Depends(get_db)):
    from app.models.store import Store
    return [{'kind': kind, 'id': item.id, 'name': getattr(item, 'merchant_name', getattr(item, 'brand', '')),
             'address': item.address, 'identity_hash': location_hash(item)}
            for kind, model in [('local', MODELS['local']), ('store', Store)]
            for item in db.scalars(select(model)).all() if not verified_location(db, kind, item)]


@router.get('/locations/{kind}/{item_id}/candidates')
def location_candidates(kind: str, item_id: int, db: Session = Depends(get_db)):
    import httpx
    from app.core.config import settings
    from app.models.store import Store
    if not settings.kakao_rest_api_key:
        raise HTTPException(503, '서버 KAKAO_REST_API_KEY 설정이 필요합니다')
    model = {'local': MODELS['local'], 'store': Store}.get(kind)
    item = db.get(model, item_id) if model else None
    if not item: raise HTTPException(404, '매장 없음')
    name = getattr(item, 'merchant_name', getattr(item, 'brand', ''))
    query = ' '.join(filter(None, [name, getattr(item, 'branch', ''), item.address]))
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get('https://dapi.kakao.com/v2/local/search/keyword.json',
                params={'query': query, 'size': 5}, headers={'Authorization': f'KakaoAK {settings.kakao_rest_api_key}'})
            response.raise_for_status()
            documents = response.json()['documents']
    except (httpx.HTTPError, KeyError, ValueError):
        raise HTTPException(502, '장소 제공처 조회 실패') from None
    return {'identity_hash': location_hash(item), 'expected_name': name, 'expected_address': item.address,
        'candidates': [{'place_id': d['id'], 'matched_name': d['place_name'],
            'matched_address': d.get('road_address_name') or d['address_name'],
            'lat': float(d['y']), 'lng': float(d['x']), 'source_url': d['place_url']}
            for d in documents]}


payment_router = APIRouter(prefix='/stores', tags=['stores'])


class EstimateRequest(BaseModel):
    kind: Literal['store', 'local']
    item_id: int
    context: PaymentContext


@payment_router.post('/estimate')
def payment_estimate(body: EstimateRequest, user=Depends(current_user), db: Session = Depends(get_db)):
    from app.models.store import Store
    from app.api.stores import _active_offers, _load_card_benefits, _offer_report_counts
    from app.services.card_normalize import store_brand_key
    from app.core.config import settings
    targets = []
    branch = ''
    if body.kind == 'store':
        store = db.get(Store, body.item_id)
        if not store or not store.is_active: raise HTTPException(404, '매장 없음')
        branch = store.branch
        counts = _offer_report_counts(db)
        targets += [('offer', o) for o in _active_offers(store, None)
                    if not (settings.store_offer_report_threshold > 0 and counts.get(o.id, 0) >= settings.store_offer_report_threshold)]
        if user.card_ids:
            targets += [('card', b) for b in _load_card_benefits(db, False, user.card_ids).get(store_brand_key(store.brand), [])]
        location = verified_location(db, 'store', store)
        if location:
            local_ids = db.scalars(select(MapLocation.item_id).where(MapLocation.kind=='local', MapLocation.place_id==location.place_id)).all()
            for local in db.scalars(select(MODELS['local']).where(MODELS['local'].id.in_(local_ids))).all():
                if local.is_active and local.discount_type not in ('currency', 'menu_price') and (not local.valid_to or local.valid_to >= today_kst()) and verified_location(db, 'local', local):
                    targets.append(('local', local))
    else:
        item = db.get(MODELS['local'], body.item_id)
        if not item or not item.is_active or (item.valid_to and item.valid_to < today_kst()): raise HTTPException(404, '매장 없음')
        branch = item.merchant_name
        targets = [('local', item)] if item.discount_type not in ('currency', 'menu_price') else []
    candidates = []
    for kind, item in targets:
        evidence = db.get(MapEvidence, (kind, item.id))
        state = evidence_status(evidence, item)
        if state != 'approved':
            candidates.append({'key': f'{kind}:{item.id}', 'label': getattr(item, 'benefit_text', getattr(item, 'condition_text', None)) or '원문 확인 필요 혜택',
                               'status': state, 'estimated_saving': None, 'reasons': ['원문 검증 필요']})
            continue
        result = estimate(BenefitRules.model_validate(evidence.rules), body.context, item.merchant_name if kind=='local' else branch, f'{kind}:{item.id}', getattr(item, 'card_id', None))
        label = item.card.card_name if kind == 'card' else item.pay_method.value if kind == 'offer' else item.merchant_name
        candidates.append({'key': f'{kind}:{item.id}', 'label': label, **result})
    if body.kind == 'store' and user.card_ids:
        from app.services.card_publication import store_programs, estimate_program
        candidates += [estimate_program(offer, body.context) for offer in store_programs(db, store, card_ids=user.card_ids)]
    eligible = [r for r in candidates if r['status'] == 'eligible' and r['estimated_saving'] > 0]
    return {'candidates': candidates, 'best': max(eligible, key=lambda r: r['estimated_saving'], default=None),
            'basis': '사용자 입력 기준 예상값; 실제 결제 내역·실적·자격 인증과 다를 수 있습니다'}
