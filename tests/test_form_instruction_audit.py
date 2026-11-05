"""Independent source-instruction gates must survive summary/error clearing."""
from copy import deepcopy
from functools import lru_cache
import uuid

import pytest

from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services.form_instruction_audit import audit_instructions
from app.services.scholarship_readiness import form_fingerprint
from app.services.writing_requirements import writing_fingerprint
from app.services.writing_chat import start_session
from reports.atomic_input_reference import record_of
from reports.measure_review_backlog import fresh_result, isolated_gate
from reports.form_semantic_reference import SOURCE_SHA256
from tests.test_writing_workflow import setup_writing, action
from tests.test_writing_flow import recommend, select_card


@lru_cache(maxsize=1)
def cached_original():
    return fresh_result()


def fixture():
    return deepcopy(cached_original())


def test_original_pipeline_keeps_both_instruction_sources_and_version():
    blocks,result=fixture()
    assert result['extractor_version']=='structured-v41'
    audit=result['coverage']['native_instruction_audit']
    assert audit['unresolved']==2 and not audit['independent_human_review_complete']
    assert [[record_of(s) for s in c['sources']] for c in audit['controls']]==[[154,1056],[276,383]]
    body='\n'.join(b['text'] for b in blocks)
    for control in audit['controls']:
        assert not control['automatic_resolution'] and control['status']=='requires_original_review'
        for p in control['sources']:
            assert body[p['text_start']:p['text_end']]==p['evidence'] and p['sha256']==SOURCE_SHA256
    assert 'native_instruction_differences_require_original_review' in result['issues']


@pytest.mark.parametrize('kind',['essay_page_rule_difference','economic_document_rule_difference'])
@pytest.mark.parametrize('tamper',['summary','status','issues'])
def test_each_gate_blocks_independently_with_matching_fingerprint(kind,tamper):
    _,result=fixture()
    control=next(c for c in result['coverage']['native_instruction_audit']['controls'] if c['kind']==kind)
    if tamper=='status':control['status']='bound'
    elif tamper=='issues':control['issues']=[]
    field={'id':'test','label':'원문 입력','evidence':'원문 입력','field_type':'text','source':{'sha256':SOURCE_SHA256}}
    state=isolated_gate([field],'native_instruction_audit',{'unresolved':0,'controls':[control]})
    assert not state['writing_ready'] and state['recommendation_ready']
    assert 'application_form_conflicts' in state['reasons']


@pytest.mark.parametrize('change',['no_cover','other_subject','ocr_cover','different_rule_source','no_groups','no_economic_footer','wrong_economic_table'])
def test_unproven_or_unrelated_instruction_is_not_borrowed(change):
    blocks,result=fixture()
    groups=result['form_groups']
    at=lambda record:next(b for b in blocks if record_of(b)==record)
    if change=='no_cover':blocks.remove(at(154))
    elif change=='other_subject':at(154)['text']='추천서는 A4 3장 내외'
    elif change=='ocr_cover':at(154)['method']='ocr'
    elif change=='different_rule_source':groups[0]['source']['sha256']='other-document'
    elif change=='no_groups':groups=[]
    elif change=='no_economic_footer':blocks.remove(at(383))
    else:at(276)['table']='unrelated'
    audit=audit_instructions(blocks,groups,{'sha256':SOURCE_SHA256})
    kinds={c['kind'] for c in audit['controls']}
    assert ('economic_document_rule_difference' if change in ('no_economic_footer','wrong_economic_table') else 'essay_page_rule_difference') not in kinds


def test_equal_page_instruction_does_not_create_difference():
    blocks,result=fixture()
    next(b for b in blocks if record_of(b)==154)['text']='* 자소서는 A4 3장 이내'
    audit=audit_instructions(blocks,result['form_groups'],{'sha256':SOURCE_SHA256})
    assert [c['kind'] for c in audit['controls']]==['economic_document_rule_difference']


@pytest.mark.parametrize('kind',['essay_page_rule_difference','economic_document_rule_difference'])
def test_admin_approval_and_existing_new_writing_sessions_are_blocked(auth_client,db,kind):
    _,result=fixture()
    control=next(c for c in result['coverage']['native_instruction_audit']['controls'] if c['kind']==kind)
    # Isolate the actual source audit in an otherwise approved synthetic test DB row.
    row,session=setup_writing(auth_client,db)
    stored=db.get(WritingSession,uuid.UUID(session['id']))
    row.eligibility={**row.eligibility,'form_coverage':[{'native_instruction_audit':{'unresolved':0,'controls':[control]}}],
                     'extraction_issues':[]}
    row.eligibility={**row.eligibility,'form_review':{'fingerprint':form_fingerprint(row)}}
    # Keep fingerprint current to prove the actual blocked-state gate, rather than stale alone.
    stored.form_fingerprint=writing_fingerprint(row)
    db.commit()
    loaded=auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded['state']=='blocked' and loaded['next_action'] is None
    for path,body,method in [('messages',{'message':'이번 신청의 실제 본인 사실입니다.'},'post'),
                             ('draft',{},'post'),('review',{'confirmed':True},'post'),('save',{},'post'),
                             (f"fields/{stored.fields[0]['id']}/reason",{'message':'실제 수정할 본인 사실입니다.'},'put')]:
        action(auth_client,loaded,path,body,method=method,expected=409)
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code==409
    started=start_session(db,stored.user_id,row,UserProfile())
    assert started['state']=='blocked'
    selected=select_card(auth_client,recommend(auth_client),row.id)
    assert selected.status_code==200 and selected.json()['state']=='blocked' and selected.json()['next_action'] is None
    url=f'/admin/crawl/scholarships/{row.id}/review'
    headers={'X-Admin-Token':'test-admin-token'}
    context=auth_client.get(url,headers=headers).json()
    assert context['coverage'][0]['native_instruction_audit']['controls'][0]['sources']==control['sources']
    response=auth_client.post(url,headers=headers,json={'attempt_id':context['attempt_id'],
        'fingerprint':context['fingerprint'],'reviewed_all_documents':True})
    assert response.status_code==409
