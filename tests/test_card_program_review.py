"""Admin readiness is read-only; renewal uses publication guards and audit."""
from datetime import datetime, timedelta

from sqlalchemy import select, func

from app.models.card import Card
from app.models.card_collection import CardCollectionAttempt
from app.models.card_publication import CardProgramPublication
from app.models.map_verification import MapReviewEvent
from app.services.card_publication import program_review_status, review_program, publication_status
from tests.test_card_publication import draft, publish, review_body, NOW

HEADERS = {'X-Admin-Token': 'test-admin-token'}


def test_review_replaces_expired_pointer_without_duplicating_card_or_auto_renewal(db,draft):
    import hashlib
    from app.models.card_collection import CardSourceSnapshot
    from app.models.card_benefit_version import CardBenefitVersion
    from app.services.card_benefit_extraction import extract_snapshot
    from app.services.card_operations import card_operations
    publication,_=publish(db,draft)
    original,source,snapshot,_=draft
    card_id=publication.card_id
    later=NOW+timedelta(hours=25)
    text=snapshot.extracted_text+' 공지 화면 갱신'
    digest=hashlib.sha256(text.encode()).hexdigest()
    updated=CardSourceSnapshot(source_id=source.id,fetched_at=later,raw_sha256=digest,
        semantic_sha256=digest,raw_object_ref=digest,extracted_text=text,mime_type='text/html',
        parser_version='fixture',completeness='text_available')
    db.add(updated)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=updated.id,status='captured',
        http_status=200,attempted_at=later))
    result=extract_snapshot(db,source,updated,now=later)
    replacement=db.get(CardBenefitVersion,result['version_id'])
    db.commit()
    assert replacement.payload==original.payload
    assert publication_status(db,publication,later)=='recheck_required'
    alerts=card_operations(db,now=later)['alerts']
    assert any(row['kind']=='program' and row['priority']==0 and row['version_id']==replacement.id for row in alerts)
    review_program(db,replacement.id,review_body(replacement,updated,recheck_hours=12),now=later)
    assert publication.version_id==replacement.id and publication.card_id==card_id
    assert original.publication_state=='superseded'
    assert publication_status(db,publication,later)=='approved'
    assert db.scalar(select(func.count()).select_from(Card))==1
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==2
    assert not any(row['kind']=='program' and row['priority']==0 for row in card_operations(db,now=later)['alerts'])


def test_review_readiness_and_source_detail_require_admin(client, db, draft):
    version, _, snapshot, _ = draft
    url = f'/admin/map/card-programs/{version.id}'
    assert client.get(url).status_code == 401
    assert client.get('/admin/map/card-programs', headers={'X-Admin-Token': 'wrong'}).status_code == 401
    result = client.get(url, headers=HEADERS).json()
    assert result['readiness']['can_approve'] is True
    assert result['readiness']['human_review_required'] is True
    assert result['readiness']['public_status'] == 'not_published'
    assert result['source_text'] == snapshot.extracted_text
    for evidence in result['evidence']:
        assert result['source_text'][evidence['start_offset']:evidence['end_offset']] == evidence['quote']
    for _ in range(2):
        assert client.get('/admin/map/card-programs', headers=HEADERS).json()['total'] == 1
    for model in (Card, CardProgramPublication, MapReviewEvent):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert version.review_state == 'pending'


def test_readiness_explains_stale_source_and_bad_product(client, db, draft):
    version, _, _, _ = draft
    attempt = db.scalar(select(CardCollectionAttempt))
    attempt.attempted_at = NOW - timedelta(hours=25)
    db.commit()
    readiness = program_review_status(db, version, now=NOW)
    assert readiness['can_approve'] is False and readiness['blocker'] == 'source_stale'
    attempt.attempted_at = NOW
    from app.models.card_collection import CardProductSource
    db.scalar(select(CardProductSource)).product_name = '다른 카드 상품'
    db.commit()
    readiness = program_review_status(db, version, now=NOW)
    assert readiness['can_approve'] is False and '상품 식별' in readiness['blocker']
    assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0


def test_queue_pagination_and_missing_detail(client, db, draft):
    data = client.get('/admin/map/card-programs?limit=1', headers=HEADERS).json()
    assert data['total'] == 1 and data['next_offset'] is None and len(data['versions']) == 1
    assert client.get('/admin/map/card-programs?offset=1', headers=HEADERS).json()['versions'] == []
    assert client.get('/admin/map/card-programs?limit=201', headers=HEADERS).status_code == 422
    assert client.get('/admin/map/card-programs/999999', headers=HEADERS).status_code == 404


def test_same_version_renewal_requires_full_revalidation_and_records_audit(db, draft):
    publication, _ = publish(db, draft)
    version, _, snapshot, _ = draft
    old_recheck = publication.recheck_at
    later = NOW + timedelta(hours=2)
    assert program_review_status(db, version, now=later)['is_current_publication'] is True
    assert publication.recheck_at == old_recheck
    review_program(db, version.id, review_body(version, snapshot), now=later)
    from app.services.map_verification import aware
    assert aware(publication.recheck_at) == later + timedelta(hours=24)
    assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 2
    assert db.scalar(select(func.count()).select_from(Card)) == 1
    assert publication_status(db, publication, NOW + timedelta(days=2)) == 'recheck_required'


def test_reviewed_period_controls_end_of_season_collection_cadence(db, draft):
    version, source, snapshot, _ = draft
    end = datetime.fromisoformat(version.payload['valid_until_exclusive'])
    now = end - timedelta(days=2)
    db.scalar(select(CardCollectionAttempt)).attempted_at = now
    source.next_check_at = now + timedelta(hours=24)
    original_policy = dict(source.schedule_policy)
    db.commit()
    assert program_review_status(db, version, now=now)['can_approve'] is True
    assert source.schedule_policy == original_policy
    review_program(db, version.id, review_body(version, snapshot), now=now)
    from app.services.map_verification import aware
    assert source.schedule_policy['reviewed_end_at'] == end.isoformat()
    assert aware(source.next_check_at) == now + timedelta(hours=6)


def test_reviewed_early_termination_policy_accelerates_collection(db, draft):
    version, source, snapshot, _ = draft
    source.next_check_at = NOW + timedelta(hours=24)
    assert 'early_end_possible' not in source.schedule_policy
    review_program(db, version.id, review_body(version, snapshot), now=NOW)
    from app.services.map_verification import aware
    assert source.schedule_policy['early_end_possible'] is True
    assert aware(source.next_check_at) == NOW + timedelta(hours=6)
