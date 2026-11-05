import json
from pathlib import Path
import hashlib

import httpx
import pytest

from app.services.shinhan_disclosure import (
    ROOT, SEARCH_URL, DOWNLOAD_URL, DEEP_OIL_DISCLOSURE_URL, fetch_deep_oil_leaflet, select_deep_oil_document,
)
from app.services.card_document_crosscheck import compare_deep_oil, document_checks, TEXT_PARSER
from app.models.card_document_text import CardDocumentText
from app.models.card_collection import CardSourceSnapshot, CardCollectionAttempt
from app.services.card_collection import register_source, collect_due
from tests.test_deep_oil_extraction import draft_deep_oil, text, NOW


def result():
    return {'mbw_result':'S','mbw_json':{'data':{'nxtQyKey':''},'crdPdPbnList':[
        {'CRD_PD_GUI_N':'00031','CRD_PD_GUI_NM':'Deep Oil',
         'CRD_PD_GUI_BUL_D':'2026.08.26','CRD_PD_GUI_FIL_NM':'A'*64}]}}


def decoded():
    return Path('tests/fixtures/shinhan-deep-oil-leaflet-decoded.txt').read_text(encoding='utf-8')


@pytest.mark.parametrize('mode,error', [('ambiguous','disclosure_product_ambiguous'),
    ('variant','disclosure_product_ambiguous'),('paged','disclosure_search_incomplete'),
    ('token','disclosure_document_identity_invalid'),('date','disclosure_document_date_invalid'),
    ('failed','disclosure_search_failed')])
def test_ambiguous_or_incomplete_search_is_rejected(mode,error):
    data = result()
    rows = data['mbw_json']['crdPdPbnList']
    if mode=='ambiguous': rows.append(dict(rows[0]))
    elif mode=='variant': rows[0]['CRD_PD_GUI_NM']='Deep Oil(배민커넥트)'
    elif mode=='paged': data['mbw_json']['data']['nxtQyKey']='more'
    elif mode=='token': rows[0]['CRD_PD_GUI_FIL_NM']='https://untrusted.example'
    elif mode=='date': rows[0]['CRD_PD_GUI_BUL_D']='2026.99.99'
    else: data['mbw_result']='F'
    with pytest.raises(ValueError,match=error): select_deep_oil_document(data)


@pytest.mark.parametrize('failure', [None,'expired','html','redirect','robots','size'])
def test_fetch_refreshes_link_and_rejects_html_expiry_and_unapproved_routes(failure,monkeypatch):
    from app.core.config import settings
    if failure=='size': monkeypatch.setattr(settings,'map_source_max_bytes',1000)
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path=='/robots.txt':
            return httpx.Response(200,text='User-agent: *\n'+('Disallow: /mob/' if failure=='robots' else 'Allow: /'))
        if str(request.url)==DEEP_OIL_DISCLOSURE_URL: return httpx.Response(200,text='public search')
        if str(request.url)==SEARCH_URL:
            from urllib.parse import parse_qs
            body = json.loads(parse_qs(request.content.decode())['mbw_json'][0])
            assert body=={'crdPdGuiNm':'Deep Oil','crdTcd':'0','nxtQyKey':''}
            return httpx.Response(200,json=result())
        assert str(request.url).startswith(DOWNLOAD_URL+'?')
        assert request.url.params['pbnNm']=='Deep Oil' and request.url.params['aFilenm']=='A'*64
        if failure=='redirect': return httpx.Response(302,headers={'location':'https://untrusted.example/'})
        body = {'expired':b'<html>scwi.err.hpp.custm.crdpdpbn.err001</html>',
                'html':b'<html>error</html>','size':b'%PDF'+b'a'*2000}.get(failure,b'%PDF document')
        return httpx.Response(200,content=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if failure:
            with pytest.raises(ValueError): fetch_deep_oil_leaflet(client)
        else:
            assert fetch_deep_oil_leaflet(client)==(b'%PDF document',{})
            assert len(paths)==4
    if failure=='robots': assert paths==['/robots.txt']


def test_actual_pdf_conflicts_are_not_silently_corrected(db):
    version = draft_deep_oil(db)
    check = compare_deep_oil(version.payload,text(),decoded())
    assert check['status']=='conflict' and len(check['checked_fields'])==10
    assert {c['field'] for c in check['conflicts']}=={'spend.higher_label','outside_scope.cinema.brand'}
    assert check['conflicts'][0]['leaflet_value']=='70만원 미만 이상'
    for quote in check['leaflet_evidence']:
        assert decoded()[quote['start_offset']:quote['end_offset']]==quote['quote']
    for quote in check['primary_evidence']:
        assert text()[quote['start_offset']:quote['end_offset']]==quote['quote']
    assert 'cap_boundary_policy' in check['unresolved']


def attach_deep_oil_leaflet(db):
    source = register_source(db,DEEP_OIL_DISCLOSURE_URL,provider_key='shinhan',kind='leaflet',
        issuer=('shinhan','신한카드'),now=NOW)
    digest = hashlib.sha256(b'%PDF fixture').hexdigest()
    snapshot = CardSourceSnapshot(source_id=source.id,fetched_at=NOW,raw_sha256=digest,
        semantic_sha256=digest,raw_object_ref='fixture',extracted_text='original text',
        mime_type='application/pdf',parser_version='fixture',completeness='text_available')
    db.add(snapshot)
    db.flush()
    db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=snapshot.id,status='captured',http_status=200,attempted_at=NOW))
    db.add(CardDocumentText(snapshot_id=snapshot.id,parser_version=TEXT_PARSER,raw_sha256=digest,
        text_sha256=hashlib.sha256(decoded().encode()).hexdigest(),text=decoded(),created_at=NOW))
    db.commit()
    return source


def test_document_crosscheck_is_required_and_displayed_without_approval(client,db):
    version = draft_deep_oil(db)
    assert document_checks(db,version,now=NOW)['status']=='source_unavailable'
    source = attach_deep_oil_leaflet(db)
    check = document_checks(db,version,now=NOW)
    assert check['required'] and check['status']=='conflict'
    response = client.get(f'/admin/map/card-programs/{version.id}',headers={'X-Admin-Token':'test-admin-token'})
    assert response.status_code==200
    readiness = response.json()['readiness']
    assert not readiness['can_approve'] and readiness['blocker']=='supporting_document_conflict'
    source.last_error='disclosure_download_expired'
    db.commit()
    assert document_checks(db,version,now=NOW)['status']=='source_unavailable'


def test_expired_html_is_recorded_as_failure_not_successful_document(db,tmp_path):
    source = register_source(db,DEEP_OIL_DISCLOSURE_URL,provider_key='shinhan',kind='leaflet',
        issuer=('shinhan','신한카드'),now=NOW)
    db.commit()
    def handler(request):
        if request.url.path=='/robots.txt': return httpx.Response(200,text='User-agent: *\nAllow: /')
        if str(request.url)==SEARCH_URL: return httpx.Response(200,json=result())
        return httpx.Response(200,text='scwi.err.hpp.custm.crdpdpbn.err001')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome=collect_due(db,client=client,now=NOW,archive_root=tmp_path,request_interval=0,trigger='test')
    assert outcome['failed']==1 and outcome['captured']==0
    db.refresh(source)
    assert source.last_error=='disclosure_download_expired'
    assert not list(tmp_path.rglob('*.pdf'))
