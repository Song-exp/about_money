import hashlib
import json
from datetime import timedelta
from urllib.parse import urlencode
import pytest
from sqlalchemy import select, func
from app.models.issuer_merchant_capture import IssuerMerchantCapture
from app.models.store import Store, StoreCategory
from app.services.issuer_merchant_capture import load_capture, import_capture, capture_matches
from app.services.issuer_merchant_matching import DIRECTORY_URL, LOOKUP_URL
from app.scripts.collect_issuer_merchants import query_fields
from tests.test_issuer_merchant_matching import response
from tests.test_card_collection import NOW


def manifest(tmp_path, *, partial=False):
    pages = []
    for index, more in enumerate(['Y'] if partial else ['Y','N']):
        rows = [('00001' if index==0 else '00002','스타벅스 경희대점' if index==0 else '다른 매장',
            '서울 동대문구 경희대로 16-1','커피전문점')]
        data = response(rows,more)
        raw = json.dumps(data,ensure_ascii=False).encode()
        ref = f'page-{index:03}.json'
        (tmp_path/ref).write_bytes(raw)
        pages.append({'raw_ref':ref,'raw_sha256':hashlib.sha256(raw).hexdigest(),
            'received_at':(NOW+timedelta(seconds=index)).isoformat(),'http_status':200,
            'request':{'NXT_QY_KEY':'' if index==0 else 'next','mchtNm':'경희',
                'siDo':'서울','siGunGu':'동대문구','areaCd':'11','pntMctYn':'N'}})
    data = {'schema_version':1,'issuer_key':'shinhan','source_url':DIRECTORY_URL,'lookup_url':LOOKUP_URL,
        'region':'서울 동대문구','keyword':'경희','capture_method':'anonymous_public_browser',
        'complete':not partial,'pages':pages}
    path = tmp_path/'capture.json'
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return path,data


def add_store(db):
    row = Store(brand='스타벅스',branch='경희대점',address='서울특별시 동대문구 경희대로 16-1 (회기동)',
        category=StoreCategory.CAFE,lat=37.5,lng=127.0,is_active=True)
    db.add(row)
    db.flush()
    return row


def test_cursor_chain_import_is_idempotent_and_read_only_matches_follow_store_changes(db,tmp_path):
    path,_ = manifest(tmp_path)
    store = add_store(db)
    root = tmp_path/'archive'
    capture = import_capture(db,path,archive_root=root,now=NOW+timedelta(minutes=1))
    again = import_capture(db,path,archive_root=root,now=NOW+timedelta(minutes=2))
    assert again.id==capture.id and db.scalar(select(func.count()).select_from(IssuerMerchantCapture))==1
    assert len(capture.pages)==2 and len(capture.records)==2 and capture.complete
    result = capture_matches(db,capture,archive_root=root,now=NOW+timedelta(minutes=1))
    assert result['unique_exact_matches']==1 and result['issue'] is None
    assert not result['matches'][0]['issuer_classification_confirmed'] and not result['public_approval']
    store.address='서울 동대문구 경희대로 16-2'
    db.flush()
    assert capture_matches(db,capture,archive_root=root,now=NOW+timedelta(minutes=1))['unique_exact_matches']==0


def test_partial_capture_is_stored_without_unique_match_claim(db,tmp_path):
    path,_=manifest(tmp_path,partial=True)
    add_store(db)
    capture=import_capture(db,path,archive_root=tmp_path/'archive',now=NOW+timedelta(minutes=1))
    result=capture_matches(db,capture,archive_root=tmp_path/'archive',now=NOW+timedelta(minutes=1))
    assert not capture.complete and result['unique_exact_matches']==0
    assert result['matches'][0]['reason']=='directory_pagination_incomplete'


@pytest.mark.parametrize('change', ['cursor','keyword','region','point','hash','page_missing','future','old',
    'complete','extra_page','path_escape','reverse_time'])
