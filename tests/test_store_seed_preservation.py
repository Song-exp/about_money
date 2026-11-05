from datetime import datetime,timezone
import pytest
from sqlalchemy import select
from app.models.store import Store,StoreOffer,StoreCategory,PayMethod
from app.models.map_verification import MapLocation,MapReviewEvent
from app.services.map_verification import location_hash
from app.scripts import seed_stores as seeder


def seed_file(tmp_path):
    path=tmp_path/'seed.js'
    path.write_text("""const stores = [
{brand: 'CU', branch: '경희대점', address: '경희대로4길 15', lat: 1, lng: 2, mark: 'CU', color: '#fff'}
];
const standardOffers = {
kakao: [5, '예시 조건']
};""",encoding='utf-8')
    return path


def make_store(db):
    row=Store(brand='CU',branch='경희대점',category=StoreCategory.CONVENIENCE,
        address='서울 동대문구 경희대로4길 15',lat=37.59,lng=127.05,is_active=True)
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize('proof',['location','address_review'])
def test_legacy_seed_preserves_reviewed_identity(db,tmp_path,monkeypatch,proof):
    row=make_store(db)
    now=datetime.now(timezone.utc)
    if proof=='location':
        db.add(MapLocation(kind='store',item_id=row.id,identity_hash=location_hash(row),place_id='123',
            lat=row.lat,lng=row.lng,source_url='https://place.map.kakao.com/123',checked_at=now,reviewer='reviewer'))
    else:
        db.add(MapReviewEvent(kind='store',item_id=row.id,action='address_review',at=now,payload={}))
    db.commit()
    monkeypatch.setattr(seeder,'init_db',lambda:None)
    seeder.seed(str(seed_file(tmp_path)))
    db.refresh(row)
    assert row.address=='서울 동대문구 경희대로4길 15'
    assert (row.lat,row.lng)==(37.59,127.05)


def test_unreviewed_legacy_candidate_can_still_be_seeded(db,tmp_path,monkeypatch):
    row=make_store(db)
    db.commit()
    monkeypatch.setattr(seeder,'init_db',lambda:None)
    seeder.seed(str(seed_file(tmp_path)))
    db.refresh(row)
    assert row.address=='경희대로4길 15' and (row.lat,row.lng)==(1,2)


@pytest.mark.parametrize('rate',[5,7])
def test_real_offer_never_becomes_sample_even_if_rate_matches_seed(db,tmp_path,monkeypatch,rate):
    row=make_store(db)
    offer=StoreOffer(store=row,pay_method=PayMethod.KAKAO,discount_rate=rate,
        condition_text='출처로 확인한 실제 조건',is_sample=False,is_active=True)
    db.add(offer)
    db.commit()
    monkeypatch.setattr(seeder,'init_db',lambda:None)
    seeder.seed(str(seed_file(tmp_path)))
    db.refresh(offer)
    assert offer.discount_rate==rate and offer.condition_text=='출처로 확인한 실제 조건'
    assert offer.is_sample is False


def test_sample_offer_can_be_updated_without_creating_duplicate(db,tmp_path,monkeypatch):
    row=make_store(db)
    offer=StoreOffer(store=row,pay_method=PayMethod.KAKAO,discount_rate=3,
        condition_text='과거 예시',is_sample=True,is_active=True)
    db.add(offer)
    db.commit()
    monkeypatch.setattr(seeder,'init_db',lambda:None)
    seeder.seed(str(seed_file(tmp_path)))
    db.refresh(offer)
    assert offer.discount_rate==5 and offer.condition_text=='예시 조건' and offer.is_sample
    assert len(list(db.scalars(select(StoreOffer))))==1
