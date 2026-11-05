from datetime import timedelta
import pytest
from sqlalchemy import select,func
from app.models.store import Store,StoreCategory,StoreOffer,PayMethod
from app.models.map_verification import MapLocation,MapReviewEvent,MapEvidence
from app.services.map_verification import location_hash,verified_location,item_hash,evidence_status
from app.services.store_address_completion import address_completion
from tests.test_card_collection import NOW


def setup_store(db):
    store=Store(brand='CU',branch='경희대점',category=StoreCategory.CONVENIENCE,
        address='경희대로4길 15',lat=37.5,lng=127.0,is_active=True)
    db.add(store)
    db.flush()
    at=NOW-timedelta(days=1)
    loc=MapLocation(kind='store',item_id=store.id,identity_hash=location_hash(store),place_id='123',
        lat=37.501,lng=127.001,source_url='https://place.map.kakao.com/123',
        reviewer='exact_name_address_match',checked_at=at)
    event=MapReviewEvent(kind='store',item_id=store.id,action='location_match',at=at,payload={
        'place_id':'123','name':'CU 경희대점','source_address':store.address,'region':'서울 동대문구',
        'qualified_address':'서울 동대문구 경희대로4길 15','address':'서울 동대문구 경희대로4길 15'})
    db.add_all([loc,event])
    db.flush()
    return store,loc,event


def test_preview_and_apply_keep_original_observation_and_are_idempotent(db):
    store,loc,event=setup_store(db)
    original_at=loc.checked_at
    coords=(store.lat,store.lng,loc.lat,loc.lng)
    assert address_completion(db,store,now=NOW)['status']=='ready'
    assert store.address=='경희대로4길 15'
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==1
    result=address_completion(db,store,now=NOW,apply=True)
    db.flush()
    assert result['status']=='address_completed' and result['source_event_id']==event.id
    assert store.address=='서울 동대문구 경희대로4길 15'
    assert loc.checked_at==original_at and loc.identity_hash==location_hash(store)
    assert verified_location(db,'store',store) is not None
    assert (store.lat,store.lng,loc.lat,loc.lng)==coords
    audit=db.scalar(select(MapReviewEvent).where(MapReviewEvent.action=='address_review'))
    assert not audit.payload['new_external_observation'] and audit.payload['source_event_id']==event.id
    assert address_completion(db,store,now=NOW,apply=True)['status']=='unchanged'
    db.flush()
    assert db.scalar(select(func.count()).select_from(MapReviewEvent))==2


@pytest.mark.parametrize('change', ['store_changed','location_hash','place_id','place_url','old','future','reviewer',
    'name','event_time','event_place','source_address','qualified','address','region','manual','inactive','campus','event_missing'])
def test_missing_stale_changed_or_ambiguous_proof_cannot_promote_address(db,change):
    store,loc,event=setup_store(db)
    if change=='store_changed': store.branch='다른지점'
    if change=='location_hash': loc.identity_hash='0'*64
    if change=='place_id': loc.place_id='different'
    if change=='place_url': loc.source_url='https://example.org/123'
    if change=='old': loc.checked_at=NOW-timedelta(days=8)
    if change=='future': loc.checked_at=NOW+timedelta(days=1)
    if change=='reviewer': loc.reviewer='manual'
    payload=dict(event.payload)
    if change=='name': payload['name']='CU 다른점'
    if change=='event_time': event.at=NOW
    if change=='event_place': payload['place_id']='456'
    if change=='source_address': payload['source_address']='다른길 3'
    if change=='qualified': payload['qualified_address']='서울 중랑구 경희대로4길 15'
    if change=='address': payload['address']='서울 동대문구 경희대로4길 15 2층'
    if change=='region': payload['region']=''
    if change=='manual': event.action='location_review'
    if change=='inactive': store.is_active=False
    if change=='campus': store.address='청운관'
    if change=='event_missing': db.delete(event)
    event.payload=payload
    db.flush()
    before=store.address
    result=address_completion(db,store,now=NOW,apply=True)
    assert result['status']=='pending' and store.address==before


def test_benefit_approval_is_not_rebound_to_new_store_address(db):
    store,_,_=setup_store(db)
    offer=StoreOffer(store=store,pay_method=PayMethod.KAKAO,discount_rate=5,is_active=True,is_sample=False)
    db.add(offer)
    db.flush()
    evidence=MapEvidence(kind='offer',item_id=offer.id,status='approved',source_url='https://example.org/event',
        provider='test',source_text='source',content_hash='0'*64,item_hash=item_hash(offer),
        fetched_at=NOW,verified_at=NOW,recheck_at=NOW+timedelta(days=1),indefinite=True,rules={})
    db.add(evidence)
    db.flush()
    previous_hash=evidence.item_hash
    assert evidence_status(evidence,offer,now=NOW)=='approved'
    address_completion(db,store,now=NOW,apply=True)
    assert evidence.item_hash==previous_hash
    assert evidence_status(evidence,offer,now=NOW)=='changed'


def test_reseed_does_not_restore_short_address(db,monkeypatch):
    from app.scripts import seed_category_stores as seeder
    store,_,_=setup_store(db)
    address_completion(db,store,now=NOW,apply=True)
    db.commit()
    monkeypatch.setattr(seeder,'init_db',lambda:None)
    monkeypatch.setattr(seeder,'_STORES',[dict(brand='CU',branch='경희대점',category=store.category,
        address='경희대로4길 15',lat=1,lng=2,mark='CU',color='#abc')])
    seeder.seed()
    db.refresh(store)
    assert store.address=='서울 동대문구 경희대로4길 15'
    assert (store.lat,store.lng)==(37.5,127.0)
