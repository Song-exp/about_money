"""Recover missing income quotes only from bounded, contiguous source blocks."""
import re

from app.services.fact_validation import validate_fact, _readable


def recover_income_evidence(data, chunk, blocks, chunk_start=0):
    eligibility = data.get('eligibility')
    if not isinstance(eligibility, dict):
        return data, []
    value = eligibility.get('income_bracket')
    evidence_map = data.get('fact_evidence') or {}
    if not isinstance(evidence_map, dict) or evidence_map.get('eligibility.income_bracket') not in (None, ''):
        return data, []
    if not isinstance(value, list) or not value or any(type(v) is not int or not 0 <= v <= 10 for v in value):
        return data, []
    candidates = []
    for index, block in enumerate(blocks):
        text = block.get('text', '')
        if not re.search(r'\d+\s*(?:분위|구간)?\s*[~～\-]\s*\d+\s*(?:분위|구간)', _readable(text)):
            continue
        windows = [[block]]
        # OCR 문단 내 바로 뒤의 두 줄만 문맥으로 사용할 수 있다.
        location = block.get('location', '')
        if location.startswith('ocr:'):
            prefix = location.rsplit(':', 1)[0]
            following = blocks[index + 1:index + 3]
            if following and all(b.get('location', '').rsplit(':', 1)[0] == prefix for b in following):
                windows.append([block, *following])
        for window in windows:
            begin, end = window[0].get('start'), window[-1].get('end')
            if not isinstance(begin, int) or not isinstance(end, int) or not chunk_start <= begin < end <= chunk_start + len(chunk):
                continue
            evidence = chunk[begin - chunk_start:end - chunk_start]
            # 위치가 틀렸거나 다른 내용을 끼워 넣은 블록은 사용하지 않는다.
            if any(not isinstance(b.get('start'), int) or not isinstance(b.get('end'), int)
                   or not begin <= b['start'] < b['end'] <= end
                   or chunk[b['start'] - chunk_start:b['end'] - chunk_start] != b.get('text') for b in window):
                continue
            if any(a['end'] > b['start'] or chunk[a['end'] - chunk_start:b['start'] - chunk_start].strip()
                   for a, b in zip(window, window[1:])):
                continue
            if len(evidence) > 700 or not validate_fact('eligibility', 'income_bracket', value, evidence):
                continue
            candidates.append({'evidence': evidence, 'blocks': [
                {k: b.get(k) for k in ('block_id', 'location', 'start', 'end', 'text')} for b in window]})
            break
    # 여러 범위가 발견되면 모델값에 맞는 하나만 골라 채택하지 않는다.
    range_blocks = [b for b in blocks if re.search(r'\d+\s*(?:분위|구간)?\s*[~～\-]\s*\d+\s*(?:분위|구간)', _readable(b.get('text', '')))]
    if len(candidates) != 1 or len(range_blocks) != 1:
        return data, []
    recovery = {'version': 'income-evidence-recovery-v1', 'path': 'eligibility.income_bracket',
                'value': value, 'reason': 'missing_model_evidence', **candidates[0]}
    return {**data, 'fact_evidence': {**evidence_map, recovery['path']: recovery['evidence']}}, [recovery]