def test_bad_captures_cannot_create_db_records(db,tmp_path,change):
    path,data=manifest(tmp_path)
    if change=='cursor': data['pages'][1]['request']['NXT_QY_KEY']='skipped'
    if change=='keyword': data['pages'][1]['request']['mchtNm']='다른검색'
    if change=='region': data['pages'][1]['request']['siGunGu']='중랑구'
    if change=='point': data['pages'][0]['request']['pntMctYn']='Y'
    if change=='hash': data['pages'][0]['raw_sha256']='0'*64
    if change=='page_missing': (tmp_path/'page-001.json').unlink()
    if change=='future': data['pages'][0]['received_at']=(NOW+timedelta(days=1)).isoformat()
    if change=='old': data['pages'][0]['received_at']=(NOW-timedelta(days=8)).isoformat()
    if change=='complete': data['complete']=False
    if change=='extra_page': data['pages'].append(data['pages'][1])
    if change=='path_escape': data['pages'][0]['raw_ref']='../other.json'
    if change=='reverse_time': data['pages'][1]['received_at']=(NOW-timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises((ValueError,OSError)):
        import_capture(db,path,archive_root=tmp_path/'archive',now=NOW+timedelta(minutes=1))
    assert db.scalar(select(func.count()).select_from(IssuerMerchantCapture))==0


@pytest.mark.parametrize('change,issue', [('raw','capture_archive_integrity_failure'),
    ('records','capture_records_changed'),('metadata','capture_metadata_changed'),('stale','capture_stale')])
def test_integrity_and_freshness_are_checked_on_each_review(db,tmp_path,change,issue):
    path,_=manifest(tmp_path)
    add_store(db)
    root=tmp_path/'archive'
    capture=import_capture(db,path,archive_root=root,now=NOW+timedelta(minutes=1))
    now=NOW+timedelta(minutes=2)
    if change=='raw': (root/capture.pages[0]['raw_ref']).write_bytes(b'tampered')
    if change=='records': capture.records=[dict(r,issuer_category='편의점') for r in capture.records]
    if change=='metadata': capture.keyword='다른검색'
    if change=='stale': now=NOW+timedelta(days=8)
    db.flush()
    result=capture_matches(db,capture,archive_root=root,now=now)
    assert result['issue']==issue and result['unique_exact_matches']==0


def test_request_parser_accepts_json_and_form_without_retaining_private_fields():
    payload={'NXT_QY_KEY':'','mchtNm':'경희','siDo':'서울','siGunGu':'동대문구','areaCd':'11','pntMctYn':'N',
        'cookie':'must_not_retain'}
    for raw in [json.dumps(payload),json.dumps({'mbw_json':payload}),urlencode({'mbw_json':json.dumps(payload)})]:
        parsed=query_fields(raw)
        assert parsed['mchtNm']=='경희' and 'cookie' not in parsed
    with pytest.raises(ValueError): query_fields('unrecognized')


def test_admin_api_requires_auth_and_missing_capture_is_404(client):
    path='/admin/map/issuer-merchant-captures/999/matches'
    assert client.get(path).status_code in (401,403)
    assert client.get(path,headers={'X-Admin-Token':'test-admin-token'}).status_code==404


def test_import_retains_only_public_query_fields(db,tmp_path):
    path,data=manifest(tmp_path)
    data['pages'][0]['request']['cookie']='must-not-retain'
    path.write_text(json.dumps(data),encoding='utf-8')
    capture=import_capture(db,path,archive_root=tmp_path/'archive',now=NOW+timedelta(minutes=1))
    assert 'must-not-retain' not in json.dumps(capture.pages)


def test_real_admin_review_rechecks_current_store_identity(client,db,tmp_path,monkeypatch):
    path,_=manifest(tmp_path)
    add_store(db)
    root=tmp_path/'archive'
    capture=import_capture(db,path,archive_root=root,now=NOW+timedelta(minutes=1))
    db.commit()
    monkeypatch.setattr('app.services.issuer_merchant_capture.merchant_archive_root',lambda:root)
    response=client.get(f'/admin/map/issuer-merchant-captures/{capture.id}/matches',
        headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code==200
    assert not response.json()['public_approval']
