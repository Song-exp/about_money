from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_parser import parse_document
from app.services.form_spreadsheet_recommendation import native_recommendation, audit_recommendation, SOURCE_SHA, VERSION
from app.services.posting_extract import extract_structured
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_drafts import compose
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.models.scholarship import PostingStatus


def fixture():
    raw = Path('reports/scholarship-improvement-20261011-v36-actual/notice-new-555351/asset-2-280961688897.bin').read_bytes()
    return parse_document(raw, 'form.xlsx')


def test_original_student_components_recommender_and_manual_destinations(monkeypatch):
    p = fixture()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: json.dumps({'application_fields': []}))
    result = extract_structured(p['extracted_text'], {'sha256': SOURCE_SHA}, p['blocks'], spreadsheet_inventory=p['spreadsheet_inventory'])
    fields = result['application_fields']
    native = [g['field'] for g in native_recommendation(p['blocks'], {'sha256': SOURCE_SHA}, p['spreadsheet_inventory'])[0]]
    assert len(native) == 11 and {f['id'] for f in fields} == {f['id'] for f in native}
    assert result['coverage']['native_spreadsheet_recommendation_audit']['unresolved'] == 0
    inputs = [f for f in native if f.get('input_spec')]
    assert sum(len(f['input_spec']['slots']) for f in inputs) == 12
    assert len([f for f in inputs if f['role'] == 'student']) == 6
    assert len([f for f in inputs if f['role'] == 'recommender']) == 2
    manual = [f for f in native if f.get('manual_spec')]
    assert len(manual) == 3 and all(requirements(f) == [] for f in manual)
    reason = next(f for f in manual if f['label'] == '추천사유')
    assert len(reason['original_destination']['writing_region']) == 55
    assert not any(f['label'].strip() == '`' for f in fields)
    answers = {}
    for f in inputs:
        for slot in f['input_spec']['slots']:
            value = {'birth_date': '2003-05-04', 'age': '23', 'grade': '3'}.get(slot['key'], '검증용 ' + slot['label'])
            _set_fact(answers, f, slot['key'], value, {})
        draft = compose(f, answers[f['id']], SimpleNamespace(title='장학'))
        assert 'original_paragraph_layout_check_required' in draft['validation']['warnings']
        assert '추천합니다' not in draft['text'] and '(인)' not in draft['text']
    for f in manual:
        with pytest.raises(HTTPException):
            _set_fact(answers, f, 'value', '추천합니다', {})
    birth = next(f for f in inputs if f['label'] == '생년월일')
    with pytest.raises(HTTPException):
        _check_choice(birth, '2023-02-29', 'birth_date', answers[birth['id']])
    with pytest.raises(HTTPException):
        _check_choice(birth, '2999-01-01', 'birth_date', answers[birth['id']])


@pytest.mark.parametrize('problem', ['no_inventory', 'hash', 'hidden', 'formula', 'span', 'template', 'blank_filled', 'print_area', 'duplicate', 'lost_field', 'wrong_role', 'signature_automated'])
def test_unsafe_binding_blocks_writing(problem):
    p = fixture()
    inventory = deepcopy(p['spreadsheet_inventory'])
    if problem == 'no_inventory':
        inventory = None
    elif problem == 'hash':
        inventory['source_sha256'] = 'wrong'
    else:
        sheet = inventory['sheets'][0]
        area = next(a for a in sheet['areas'] if a['row'] == 6 and a['column'] == 1)
        if problem == 'hidden':
            area['hidden'] = True
        elif problem == 'formula':
            area['formula'] = True
        elif problem == 'span':
            area['column_span'] = 3
        elif problem == 'template':
            area['value'] = '바뀐 틀'
        elif problem == 'blank_filled':
            next(a for a in sheet['areas'] if a['row'] == 7 and a['column'] == 1).update(blank=False, value='이미 작성')
        elif problem == 'print_area':
            sheet['print_area'] = ''
        elif problem == 'duplicate':
            sheet['areas'].append(deepcopy(area))
    groups, _ = native_recommendation(p['blocks'], {'sha256': SOURCE_SHA}, inventory)
    fields = [g['field'] for g in groups]
    if problem == 'lost_field':
        fields.pop()
    elif problem == 'wrong_role':
        fields[-4]['role'] = 'student'
    elif problem == 'signature_automated':
        next(f for f in fields if f['field_type'] == 'signature')['manual_spec'] = {}
    audit = audit_recommendation(p['blocks'], fields, {'sha256': SOURCE_SHA}, inventory)
    assert audit['unresolved']
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash='test', eligibility={
        'application_fields': fields, 'form_coverage': [{'native_spreadsheet_recommendation_audit': audit}],
        'extraction_issues': [], 'validation_version': VALIDATION_VERSION})
    row.eligibility['form_review'] = {'fingerprint': form_fingerprint(row)}
    assert not readiness(row)['writing_ready']
