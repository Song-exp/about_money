"""Bind the verified Songhwa recommendation sheet without inventing a signature."""
from datetime import date
import re

from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = 'spreadsheet-recommendation-v1'
SOURCE_SHA = '2809616888977fb231041f69265e904a42cbf511794d1b0799f0deb5751f0b1f'
SHEET = '추천서양식'


def native_recommendation(blocks, source, inventory):
    scoped = [b for b in blocks if b.get('sheet') == SHEET]
    audit = {'version': VERSION, 'controls': [], 'unresolved': 0, 'scope': 'one_hash_bound_recommendation_sheet_not_generic_excel'}
    if not scoped and (source or {}).get('sha256') != SOURCE_SHA:
        return [], audit
    sheets = [s for s in (inventory or {}).get('sheets', []) if s['name'] == SHEET]
    sheet = sheets[0] if sheets else {}
    areas = sheet.get('areas', [])
    cells = {(a['row'], a['column']): a for a in areas}
    texts = {(b.get('row'), b.get('column')): b for b in scoped}
    valid = (len(sheets) == 1 and not sheet.get('hidden') and len(cells) == len(areas)
             and len(texts) == len(scoped) and all(b.get('method') == 'native' for b in scoped)
             and (inventory or {}).get('version') == 'spreadsheet-source-inventory-v1'
             and (inventory or {}).get('source_sha256') == (source or {}).get('sha256') == SOURCE_SHA
             and sheet.get('print_area') == "'추천서양식'!$A$1:$F$28")

    def cell(row, col, span, expected=None, row_span=1):
        nonlocal valid
        a = cells.get((row, col), {})
        overlaps = [c for c in areas if c['row'] < row + row_span and row < c['row'] + c['row_span']
                    and c['column'] < col + span and col < c['column'] + c['column_span']]
        ok = (a.get('row_span') == row_span and a.get('column_span') == span
              and not a.get('hidden') and not a.get('formula') and len(overlaps) == 1)
        if expected is None:
            ok = ok and a.get('blank') and not str(a.get('value') or '').strip()
        else:
            b = texts.get((row, col), {})
            ok = ok and a.get('value') == expected and b.get('text') == expected
        valid = valid and ok
        return {**(source or {}), **a}

    cell(0, 0, 6, '장학생 추천서', 2)
    cell(19, 0, 1, '  위 학생을 상기 추천사유에 의거 귀 재단의 장학생으로 추천합니다.')
    cell(21, 0, 1, '추천인')
    cell(25, 0, 6, '재단법인 송화(松和)재단 귀중', 2)
    # The backtick outside the explicitly defined print area is not an input.
    cell(11, 10, 1, '`')
    groups = []

    def bind(row, col, span, label, role, components):
        cell(row, col, span, label)
        b = texts.get((row, col))
        if not b:
            return
        slots = []
        for key, title, kind, target_row, target_col, target_span, template in components:
            dest = cell(target_row, target_col, target_span, template)
            proof = block_source(texts[(target_row, target_col)], source) if template is not None and (target_row, target_col) in texts else block_source(b, source)
            slots.append({'key': key, 'label': title, 'kind': kind, 'unit': '', 'source': proof, 'destination': dest,
                          'template': template, 'role': role})
        f = _field(b, source, f'{b["location"]}/components')
        f.update(role=role, field_type='table', input_spec={'version': VERSION, 'slots': slots, 'original_layout_review_required': True},
                 role_sources=[block_source(texts[(21, 0) if role == 'recommender' else (19, 0)], source)] if ((21, 0) if role == 'recommender' else (19, 0)) in texts else [])
        groups.append({'field': f, 'locations': set()})

    bind(4, 0, 1, '성명', 'student', [('korean_name', '한글 성명', 'text', 4, 1, 2, '(한글)'), ('chinese_name', '한자 성명', 'text', 4, 3, 3, '(한자)')])
    bind(5, 0, 1, '생년월일', 'student', [('birth_date', '생년월일', 'reported_birth_date', 5, 1, 2, '                                   (만)      세'),
                                      ('age', '만 나이', 'nonnegative_integer', 5, 1, 2, '                                   (만)      세')])
    bind(5, 3, 1, '성  별', 'student', [('gender', '성별', 'text', 5, 4, 2, None)])
    bind(6, 0, 1, '학 교 명', 'student', [(k, t, kind, 6, 1, 2, '                     학교          과   학년') for k, t, kind in
                                         [('school', '학교명', 'text'), ('department', '학과', 'text'), ('grade', '학년', 'positive_integer')]])
    bind(6, 3, 1, '연락처', 'student', [('phone', '학생 연락처', 'text', 6, 4, 2, None)])
    bind(7, 0, 1, '주    소', 'student', [('address', '학생 주소', 'text', 7, 1, 5, None)])
    bind(21, 1, 1, '소속', 'recommender', [('affiliation', '추천인 소속', 'text', 21, 2, 1, None)])
    bind(21, 3, 2, '성명', 'recommender', [('name', '추천인 성명', 'text', 21, 5, 1, '                                (인)')])
    for row, col, span, label, kind, action in [
        (8, 0, 1, '추천사유', 'reason', '추천사유는 추천인이 학생의 실제 사실을 확인하여 원본에서 작성해야 합니다. 학생 답변만으로 추천인의 판단이나 추천 의사를 만들지 않습니다.'),
        (21, 5, 1, '                                (인)', 'signature', '추천인이 원본의 성명과 날인 위치를 확인하고 직접 서명하거나 날인하세요. 채팅 응답을 실제 서명으로 대신하지 않습니다.'),
        (23, 0, 6, '20      년        월       일', 'date', '추천인이 실제 작성일을 확인하고 원본에 직접 작성하세요. 현재 날짜를 자동으로 채우지 않습니다.')]:
        dest = cell(row, col, span, label, 11 if kind == 'reason' else 1)
        b = texts.get((row, col))
        if not b:
            continue
        f = _field(b, source, b['location'] + '/original-only')
        f.update(role='recommender', field_type='signature' if kind == 'signature' else 'text',
                 manual_spec={'version': VERSION, 'mode': 'original_only', 'action': action, 'sources': [block_source(b, source)]},
                 original_destination={'version': VERSION, 'kind': kind, 'source': dest, 'visual_placement_verified': False},
                 instructions=label + '\n' + action)
        if kind == 'reason':
            region = [cell(r, c, 1) for r in range(8, 19) for c in range(1, 6)]
            f['original_destination']['writing_region'] = region
        groups.append({'field': f, 'locations': set()})
    control = {'source': block_source(scoped[0], source) if scoped else source,
               'status': 'bound' if valid and len(groups) == 11 else 'requires_original_review',
               'issues': [] if valid and len(groups) == 11 else ['spreadsheet_recommendation_geometry_missing_or_conflicting']}
    audit.update(controls=[control], unresolved=int(bool(control['issues'])))
    if audit['unresolved']:
        return [], audit
    for g in groups:
        g['locations'] = {b['location'] for b in scoped}
    return groups, audit


def check_recommendation(slot, text):
    if slot['kind'] == 'reported_birth_date':
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', text):
            raise ValueError('report the actual birth date')
        if date.fromisoformat(text) > date.today():
            raise ValueError('birth date cannot be in the future')
    if slot.get('template') and compact(text) == compact(slot['template']):
        raise ValueError('report a fact rather than the printed template')


def audit_recommendation(blocks, fields, source, inventory):
    groups, audit = native_recommendation(blocks, source, inventory)
    for g in groups:
        target = g['field']
        actual = [f for f in fields if f['id'] == target['id']]
        if len(actual) != 1 or any(actual[0].get(k) != target.get(k) for k in ('source', 'input_spec', 'role', 'field_type', 'manual_spec', 'original_destination')):
            audit['controls'][0].update(status='requires_original_review', issues=['spreadsheet_recommendation_binding_missing_or_conflicting'])
    audit['unresolved'] = sum(bool(c['issues']) for c in audit['controls'])
    return audit
