from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_parser import parse_document
from app.services.posting_extract import extract_structured
from app.services.form_spreadsheet_repeat import native_spreadsheet_repeats, audit_spreadsheet_repeats, SOURCE_SHA
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_chat import start_session
from app.services.writing_drafts import compose
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from tests.test_writing_workflow import setup_writing, action


def fixture():
    root = Path('reports/scholarship-improvement-20261011-v36-actual/notice-new-555351')
    parsed = parse_document(next(root.glob('asset-*-a0167c494ee6.bin')).read_bytes(), 'form.xls')
    return parsed


def fields():
    p = fixture()
    return [g['field'] for g in native_spreadsheet_repeats(p['blocks'], {'sha256': SOURCE_SHA}, p['spreadsheet_inventory'])[0]]


def test_empty_model_restores_three_groups_and_suppresses_header_duplicates(monkeypatch):
    p = fixture()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **kw: json.dumps({'application_fields': []}))
    result = extract_structured(p['extracted_text'], {'sha256': SOURCE_SHA}, p['blocks'], spreadsheet_inventory=p['spreadsheet_inventory'])
    groups = [f for f in result['application_fields'] if f.get('input_spec',{}).get('version') == 'spreadsheet-repeating-v1']
    assert len(groups) == 3
    assert [len(f['input_spec']['repeat']['templates']) for f in groups] == [3,3,5]
    assert result['coverage']['native_spreadsheet_repeat_audit']['unresolved'] == 0
    assert all(f['field_type'] == 'table' for f in groups)


@pytest.mark.parametrize('problem', ['missing_inventory','wrong_hash','filled','hidden','span','duplicate','header','lost_field'])
def test_unproven_geometry_or_binding_blocks_writing(problem):
    p = fixture()
    inventory = deepcopy(p['spreadsheet_inventory'])
    blocks = deepcopy(p['blocks'])
    if problem == 'missing_inventory':
        inventory = None
    elif problem == 'wrong_hash':
        inventory['source_sha256'] = 'wrong'
    elif problem == 'header':
        next(b for b in blocks if b['text'] == '동거여부[Y/N]')['text'] = '다른 값'
    else:
        cell = next(a for a in inventory['sheets'][0]['areas'] if a['row'] == 33 and a['column'] == 3)
        if problem == 'filled':
            cell.update(blank=False, value='미리 기입')
        elif problem == 'hidden':
            cell['hidden'] = True
        elif problem == 'span':
            cell['column_span'] = 5
        elif problem == 'duplicate':
            inventory['sheets'][0]['areas'].append(deepcopy(cell))
    groups, _ = native_spreadsheet_repeats(blocks, {'sha256':SOURCE_SHA}, inventory)
    selected = [g['field'] for g in groups]
    if problem == 'lost_field':
        selected.pop()
    audit = audit_spreadsheet_repeats(blocks, selected, {'sha256':SOURCE_SHA}, inventory)
    assert audit['unresolved'] > 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash='test', eligibility={
        'application_fields':selected, 'form_coverage':[{'native_spreadsheet_repeat_audit':audit}],
        'extraction_issues':[], 'validation_version':VALIDATION_VERSION})
    row.eligibility['form_review'] = {'fingerprint':form_fingerprint(row)}
    assert not readiness(row)['writing_ready']


def test_original_destinations_date_enum_capacity_and_count_change():
    qualification, award, family = fields()
    answers = {}
    _set_fact(answers, qualification, 'entry_count', '1', {})
    slots = requirements(qualification, answers[qualification['id']])
    assert len(slots) == 5 and slots[1]['destination']['row'] == 20
    for key, value in [('name','검증용 자격'), ('level','1급'), ('date','2024-02-29'), ('issuer','검증용 기관')]:
        _set_fact(answers, qualification, 'entry_0_'+key, value, {})
    assert '2024-02-29' in compose(qualification, answers[qualification['id']], SimpleNamespace(title='장학'))['text']
    _set_fact(answers, family, 'entry_count', '1', {})
    with pytest.raises(HTTPException):
        _check_choice(family, '예', 'entry_0_living_together', answers[family['id']])
    _check_choice(family, 'Y', 'entry_0_living_together', answers[family['id']])
    with pytest.raises(HTTPException):
        _check_choice(qualification, '2023-02-29', 'entry_0_date', answers[qualification['id']])
    with pytest.raises(HTTPException):
        _check_choice(qualification, '4', 'entry_count', answers[qualification['id']])
    _set_fact(answers, qualification, 'entry_count', '0', {})
    assert set(answers[qualification['id']]['facts']) == {'entry_count'}
    assert '외부 장학' not in compose(qualification, answers[qualification['id']], SimpleNamespace(title='장학'))['text']


def test_api_collection_confirm_draft_export_and_revision(auth_client, db, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial['id']))
    selected = fields()
    row.eligibility = {**row.eligibility, 'application_fields':selected, 'form_groups':[], 'form_exclusions':[],
                       'form_coverage':[], 'extraction_issues':[]}
    row.eligibility = {**row.eligibility, 'form_review':{'fingerprint':form_fingerprint(row)}}
    db.commit()
    from app.schemas.schemas import UserProfile
    session = start_session(db, original.user_id, row, UserProfile())
    if session['next_action']['kind'] == 'qualification':
        session = action(auth_client, session, 'messages', {'message':'네, 공고 조건에 해당합니다.'})
    values = [['1','검증용 자격','1급','2024-02-29','검증용 발행기관'],
              ['1','검증용 수상','2024-03-01','검증용 시상기관'],
              ['1','모','검증용 가족','Y','해당 없음','해당 없음']]
    for field, replies in zip(selected, values):
        for value in replies:
            session = action(auth_client, session, 'messages', {'message':value})
        session = action(auth_client, session, f"fields/{field['id']}/confirm", {'confirmed':True})
    session = action(auth_client, session, 'draft')
    action(auth_client, session, 'review', {'confirmed':True}, expected=409)
    session = action(auth_client, session, 'review', {'confirmed':True,'manual_checks_confirmed':True})
    for fmt in ('txt','docx'):
        response = auth_client.get(f"/writing-sessions/{session['id']}/export?format={fmt}")
        assert response.status_code == 200
    session = action(auth_client, session, f"fields/{selected[0]['id']}/entry_count", {'message':'0'}, method='put')
    assert not session['drafts']
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
