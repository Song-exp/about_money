import json

import pytest

from app.services import llm
from app.services.income_evidence import recover_income_evidence
from app.services.fact_validation import validate_fact
from app.services.posting_extract import extract_structured


LINES = ['1. 2026-2 학 기 국 가 장학금 1 차 /2 차 신청자 중 0~3 구 간 해 당 자',
         '※ 국 가 장 학 금 을 반드시 신 청 해야',
         '※ 지 원 구간 심사 기 간 (10 월 16 일 ) 까 지 확 정 된 자 에 한하여 지급:']


def source(lines=LINES):
    text, blocks = '\n'.join(lines), []
    offset = 0
    for i, line in enumerate(lines):
        blocks.append({'text': line, 'start': offset, 'end': offset + len(line),
                       'block_id': f'b{i}', 'location': f'ocr:1:3:1:{i + 1}'})
        offset += len(line) + 1
    return text, blocks


def candidate(value=None, evidence=None):
    return {'eligibility': {'income_bracket': [0, 1, 2, 3] if value is None else value},
            'fact_evidence': {} if evidence is None else {'eligibility.income_bracket': evidence}}


def test_missing_quote_recovers_contiguous_source_and_leaves_model_candidate_unchanged():
    text, blocks = source()
    data = candidate()
    fixed, recoveries = recover_income_evidence(data, text, blocks)
    assert fixed['fact_evidence']['eligibility.income_bracket'] == text
    assert data['fact_evidence'] == {}
    assert recoveries[0]['blocks'] == blocks
    assert validate_fact('eligibility', 'income_bracket', [0, 1, 2, 3], text)


@pytest.mark.parametrize('change', ['wrong_value', 'wrong_type', 'wrong_quote', 'other_page',
                                  'other_paragraph', 'stale_offset', 'missing_context', 'exception',
                                  'alternative', 'second_range', 'reverse_range', 'lower_missing'])
def test_unproven_candidates_are_not_repaired(change):
    lines = list(LINES)
    data = candidate()
    if change == 'wrong_value':
        data = candidate([1, 2, 3])
    elif change == 'wrong_type':
        data = candidate(['0~3구간'])
    elif change == 'wrong_quote':
        data = candidate(evidence='학자금 지원 1~3구간')
    elif change == 'missing_context':
        lines[2] = '일정은 홈페이지에서 확인'
    elif change == 'exception':
        lines[0] += ' 단, 2구간 제외'
    elif change == 'alternative':
        lines[0] += ' 또는 기초생활수급자'
    elif change == 'second_range':
        lines += ['소득 4~6구간 지원']
    elif change == 'reverse_range':
        lines[0] = lines[0].replace('0~3', '3~0')
    elif change == 'lower_missing':
        lines[0] = lines[0].replace('0~3', '3').replace('해 당 자', '이하 해당자')
    text, blocks = source(lines)
    if change == 'other_page':
        blocks[2]['location'] = 'ocr:2:3:1:3'
    elif change == 'other_paragraph':
        blocks[2]['location'] = 'ocr:1:4:1:3'
    elif change == 'stale_offset':
        blocks[1]['start'] += 1
    assert recover_income_evidence(data, text, blocks) == (data, [])


@pytest.mark.parametrize('proof', ['소득 1~3구간 및 5구간', '소득 1~3구간, 4~6구간',
                                  '소득 3~1구간', '소득 0~11구간'])
def test_complex_or_invalid_income_range_is_rejected(proof):
    assert not validate_fact('eligibility', 'income_bracket', [1, 2, 3], proof)


def test_chunk_and_actual_fact_provenance(monkeypatch):
    text, blocks = source()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **kw: json.dumps(candidate()))
    trace = []
    result = extract_structured(text, {'sha256': 'original'}, blocks, trace_sink=trace.append)
    assert result['eligibility']['income_bracket'] == [0, 1, 2, 3]
    fact = result['facts'][0]
    assert fact['evidence'] == text and fact['source']['sha256'] == 'original'
    assert fact['source']['location'] == blocks[0]['location']
    assert len(fact['evidence_recovery']['blocks']) == 3
    assert trace[0]['candidate']['fact_evidence'] == {}
    assert len(trace[0]['fact_evidence_recoveries']) == 1


def test_supporting_blocks_outside_chunk_cannot_supply_context():
    text, blocks = source()
    short = text[:blocks[1]['end']]
    assert recover_income_evidence(candidate(), short, blocks) == (candidate(), [])


def test_direct_income_label_can_recover_without_ocr_context():
    text, blocks = source(['학자금 지원 0~3구간'])
    blocks[0]['location'] = 'table:1/row:1/cell:1'
    assert recover_income_evidence(candidate(), text, blocks)[0]['fact_evidence']['eligibility.income_bracket'] == text


def test_unrepresented_content_between_blocks_is_not_borrowed():
    lines = [LINES[0], '다른 신청 사업의 설명', *LINES[1:]]
    text, blocks = source(lines)
    selected = [blocks[0], *blocks[2:]]
    assert recover_income_evidence(candidate(), text, selected) == (candidate(), [])
