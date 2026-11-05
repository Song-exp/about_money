"""Real original essays: source contracts, missing facts, review and export invalidation."""
import json
import uuid
from copy import deepcopy

import pytest

from app.core.config import settings
from app.models.writing_session import WritingSession
from app.models.scholarship import Scholarship
from app.schemas.schemas import UserProfile
from app.services import llm
from app.services.scholarship_readiness import form_fingerprint
from app.services.writing_chat import start_session
from app.services.writing_requirements import requirements
from app.services.essay_collection import detail_slots
from reports.essay_reference import build_essay_gold, audit_essays
from reports.measure_essays import BASE, ROOT, PREDICTIONS, measure_collection
from reports.measure_single_inputs import fresh_basic_fields, original_bytes
from reports.form_semantic_reference import SOURCE_SHA256
from app.services.posting_extract import extract_structured
from tests.test_submission_consent_reference import api_session, finish_collection, exported_texts
from tests.test_writing_workflow import action


def fixture():
    gold = json.loads((ROOT / 'gold.json').read_text(encoding='utf-8'))
    prediction = json.loads((PREDICTIONS / 'r1-prediction.json').read_text(encoding='utf-8'))
    fields = [f for f in prediction['application_fields'] if f['field_type'] == 'essay']
    return gold, fields, prediction['form_groups']


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)


def test_original_reference_rebuild_and_fact_preservation():
    gold, fields, groups = fixture()
    inventory = json.loads((BASE / 'scholarship-improvement-20261010-source-records-verified/source-record-gold.json').read_text(encoding='utf-8'))
    assert json.loads(json.dumps(build_essay_gold(inventory))) == gold
    before = deepcopy((fields, groups))
    assert audit_essays(gold, fields, groups)['unresolved_contracts'] == 0
    assert [c['slots'] for c in measure_collection(fields, groups)['collected']] == [13,4,2,3]
    assert (fields, groups) == before
    inventory['source_sha256'] = 'other'
    with pytest.raises(ValueError):
        build_essay_gold(inventory)


def test_fresh_original_parser_restores_essays_without_model_candidates(monkeypatch):
    gold, _, _ = fixture()
    blocks, _ = fresh_basic_fields(original_bytes())
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *args, **kwargs: json.dumps({'application_fields': []}))
    result = extract_structured('\n'.join(b['text'] for b in blocks), {'sha256': SOURCE_SHA256}, blocks)
    assert audit_essays(gold, result['application_fields'], result['form_groups'])['unresolved_contracts'] == 0


@pytest.mark.parametrize('change', ['missing_prompt','duplicate_prompt','wrong_label','wrong_source','wrong_cell',
    'missing_detail','duplicate_detail','wrong_detail_source','wrong_context','wrong_context_source',
    'missing_copy_rule','wrong_copy_source','per_prompt_pages','missing_group','crossed_member',
    'missing_membership','wrong_pages','missing_format','wrong_format_source','wrong_shared_source'])
def test_contract_detects_corruption(change):
    gold, fields, groups = fixture()
    f, g = fields[0], groups[0]
    if change == 'missing_prompt': fields.pop()
    elif change == 'duplicate_prompt': fields.append(deepcopy(f))
    elif change == 'wrong_label': f['label'] = '일반 자기소개'
    elif change == 'wrong_source': f['source']['sha256'] = 'other'
    elif change == 'wrong_cell': f['input_cell_source']['record'] = 1094
    elif change == 'missing_detail': f['content_requirements'].pop()
    elif change == 'duplicate_detail': f['content_requirements'].append(deepcopy(f['content_requirements'][0]))
    elif change == 'wrong_detail_source': f['content_requirements'][0]['source']['location'] = 'record:1087'
    elif change == 'wrong_context': f['content_requirements'][0]['context'] = '다른 질문'
    elif change == 'wrong_context_source': f['content_requirements'][0]['context_source']['sha256'] = 'other'
    elif change == 'missing_copy_rule': f.pop('history_reuse_policy')
    elif change == 'wrong_copy_source': f['history_reuse_policy']['sources'][0]['sha256'] = 'other'
    elif change == 'per_prompt_pages': f['max_length'] = 3
    elif change == 'missing_group': groups.clear()
    elif change == 'crossed_member': g['field_ids'].reverse()
    elif change == 'missing_membership': f['group_ids'] = []
    elif change == 'wrong_pages': g['constraints']['max_length'] = 12
    elif change == 'missing_format': g['format_rules'].pop()
    elif change == 'wrong_format_source': g['format_rules'][0]['source']['sha256'] = 'other'
    else: g['source']['sha256'] = 'other'
    assert audit_essays(gold, fields, groups)['unresolved_contracts'] > 0


