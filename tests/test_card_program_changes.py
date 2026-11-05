from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from sqlalchemy import select,func

from app.models.card_benefit_version import CardBenefitVersion,CardBenefitFieldEvidence
from app.models.card_collection import CardSourceSnapshot,CardCollectionAttempt
from app.models.map_verification import MapReviewEvent
from app.services.card_program_changes import payload_changes,program_changes
from tests.test_card_publication import draft,publish,NOW


def next_version(db, original, snapshot, *, new_source=False):
    payload=deepcopy(original.payload)
    payload['rules'][0]['rate_bps']=999
    if new_source:
        text=snapshot.extracted_text+' changed notice'
        digest=hashlib.sha256(text.encode()).hexdigest()
        snapshot=CardSourceSnapshot(source_id=original.source_id,fetched_at=NOW+timedelta(minutes=1),
            raw_sha256=digest,semantic_sha256=digest,raw_object_ref=digest,extracted_text=text,
            mime_type='text/html',parser_version='fixture',completeness='text_available')
        db.add(snapshot)
        db.flush()
    version=CardBenefitVersion(program_key=original.program_key,source_id=original.source_id,
        snapshot_id=snapshot.id,extractor_version='fixture_new',payload=payload,
        payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
        created_at=NOW+timedelta(minutes=1))
    db.add(version)
    db.flush()
    db.add(CardCollectionAttempt(source_id=version.source_id,snapshot_id=snapshot.id,
        status='captured',http_status=200,attempted_at=NOW+timedelta(minutes=1)))
    db.add(CardBenefitFieldEvidence(version_id=version.id,field_path='changed',quote='changed notice',
        start_offset=0,end_offset=14))
    db.commit()
    return version


def test_added_removed_null_types_and_truncation():
    changes=payload_changes({'null':None,'removed':1,'flag':True},{'added':None,'flag':1})
    assert {(r['path'],r['kind']) for r in changes['fields']}=={
        ('/null','removed'),('/removed','removed'),('/added','added'),('/flag','changed')}
    assert payload_changes({}, {'a':1,'b':2},limit=1)['truncated'] is True


def test_rule_identity_and_order_are_compared_separately():
    before={'rules':[{'rule_key':'coffee','rate_bps':500},{'rule_key':'shop','rate_bps':100}]}
    after={'rules':[{'rule_key':'shop','rate_bps':100},{'rule_key':'coffee','rate_bps':700}]}
    assert [row['path'] for row in payload_changes(before,after)['fields']]==[
        '/rules/$order','/rules/coffee/rate_bps']
    malformed={'rules':[{'rule_key':'a'},{'rule_key':'a'}]}
    assert payload_changes(before,malformed)['fields'][0]['path']=='/rules'


def test_first_version_has_no_baseline_and_detail_is_admin_only(client,db,draft):
    version=draft[0]
    assert program_changes(db,version)['status']=='no_baseline'
    path=f'/admin/map/card-programs/{version.id}'
    assert client.get(path).status_code==401
    data=client.get(path,headers={'X-Admin-Token':'test-admin-token'}).json()['changes']
    assert data['baseline'] is None and data['review_required']


def test_prior_extraction_and_parser_only_change_are_distinct(db,draft):
    original,_,snapshot,_=draft
    version=next_version(db,original,snapshot)
    result=program_changes(db,version)
    assert result['baseline']['kind']=='previous_extraction'
    assert result['baseline']['version_id']==original.id
    assert not result['source']['raw_changed'] and not result['source']['text_changed']
    assert result['extractor_changed'] and result['payload']['fields']
    assert result['evidence_quotes']['fields']


def test_publication_baseline_and_source_reversion_use_observed_attempt_not_snapshot_id(client,db,draft):
    publication,_=publish(db,draft)
    original,_,snapshot,_=draft
    target=next_version(db,original,snapshot,new_source=True)
    report=program_changes(db,target)
    assert report['baseline']['kind']=='publication_pointer'
    assert report['baseline']['version_id']==original.id
    assert report['target_matches_latest_observation'] and report['source']['raw_changed']
    old_time=publication.recheck_at
    db.add(CardCollectionAttempt(source_id=original.source_id,snapshot_id=snapshot.id,status='captured',
        http_status=200,attempted_at=NOW+timedelta(minutes=2)))
    db.commit()
    assert program_changes(db,target)['target_matches_latest_observation'] is False
    old=program_changes(db,original)
    assert old['status']=='same_version' and old['target_matches_latest_observation']
    assert not old['payload']['fields'] and old['review_required']
    response=client.get(f'/admin/map/card-programs/{target.id}',headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code==200 and response.json()['changes']['baseline']['version_id']==original.id
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==1
    db.refresh(publication)
    assert publication.recheck_at==old_time and publication.version_id==original.id
