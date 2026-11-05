from datetime import timedelta

from sqlalchemy import select,func

from app.models.card_collection import CardCollectionAttempt
from app.models.map_verification import MapReviewEvent
from app.models.card_publication import CardProgramPublication
from app.services.card_policy_review import policy_review_packet
from app.services.card_document_crosscheck import document_checks
from tests.test_card_document_crosscheck import documents,NOW
from tests.test_card_publication import draft
from tests.test_deep_oil_extraction import draft_deep_oil,NOW as DEEP_NOW
from tests.test_shinhan_disclosure import attach_deep_oil_leaflet

HEADERS={'X-Admin-Token':'test-admin-token'}


def test_day_packet_binds_questions_to_actual_evidence_and_does_not_treat_omission_as_permission(db,documents):
    version,_,secondary,derived=documents
    check=document_checks(db,version,now=NOW)
    packet=policy_review_packet(db,version,check)
    assert packet['status']=='unresolved' and packet['comparison_evidence_verified']
    assert len(packet['conflicts'])==5
    assert packet['supporting_document']['snapshot_id']==secondary.id
    assert packet['supporting_document']['text_id']==derived.id
    assert packet['supporting_document']['text_sha256']==derived.text_sha256
    text=packet['inquiry_text']
    assert '미기재(할인 허용으로 확정하지 않음)' in text
    assert '1,000.5원' in text and '8개 영역' in text
    assert '자동납부에 대한 별도 예외' in text
    assert version.payload_hash in text and secondary.raw_sha256 in text


def test_admin_detail_packet_has_no_approval_or_audit_side_effects(client,db,documents):
    version=documents[0]
    url=f'/admin/map/card-programs/{version.id}'
    assert client.get(url).status_code==401
    for _ in range(2):
        response=client.get(url,headers=HEADERS)
        assert response.status_code==200
        data=response.json()
        assert data['policy_review']['status']=='unresolved'
        assert data['readiness']['can_approve'] is False
        assert data['readiness']['blocker']=='supporting_document_conflict'
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==0
    assert db.scalar(select(func.count()).select_from(CardProgramPublication))==0


def test_stale_document_does_not_reuse_verified_conflicts(db,documents):
    version,source,_,_=documents
    full=db.scalar(select(CardCollectionAttempt).where(CardCollectionAttempt.source_id==source.id))
    full.attempted_at=NOW-timedelta(days=8)
    db.commit()
    packet=policy_review_packet(db,version,document_checks(db,version,now=NOW))
    assert packet['comparison_status']=='source_stale' and not packet['comparison_evidence_verified']
    assert packet['conflicts']==[]


def test_deep_oil_questions_preserve_scope_and_unresolved_arithmetic(db):
    version=draft_deep_oil(db)
    attach_deep_oil_leaflet(db)
    packet=policy_review_packet(db,version,document_checks(db,version,now=DEEP_NOW))
    assert len(packet['conflicts'])==2
    assert packet['conflicts'][1]['impact']=='현재 매장 규칙 범위 밖의 영화 서비스'
    assert '70만원 미만 이상' in packet['inquiry_text']
    assert '500.5원' in packet['inquiry_text'] and '300원에 대한 15원 할인' in packet['inquiry_text']
    assert '택시의 오프라인 제한' in packet['inquiry_text']


def test_missing_document_stays_unresolved(db):
    version=draft_deep_oil(db)
    packet=policy_review_packet(db,version,document_checks(db,version,now=DEEP_NOW))
    assert packet['comparison_status']=='source_unavailable'
    assert not packet['comparison_evidence_verified'] and packet['supporting_document']['snapshot_id'] is None
    assert packet['conflicts']==[] and packet['questions']


def test_corrupt_primary_never_generates_verified_conflict_claims(db,documents):
    version,_,_,_=documents
    from app.models.card_collection import CardSourceSnapshot
    primary=db.get(CardSourceSnapshot,version.snapshot_id)
    check=document_checks(db,version,now=NOW)
    primary.extracted_text+='tampered'
    packet=policy_review_packet(db,version,check)
    assert not packet['comparison_evidence_verified'] and packet['conflicts']==[]


def test_supported_toss_flow_has_no_unrelated_inquiry(db,draft):
    assert policy_review_packet(db,draft[0],{'status':'not_configured','required':False}) is None