def essay_session(client, db):
    _, fields, groups = fixture()
    initial = api_session(client, db, fields)
    stored = db.get(WritingSession, uuid.UUID(initial['id']))
    row = db.get(Scholarship, stored.scholarship_id)
    row.eligibility = {**row.eligibility, 'form_groups': groups}
    row.eligibility = {**row.eligibility, 'form_review': {'fingerprint': form_fingerprint(row)}}
    db.commit()
    session = start_session(db, stored.user_id, row, UserProfile())
    if session['next_action']['kind'] == 'qualification':
        session = action(client, session, 'messages', {'message': '네, 공고 조건에 해당합니다.'})
    return session, fields, groups


def test_real_four_essays_collect_review_export_and_revise(auth_client, db):
    session, fields, groups = essay_session(auth_client, db)
    for field in fields:
        action(auth_client, session, f"fields/{field['id']}/confirm", {'confirmed': True}, expected=409)
        assert auth_client.get(f"/writing-sessions/{session['id']}/history", params={'field_id': field['id']}).status_code == 422
        action(auth_client, session, 'history', {'field_id': field['id'], 'slot_key': requirements(field)[0]['key'],
               'document_id': 1, 'quote': '이전 신청 내용', 'confirmed': True}, expected=422)
    action(auth_client, session, 'draft', expected=409)
    values = {(f['id'], s['key']): ('지원 있음' if s.get('response_options') else f"이번 신청 사실 {i}-{j}: 본인이 확인한 내용입니다.")
              for i, f in enumerate(fields) for j, s in enumerate(detail_slots(f) or requirements(f))}
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, 'draft')
    assert len(session['drafts']) == 4
    assert set(session['group_validations'][0]['warnings']) == {'original_layout_page_check_required','original_group_format_check_required'}
    action(auth_client, session, 'review', {'confirmed': True}, expected=409)
    session = action(auth_client, session, 'review', {'confirmed': True,'manual_checks_confirmed': True})
    for text in exported_texts(auth_client, session):
        assert all(value in text for value in values.values())
        assert groups[0]['evidence'] in text
    old = session
    field, key = fields[0], requirements(fields[0])[0]['key']
    before = deepcopy(session['answers'])
    session = action(auth_client, session, f"fields/{field['id']}/{key}", {'message': '수정 후 본인이 확인한 새 소개입니다.'}, method='put')
    assert not session['drafts'] and not session['draft_reviewed']
    assert all(session['answers'][f['id']] == before[f['id']] for f in fields[1:])
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    action(auth_client, old, f"fields/{field['id']}/confirm", {'confirmed': True}, expected=409)
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded['answers'] == session['answers']
    session = finish_collection(auth_client, loaded, values)
    session = action(auth_client, session, 'draft')
    action(auth_client, session, 'review', {'confirmed': True}, expected=409)
    session = action(auth_client, session, 'review', {'confirmed': True,'manual_checks_confirmed': True})
    for text in exported_texts(auth_client, session):
        assert '수정 후 본인이 확인한 새 소개입니다.' in text
        assert values[(field['id'],key)] not in text


def test_care_activity_does_not_fill_frequency(auth_client, db):
    session, fields, _ = essay_session(auth_client, db)
    field = fields[0]
    rule = next(r for r in detail_slots(field) if r.get('collection_label') == '돌봄 내용')
    session = action(auth_client, session, f"fields/{field['id']}/{rule['key']}", {'message': '병원에 동행합니다.'}, method='put')
    assert session['answers'][field['id']]['facts'][rule['key']]['text'] == '병원에 동행합니다.'
    frequency = next(r for r in detail_slots(field) if r.get('collection_label') == '돌봄 빈도')
    assert frequency['key'] not in session['answers'][field['id']]['facts']
    assert not fixture()[0]['compound_response_completeness_verified']
