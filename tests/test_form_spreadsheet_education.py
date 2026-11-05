from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.form_spreadsheet_education import native_education, audit_education, VERSION
from app.services.form_spreadsheet_repeat import SOURCE_SHA
from app.services.posting_extract import extract_structured
from app.services.form_profile_inputs import render_input
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.services.writing_chat import start_session
from app.schemas.schemas import UserProfile
from tests.test_form_spreadsheet_repeat import fixture
from tests.test_writing_workflow import setup_writing, action


def education_field():
    p = fixture()
    groups, audit = native_education(p['blocks'], {'sha256': SOURCE_SHA}, p['spreadsheet_inventory'])
    assert audit['unresolved'] == 0
    return groups[0]['field']


def test_native_source_restores_education_without_model_template_facts(monkeypatch):
    p = fixture()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    # Model treats printed names as applicant fields: all those candidates must be suppressed.
    candidates = [{'label': b['text'], 'evidence': b['text'], 'field_type': 'text', 'role': 'unknown'}
                  for b in p['blocks'] if b.get('row') in range(13, 18)]
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **kw: json.dumps({'application_fields': candidates}))
    result = extract_structured(p['extracted_text'], {'sha256': SOURCE_SHA}, p['blocks'], spreadsheet_inventory=p['spreadsheet_inventory'])
    fields = [f for f in result['application_fields'] if f.get('input_spec', {}).get('version') == VERSION]
    assert len(fields) == 1 and result['coverage']['native_spreadsheet_education_audit']['unresolved'] == 0
    assert len(requirements(fields[0])) == 5
    assert all('present' in r['key'] and r['destination'] is None for r in requirements(fields[0]))
    gold = json.loads(open('reports/scholarship-improvement-20261011-spreadsheet-source-verified/gold.json', encoding='utf-8').read())
    original = next(t for t in gold['scoped_repeat_tables'] if t['key'] == 'education')
    for actual, expected in zip(fields[0]['input_spec']['education_rows'], original['row_areas']):
        assert [tuple(s['destination'][k] for k in ('row', 'column', 'row_span', 'column_span', 'value', 'blank', 'location')) for s in actual['slots']] == [
            tuple(c[k] for k in ('row', 'column', 'row_span', 'column_span', 'value', 'blank', 'location')) for c in expected]
    owned = native_education(p['blocks'], {'sha256': SOURCE_SHA}, p['spreadsheet_inventory'])[0][0]['locations']
    assert not any(f['source']['location'] in owned for f in result['application_fields'] if f['id'] != fields[0]['id'])


@pytest.mark.parametrize('problem', ['no_inventory', 'hash', 'school', 'month', 'status', 'hidden', 'span', 'duplicate', 'missing_label', 'missing_field', 'altered_spec'])
def test_unproven_templates_block_even_reviewed_form(problem):
    p = fixture()
    inventory = deepcopy(p['spreadsheet_inventory'])
    blocks = deepcopy(p['blocks'])
    if problem == 'no_inventory':
        inventory = None
    elif problem == 'hash':
        inventory['source_sha256'] = 'wrong'
    elif problem == 'missing_label':
        blocks = [b for b in blocks if not (b.get('row') == 12 and b.get('column') == 1)]
    else:
        cell = next(a for a in inventory['sheets'][0]['areas'] if a['row'] == 13 and a['column'] == 8)
        if problem == 'school':
            cell['value'] = '가짜학교'
        elif problem in ('month', 'status'):
            cell = next(a for a in inventory['sheets'][0]['areas'] if a['row'] == 15 and a['column'] == (3 if problem == 'month' else 21))
            cell['value'] = '바뀐 틀'
        elif problem == 'hidden':
            cell['hidden'] = True
        elif problem == 'span':
            cell['column_span'] += 1
        elif problem == 'duplicate':
            inventory['sheets'][0]['areas'].append(deepcopy(cell))
    groups, _ = native_education(blocks, {'sha256': SOURCE_SHA}, inventory)
    fields = [g['field'] for g in groups]
    if problem == 'missing_field':
        fields = []
    elif problem == 'altered_spec':
        fields[0]['input_spec']['education_rows'][0]['slots'][0]['destination']['column'] = 9
    audit = audit_education(blocks, fields, {'sha256': SOURCE_SHA}, inventory)
    assert audit['unresolved'] > 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash='test', eligibility={
        'application_fields': fields, 'form_coverage': [{'native_spreadsheet_education_audit': audit}],
        'extraction_issues': [], 'validation_version': VALIDATION_VERSION})
    row.eligibility['form_review'] = {'fingerprint': form_fingerprint(row)}
    assert not readiness(row)['writing_ready']


