import json
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select,func

from app.models.card_document_text import CardDocumentText
from app.models.card_collection import CardSourceSnapshot,CardCollectionAttempt
from app.models.card import Card
from app.services.toss_kpass_document import parse_kpass_document,KPASS_URL,KPASS_TEXT_PARSER
from app.services.card_document_crosscheck import prepare_document_text
from app.services.card_collection import register_source,capture_document,collect_due
from tests.test_card_collection import NOW


def payload():
    return json.loads(Path('tests/fixtures/toss-kpass-hydration.json').read_text(encoding='utf-8'))


def html(data=None):
    return (Path('tests/fixtures/toss-kpass-root.html').read_bytes()+
        b'<script id="__NEXT_DATA__" type="application/json">'+
        json.dumps(payload() if data is None else data,ensure_ascii=False).encode()+b'</script>')


def resolve(data,pointer):
    for key in pointer.lstrip('/').split('/'):
        data=data[int(key)] if isinstance(data,list) else data[key]
    return data


def test_actual_sections_and_tables_are_preserved_with_json_and_text_locators():
    data=payload()
    result=parse_kpass_document(html(data))
    assert result['sections']==['상품 안내','K-패스 안내','온라인 결제 안내','기타 안내']
    assert result['block_count']==93 and len(result['segments'])==96 and len(result['text'])==5462
    assert '그 외(미지원) 통화' in result['text']
    assert '추가 교통비 캐시백 2,000원' in result['text']
    assert {r['target'] for r in result['references']}=={'60303005','60303001'}
    assert all(r['collection_status']=='not_collected' for r in result['references'])
    for quote in result['segments']:
        import re
        assert re.sub(r'\s+',' ',resolve(data,quote['json_pointer'])).strip()==quote['quote']
        assert result['text'][quote['start_offset']:quote['end_offset']]==quote['quote']


@pytest.mark.parametrize('mode',['page','unpublished','wrong_product','duplicate_query','missing_section',
    'unknown_block','missing_children','duplicate_block','rich_conflict','table_width','invalid_children'])
def test_changed_or_incomplete_structure_cannot_be_silently_omitted(mode):
    data=payload()
    queries=data['props']['pageProps']['dehydratedState']['queries']
    resource=queries[0]['state']['data']
    blocks=resource['data']
    if mode=='page': data['page']='/another-card'
    elif mode=='unpublished': resource['isPublished']=False
    elif mode=='wrong_product': resource['title']='other product'
    elif mode=='duplicate_query': queries.append(queries[0])
    elif mode=='missing_section': blocks.pop(0)
    elif mode=='unknown_block': blocks[0]['type']='image'
    elif mode=='missing_children': blocks[0]['children']=[]
    elif mode=='duplicate_block': blocks.append(blocks[0])
    elif mode=='rich_conflict': blocks[0]['toggle']['rich_text'][0]['plain_text']='forged'
    elif mode=='invalid_children': blocks[-1]['children']={}
    else:
        def change(rows):
            for block in rows:
                if block['type']=='table': block['table']['table_width']=3; return True
                if change(block.get('children',[])): return True
        assert change(blocks)
    with pytest.raises(ValueError): parse_kpass_document(html(data))


def test_missing_or_duplicate_hydration_is_rejected():
    with pytest.raises(ValueError): parse_kpass_document(b'<html>no data</html>')
    with pytest.raises(ValueError): parse_kpass_document(html()+html())


def test_inline_fragments_do_not_create_spaces_inside_words():
    data=payload()
    block=data['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['data'][-1]
    block['paragraph']['rich_text']=[{'type':'text','plain_text':v,'text':{'content':v}} for v in ['현장','결제']]
    result=parse_kpass_document(html(data))
    assert result['text'].endswith('현장결제') and not result['text'].endswith('현장 결제')
    assert result['segments'][-1]['start_offset']==result['segments'][-2]['end_offset']


def test_derivation_preserves_original_and_is_idempotent(db,tmp_path):
    source=register_source(db,KPASS_URL,provider_key='tossbank',now=NOW)
    raw=html()
    snapshot=capture_document(db,source,raw,now=NOW,archive_root=tmp_path)
    original=snapshot.extracted_text
    row=prepare_document_text(db,source,snapshot,raw,now=NOW)
    assert row.parser_version==KPASS_TEXT_PARSER and len(row.text)==5462
    assert prepare_document_text(db,source,snapshot,raw,now=NOW).id==row.id
    assert snapshot.extracted_text==original and len(original)==438
    assert row.raw_sha256==hashlib.sha256(raw).hexdigest()
    assert db.scalar(select(func.count()).select_from(CardDocumentText))==1
    assert db.scalar(select(func.count()).select_from(Card))==0
    with pytest.raises(ValueError,match='integrity'): prepare_document_text(db,source,snapshot,b'wrong')


def test_admin_source_document_is_read_only_and_checks_current_derivation(client,db,tmp_path):
    source=register_source(db,KPASS_URL,provider_key='tossbank',now=NOW)
    snapshot=capture_document(db,source,html(),now=NOW,archive_root=tmp_path)
    derived=prepare_document_text(db,source,snapshot,html(),now=NOW)
    db.add(CardCollectionAttempt(source_id=source.id,snapshot_id=snapshot.id,status='captured',http_status=200,attempted_at=NOW))
    db.commit()
    url=f'/admin/map/card-sources/{source.id}/document'
    assert client.get(url).status_code==401
    headers={'X-Admin-Token':'test-admin-token'}
    response=client.get(url,headers=headers)
    assert response.status_code==200 and response.json()['document']['text']==derived.text
    assert response.json()['issues']==['benefit_rules_not_extracted','not_publicly_approved','linked_documents_not_collected']
    derived.text+='forged'
    db.commit()
    assert client.get(url,headers=headers).status_code==409
