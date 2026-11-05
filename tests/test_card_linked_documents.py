import hashlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select,func

from app.models.card_collection import CardCollectionSource,CardSourceSnapshot,CardCollectionAttempt,CardProductSource
from app.models.card_document_text import CardDocumentText
from app.services.card_collection import register_source,capture_document,canonical_url,bootstrap_supporting_documents,collect_due
from app.services.card_document_crosscheck import prepare_document_text,TEXT_PARSER
from app.services.card_linked_documents import DOCUMENTS,DOCUMENT_URLS,linked_document_status
from app.services.toss_kpass_document import KPASS_URL
from tests.test_toss_kpass_document import html,payload
from tests.test_card_collection import NOW


def parent(db,tmp_path,data=None):
    source=register_source(db,KPASS_URL,provider_key='tossbank',issuer=('tossbank','토스뱅크'),now=NOW)
    raw=html(data)
    snapshot=capture_document(db,source,raw,now=NOW,archive_root=tmp_path)
    text=prepare_document_text(db,source,snapshot,raw,now=NOW)
    return snapshot,text


def test_only_observed_public_download_paths_are_allowed():
    assert all(canonical_url(url)==url for url in DOCUMENT_URLS)
    for url in ['https://api.tossbank.com/api-private/users',
        'https://api.tossbank.com/api-public/disclosure/attachment/document/document-no/99999999',
        next(iter(DOCUMENT_URLS))+'?token=secret',next(iter(DOCUMENT_URLS))+'/extra']:
        with pytest.raises(ValueError): canonical_url(url)


def test_registration_is_idempotent_without_product_or_benefit_creation(db):
    first=bootstrap_supporting_documents(db,now=NOW)
    second=bootstrap_supporting_documents(db,now=NOW)
    assert first['registered_documents']==4 and first==second
    assert db.scalar(select(func.count()).select_from(CardProductSource))==0
    linked=list(db.scalars(select(CardCollectionSource).where(CardCollectionSource.canonical_url.in_(DOCUMENT_URLS))))
    assert len(linked)==2
    assert all(s.schedule_policy['interval_hours']==168 for s in linked)


def test_missing_then_blocked_references_are_not_collected(db,tmp_path):
    snapshot,text=parent(db,tmp_path)
    result=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    assert {r['status'] for r in result['documents']}=={'source_not_registered'}
    bootstrap_supporting_documents(db,now=NOW)
    for source in db.scalars(select(CardCollectionSource).where(CardCollectionSource.canonical_url.in_(DOCUMENT_URLS))):
        source.last_error='source_disallows_fetch'
    db.flush()
    result=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    assert {r['status'] for r in result['documents']}=={'collection_blocked'}
    assert not result['crosscheck_completed'] and not result['public_approval']


def test_robots_denial_never_requests_download_endpoint(db,tmp_path):
    bootstrap_supporting_documents(db,now=NOW)
    ids=list(db.scalars(select(CardCollectionSource.id).where(CardCollectionSource.canonical_url.in_(DOCUMENT_URLS))))
    db.commit()
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200,text='User-agent: *\nDisallow: /')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=collect_due(db,client=client,now=NOW,archive_root=tmp_path,only_source_ids=ids,request_interval=0)
    assert paths==['/robots.txt','/robots.txt']
    assert result['failed']==2 and result['captured']==result['auto_approved']==0
    for source in db.scalars(select(CardCollectionSource).where(CardCollectionSource.id.in_(ids))):
        assert source.last_error=='source_disallows_fetch'
        assert source.next_retry_at.replace(tzinfo=NOW.tzinfo)==NOW+timedelta(days=1)


def test_available_text_only_becomes_ready_for_crosscheck(db,tmp_path):
    snapshot,text=parent(db,tmp_path)
    bootstrap_supporting_documents(db,now=NOW)
    for source in db.scalars(select(CardCollectionSource).where(CardCollectionSource.canonical_url.in_(DOCUMENT_URLS))):
        raw=source.canonical_url.encode()
        digest=hashlib.sha256(raw).hexdigest()
        (tmp_path/digest).write_bytes(raw)
        doc=CardSourceSnapshot(source_id=source.id,fetched_at=NOW,raw_sha256=digest,semantic_sha256=digest,
            raw_object_ref=digest,extracted_text='PDF original',mime_type='application/pdf',parser_version='fixture',completeness='text_available')
        db.add(doc)
        db.flush()
        db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=doc.id,status='captured',http_status=200,attempted_at=NOW))
        value='제공받은 공식 문서의 복원 텍스트'
        db.add(CardDocumentText(snapshot_id=doc.id,parser_version=TEXT_PARSER,raw_sha256=digest,
            text_sha256=hashlib.sha256(value.encode()).hexdigest(),text=value,created_at=NOW))
    db.flush()
    result=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    assert result['status']=='ready_for_crosscheck'
    assert not result['crosscheck_completed'] and not result['public_approval']
    stale=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW+timedelta(days=8))
    assert {r['status'] for r in stale['documents']}=={'document_stale'}
    document=db.get(CardSourceSnapshot,result['documents'][0]['snapshot_id'])
    (tmp_path/document.raw_object_ref).write_bytes(b'corrupted original')
    damaged=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    assert damaged['status']=='collection_pending'
    assert damaged['documents'][0]['status']=='document_archive_integrity_failure'


def test_changed_reference_is_not_turned_into_a_guessed_url(db,tmp_path):
    data=payload()
    part=data['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['data'][0]['children'][7]['bulleted_list_item']['rich_text'][0]
    part['href']=part['text']['link']['url']='99999999'
    snapshot,text=parent(db,tmp_path,data)
    result=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    unknown=next(r for r in result['documents'] if r['document_identifier']=='99999999')
    assert unknown['status']=='unconfigured_reference' and 'source_url' not in unknown


def test_modified_archive_cannot_report_verified_references(db,tmp_path):
    snapshot,text=parent(db,tmp_path)
    (tmp_path/snapshot.raw_object_ref).write_bytes(b'forged')
    result=linked_document_status(db,snapshot,text,archive_root=tmp_path,now=NOW)
    assert result['status']=='integrity_failure' and result['documents']==[]
