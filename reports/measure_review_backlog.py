"""Source-first unresolved-review manifest and current isolated approval gates."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models.scholarship import PostingStatus
from app.services.hwp_source_inventory import source_inventory
from app.services.posting_extract import extract_structured, EXTRACTOR_VERSION
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256
from reports.measure_single_inputs import original_bytes, fresh_basic_fields
from reports.measure_whole_form_inventory import read, save
from reports.measure_essays import PREDICTIONS

ROOT=Path(__file__).resolve().parent/'scholarship-improvement-20261010-review-backlog-verified'
RENDER=Path(__file__).resolve().parent/'scholarship-improvement-20261010-control-context-verified'


def build_review_gold(inventory):
    assert inventory['source_sha256']==SOURCE_SHA256
    texts={t['record']:t for t in inventory['sections'][0]['texts']}
    uncertain=[c for c in inventory['sections'][0]['list_records'] if c['kind']!='cell_geometry_candidate']
    assert {c['record'] for c in uncertain}=={13,315,349,407,495,1103,1144}
    definitions=[('address_geometry',[492], 'native_basic_input_audit'),
                 ('applicant_consent_reference',[1435,1216,1271,1379], 'native_reference_audit'),
                 ('essay_page_rule_difference',[154,1056], 'native_instruction_audit'),
                 ('economic_document_rule_difference',[276,383], 'native_instruction_audit'),
                 ('body_consent_person_relation',[749,753,763,772,1466], None)]
    return {'version':'unresolved-original-review-reference-v1','source_sha256':SOURCE_SHA256,
            'items':[{'kind':kind,'sources':[texts[r] for r in records], 'audit':audit,
                      'resolution':'unresolved_original_review','automatic_resolution':False} for kind,records,audit in definitions],
            'uncertain_list_records':uncertain,'independent_human_review_complete':False,
            'whole_form_complete':False,'complete_atomic_gold':False}


def fresh_result():
    blocks,_=fresh_basic_fields(original_bytes())
    with patch('app.services.llm.has_llm',return_value=True), patch('app.services.llm.chat_complete',return_value='{"application_fields":[]}'):
        result=extract_structured('\n'.join(b['text'] for b in blocks),{'sha256':SOURCE_SHA256},blocks)
    return blocks,result


def isolated_gate(fields,audit_kind,audit):
    row=SimpleNamespace(status=PostingStatus.OPEN,deadline_at=None,content_hash='isolated-review-test',eligibility={
        'application_fields':fields,'extraction_issues':[],'validation_version':VALIDATION_VERSION,
        'form_coverage':[{audit_kind:audit}]})
    row.eligibility['form_review']={'fingerprint':form_fingerprint(row)}
    return readiness(row)


def main():
    ROOT.mkdir(exist_ok=True)
    gold=build_review_gold(source_inventory(original_bytes()))
    path=ROOT/'gold.json'
    if path.exists(): assert read(path)==gold,'frozen review manifest changed'
    else: save(path,gold)
    blocks,result=fresh_result()
    save(ROOT/'fresh-v31-extraction.json',result)
    audit=result['coverage']['native_instruction_audit']
    assert [c['kind'] for c in audit['controls']]==['essay_page_rule_difference','economic_document_rule_difference']
    assert [[record_of(s) for s in c['sources']] for c in audit['controls']]==[[154,1056],[276,383]]
    gates=[]
    address=next(f for f in result['application_fields'] if record_of(f['source'])==492)
    assert address['input_cell_source']['record']==495 and address['input_cell_source']['geometry_proven'] is False
    basic=next(c for c in result['coverage']['native_basic_input_audit']['controls'] if c.get('kind')=='contact')
    reference=result['coverage']['native_reference_audit']['controls'][0]
    for kind,audit_kind,control in [('address_geometry','native_basic_input_audit',basic),
                                  ('applicant_consent_reference','native_reference_audit',reference),
                                  *[(c['kind'],'native_instruction_audit',c) for c in audit['controls']]]:
        field={'id':'isolated','label':'원문 사실','field_type':'text','evidence':'원문 사실','source':{'sha256':SOURCE_SHA256}}
        state=isolated_gate([field],audit_kind,{'unresolved':0,'controls':[control]})
        assert not state['writing_ready'] and state['recommendation_ready']
        gates.append({'kind':kind,'summary_unresolved_cleared':True,'matching_review_fingerprint':True,'state':state})
    save(ROOT/'isolated-gates.json',gates)
    runs=[]
    for index in range(1,4):
        original_bytes(index)
        saved=read(PREDICTIONS/f'r{index}-prediction.json')
        assert 'native_instruction_audit' not in saved['coverage']
        runs.append({'run':index,'saved_v28_has_independent_instruction_gate':False,
                     'current_source_instruction_review_items':2,'isolated_gate_checks':4})
    evidence={'pdf_sha256':hashlib.sha256((RENDER/'original-render.pdf').read_bytes()).hexdigest(),
              'pages':[{'page':n,'sha256':hashlib.sha256((RENDER/f'page-{n}.png').read_bytes()).hexdigest()} for n in (1,2,3,4,5)],
              'method':'existing_original_hancom_render_and_assistant_inspection_not_independent_human_review'}
    save(ROOT/'render-evidence.json',evidence)
    summary={'gold_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'extractor_version':EXTRACTOR_VERSION,
             'runs':runs,'instruction_review_items':2,'isolated_approval_gates_blocked':4,'uncertain_list_records':7,
             'new_llm_calls':0,'database_written':False,'complete_atomic_gold':False,'whole_form_complete':False,
             'independent_human_review_complete':False,'measurement':'saved_source_inventory_plus_fresh_native_pipeline_with_stubbed_empty_model_not_new_model_runs'}
    save(ROOT/'summary.json',summary)
    print(json.dumps(summary))


if __name__=='__main__':main()