def test_row_branch_dates_choices_and_clearing():
    field = education_field()
    answers = {}
    for i in range(5):
        _set_fact(answers, field, f'education_{i}_present', '작성하지 않음', {})
    _set_fact(answers, field, 'education_4_present', '작성함', {})
    assert len(requirements(field, answers[field['id']])) == 10
    for suffix, value in [('graduation', '졸업 전'), ('school', '검증대학교'), ('major', '컴퓨터공학'), ('status', '재학'), ('activities', '검증 동아리')]:
        _set_fact(answers, field, 'education_4_' + suffix, value, {})
    text = render_input(field, answers[field['id']])
    assert '검증대학교' in text and '원본 칸을 비워 둠(사용자 응답: 졸업 전)' in text
    for key, value in [('graduation','2024-13'), ('graduation','2024-02-29'), ('graduation','0000-01'), ('status','졸업'), ('school','대학교')]:
        with pytest.raises(HTTPException):
            _check_choice(field, value, 'education_4_' + key, answers[field['id']])
    _check_choice(field, '2024-02', 'education_4_graduation', answers[field['id']])
    _check_choice(field, '표시하지 않음', 'education_4_status', answers[field['id']])
    with pytest.raises(HTTPException):
        _set_fact(answers, field, 'education_0_school', '검증초등학교', {})
    _set_fact(answers, field, 'education_4_present', '작성하지 않음', {})
    assert set(answers[field['id']]['facts']) == {f'education_{i}_present' for i in range(5)}
    assert '검증대학교' not in render_input(field, answers[field['id']])
    _set_fact(answers, field, 'education_4_present', '작성함', {})
    assert any(r['key'] == 'education_4_school' for r in requirements(field, answers[field['id']]))
    assert 'education_4_school' not in answers[field['id']]['facts']


def test_api_education_collect_export_and_branch_revision(auth_client, db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial['id']))
    field = education_field()
    row.eligibility = {**row.eligibility, 'application_fields': [field], 'form_groups': [], 'form_exclusions': [], 'form_coverage': [], 'extraction_issues': []}
    row.eligibility = {**row.eligibility, 'form_review': {'fingerprint': form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session['next_action']['kind'] == 'qualification':
        session = action(auth_client, session, 'messages', {'message': '네, 공고 조건에 해당합니다.'})
    # First three rows unused, college graduated, university enrolled.
    for reply in ['작성하지 않음'] * 3 + ['작성함', '2024-02', '검증전문대학', '컴퓨터공학', '졸업', '해당 없음',
                                            '작성함', '졸업 전', '검증대학교', '컴퓨터공학', '재학', '검증 동아리']:
        session = action(auth_client, session, 'messages', {'message': reply})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {'confirmed': True})
    session = action(auth_client, session, 'draft')
    action(auth_client, session, 'review', {'confirmed': True}, expected=409)
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    for fmt in ('txt', 'docx'):
        assert auth_client.get(f"/writing-sessions/{session['id']}/export?format={fmt}").status_code == 200
    session = action(auth_client, session, f"fields/{field['id']}/education_4_present", {'message': '작성하지 않음'}, method='put')
    assert not session['drafts']
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
