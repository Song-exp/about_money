from datetime import timedelta

import pytest
from sqlalchemy import select,func

from app.models.card_collection import CardCollectionRun,CardCollectionAttempt
from app.models.card_benefit_version import CardBenefitExtraction
from app.models.map_verification import MapReviewEvent
from app.services.card_operations import card_operations
from tests.test_card_publication import draft,publish,NOW
from tests.test_card_program_changes import next_version


def program(report):
    return next(row for row in report['alerts'] if row['kind']=='program')


def test_empty_monitor_and_authentication(client,db):
    assert card_operations(db,now=NOW)['total']==0
    assert client.get('/admin/map/card-operations').status_code==401
    headers={'X-Admin-Token':'test-admin-token'}
    response=client.get('/admin/map/card-operations',headers=headers)
    assert response.status_code==200 and response.json()['notification_channel']=='admin_dashboard_only'
    assert client.get('/admin/map/card-operations?limit=201',headers=headers).status_code==422
    assert client.get('/admin/map/card-operations?offset=-1',headers=headers).status_code==422


def test_new_candidate_is_pending_review_and_healthy_publication_has_no_program_alert(db,draft):
    assert program(card_operations(db,now=NOW))['reasons']==['pending_review']
    publish(db,draft)
    assert not [row for row in card_operations(db,now=NOW)['alerts'] if row['kind']=='program']


@pytest.mark.parametrize('hours,priority,reason',[(23,1,'review_due_soon'),(25,0,'published_program_unavailable')])
def test_expiring_and_stopped_programs_have_priority(db,draft,hours,priority,reason):
    publication,_=publish(db,draft)
    old_deadline=publication.recheck_at
    report=card_operations(db,now=NOW+timedelta(hours=hours))
    alert=program(report)
    assert alert['priority']==priority and reason in alert['reasons']
    db.refresh(publication)
    assert publication.recheck_at==old_deadline and publication.status=='approved'


def test_disabled_source_not_reported_as_collection_failure_but_publication_still_reported(db,draft):
    publish(db,draft)
    source=draft[1]
    source.enabled=False
    source.last_error='ConnectError'
    source.failure_count=10
    db.commit()
    report=card_operations(db,now=NOW)
    assert report['sources']=={'enabled':0,'disabled':1}
    assert all(row['kind']!='source' for row in report['alerts'])
    assert program(report)['priority']==0


@pytest.mark.parametrize('error,count,priority,reason',[
    ('ConnectError',1,2,'collection_failed'),('ConnectError',3,1,'repeated_collection_failure'),
    ('source_disallows_fetch',10,2,'source_blocked')])
def test_failure_severity_and_future_backoff(error,count,priority,reason,db,draft):
    source=draft[1]
    source.last_error=error
    source.failure_count=count
    source.last_attempt_at=NOW
    source.next_check_at=NOW-timedelta(hours=1)
    source.next_retry_at=NOW+timedelta(hours=1)
    db.commit()
    alert=next(row for row in card_operations(db,now=NOW)['alerts'] if row['kind']=='source')
    assert alert['priority']==priority and reason in alert['reasons']
    assert 'collection_overdue' not in alert['reasons']


def test_overdue_source_recovers_without_acknowledgement_write(db,draft):
    source=draft[1]
    source.last_attempt_at=NOW-timedelta(hours=2)
    source.next_check_at=NOW-timedelta(minutes=16)
    db.commit()
    assert any('collection_overdue' in row['reasons'] for row in card_operations(db,now=NOW)['alerts'])
    source.next_check_at=NOW+timedelta(days=1)
    db.commit()
    assert not [row for row in card_operations(db,now=NOW)['alerts'] if row['kind']=='source']


def test_latest_candidate_has_publication_context_and_observation_reversion(db,draft):
    publication,_=publish(db,draft)
    target=next_version(db,draft[0],draft[2],new_source=True)
    target.extractor_version=draft[0].extractor_version
    db.commit()
    report=card_operations(db,now=NOW+timedelta(minutes=2))
    alert=program(report)
    assert alert['version_id']==target.id and alert['published_version_id']==publication.version_id
    assert alert['priority']==0 and 'candidate_differs_from_publication' in alert['reasons']
    assert any('extraction_missing' in r['reasons'] for r in report['alerts'])
    db.add(CardCollectionAttempt(source_id=draft[1].id,snapshot_id=draft[2].id,
        status='captured',http_status=200,attempted_at=NOW+timedelta(minutes=3)))
    db.commit()
    assert not [r for r in card_operations(db,now=NOW+timedelta(minutes=3))['alerts'] if r['kind']=='program']


def test_failed_extraction_is_visible(db,draft):
    record=db.scalar(select(CardBenefitExtraction))
    record.status='failed'
    db.commit()
    assert any('extraction_failed' in row['reasons'] for row in card_operations(db,now=NOW)['alerts'])


def test_document_conflict_is_prioritized_without_publication(db):
    from tests.test_deep_oil_extraction import draft_deep_oil, NOW as document_now
    from tests.test_shinhan_disclosure import attach_deep_oil_leaflet
    draft_deep_oil(db)
    attach_deep_oil_leaflet(db)
    alert=program(card_operations(db,now=document_now))
    assert alert['priority']==1 and 'supporting_document_conflict' in alert['reasons']
    assert alert['approval_blocker']=='supporting_document_conflict'


def test_latest_successful_run_replaces_old_failure_alert(db):
    db.add(CardCollectionRun(id='old',trigger='test',started_at=NOW-timedelta(hours=1),status='failed'))
    db.add(CardCollectionRun(id='new',trigger='test',started_at=NOW,status='complete'))
    db.commit()
    report=card_operations(db,now=NOW)
    assert report['total']==0 and report['latest_run']['id']=='new'


@pytest.mark.parametrize('status,minutes,reason',[('failed',5,'collection_run_failed'),
    ('degraded',5,'collection_run_degraded'),('running',76,'collection_run_stalled')])
def test_run_failures(status,minutes,reason,db):
    db.add(CardCollectionRun(id='run',trigger='test',started_at=NOW-timedelta(minutes=minutes),status=status))
    db.commit()
    assert reason in card_operations(db,now=NOW)['alerts'][0]['reasons']


def test_pagination_counts_and_readonly_audit(client,db,draft):
    publication,_=publish(db,draft)
    draft[1].last_error='ConnectError'
    draft[1].failure_count=3
    db.commit()
    report=card_operations(db,now=NOW,limit=1)
    assert report['total']==2 and report['next_offset']==1
    assert report['counts']=={'p0':1,'p1':1,'p2':0}
    assert card_operations(db,now=NOW,limit=1,offset=1)['alerts'][0]['kind']=='source'
    response=client.get('/admin/map/card-operations?limit=1',headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code==200
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==1
    assert publication.version_id==draft[0].id
