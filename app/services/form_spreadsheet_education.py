"""Source-bound education rows; printed templates are never applicant facts."""
import re
from datetime import date

from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source
from app.services.form_spreadsheet_repeat import SOURCE_SHA

VERSION = 'spreadsheet-education-v1'
SCHOOLS = ['초등학교', '중학교', '고등학교', '대학', '대학교']
STATUS = ['', '', '재학/중퇴', '졸업/재학/중퇴', '재학/중퇴/휴학']
COLUMNS = [(3, 5, '졸업 년월', 'graduation', 'education_month'),
           (8, 8, '학교명', 'school', 'education_school'),
           (16, 5, '전공', 'major', 'text'),
           (21, 7, '구분', 'status', 'education_status'),
           (28, 6, '과외(동아리)활동', 'activities', 'text')]


def native_education(blocks, source, inventory):
    labels = [b for b in blocks if b.get('sheet') == '신상카드양식' and compact(b['text']) == '학력사항']
    audit = {'version': VERSION, 'controls': [], 'unresolved': 0,
             'scope': 'five_hash_bound_songhwa_education_rows_not_whole_form'}
    if not labels:
        if (source or {}).get('sha256') == SOURCE_SHA:
            audit['controls'] = [{'source': source, 'status': 'requires_original_review',
                                  'issues': ['spreadsheet_education_label_missing']}]
            audit['unresolved'] = 1
        return [], audit
    label = labels[0]
    sheets = [s for s in (inventory or {}).get('sheets', []) if s['name'] == '신상카드양식']
    areas = sheets[0].get('areas', []) if sheets else []
    cells = {(a['row'], a['column']): a for a in areas}
    valid = (len(labels) == len(sheets) == 1 and not sheets[0].get('hidden')
             and len(cells) == len(areas) and label.get('row') == 12 and label.get('column') == 1
             and label.get('method') == 'native'
             and (inventory or {}).get('version') == 'spreadsheet-source-inventory-v1'
             and (inventory or {}).get('source_sha256') == (source or {}).get('sha256') == SOURCE_SHA)
    used, headers, rows = [label], {}, []

    def verified_cell(row, col, span, value, row_span=1):
        nonlocal valid
        area = cells.get((row, col), {})
        overlaps = [a for a in areas if a['row'] < row + row_span and row < a['row'] + a['row_span']
                    and a['column'] < col + span and col < a['column'] + a['column_span']]
        ok = (area.get('value') == value and area.get('blank') == (value == '')
              and area.get('row_span') == row_span and area.get('column_span') == span
              and not area.get('hidden') and not area.get('formula') and len(overlaps) == 1)
        if value:
            matches = [b for b in blocks if b.get('sheet') == '신상카드양식'
                       and b.get('row') == row and b.get('column') == col]
            ok = ok and len(matches) == 1 and matches[0].get('method') == 'native' and matches[0]['text'] == value
            if len(matches) == 1:
                used.append(matches[0])
        valid = valid and ok
        return {**(source or {}), **area}

    verified_cell(12, 1, 2, label['text'], 6)
    for col, span, title, key, kind in COLUMNS:
        verified_cell(12, col, span, title)
        header = next((b for b in used if b.get('row') == 12 and b.get('column') == col), None)
        headers[key] = block_source(header, source) if header else {}
    for i, school in enumerate(SCHOOLS):
        values = ['년     월', school, '', STATUS[i], '']
        row_slots = []
        for (col, span, title, key, kind), value in zip(COLUMNS, values):
            destination = verified_cell(13 + i, col, span, value)
            row_slots.append({'key': f'education_{i}_{key}', 'label': f'{school} 행 · {title}',
                              'kind': kind, 'unit': '', 'source': headers[key], 'destination': destination,
                              'template': value, 'original_options': value.split('/') if key == 'status' and value else []})
        rows.append({'school_template': school, 'presence_key': f'education_{i}_present', 'slots': row_slots})
    audit['controls'] = [{'source': block_source(label, source),
                          'status': 'bound' if valid else 'requires_original_review',
                          'issues': [] if valid else ['spreadsheet_education_geometry_missing_or_conflicting']}]
    audit['unresolved'] = int(not valid)
    if not valid:
        return [], audit
    field = _field(label, source, label['location'])
    field['field_type'] = 'table'
    field['input_spec'] = {'version': VERSION, 'slots': [], 'education_rows': rows,
                           'original_layout_review_required': True}
    field['instruction_sources'] = [block_source(b, source) for b in used]
    field['instructions'] = '\n'.join(b['text'] for b in used)
    return [{'field': field, 'locations': {b['location'] for b in used}}], audit


def education_slots(field, answer=None):
    slots = []
    facts = (answer or {}).get('facts', {})
    for row in field['input_spec']['education_rows']:
        slots.append({'key': row['presence_key'], 'label': f"{row['school_template']} 행 작성 여부",
                      'kind': 'education_presence', 'unit': '', 'service_only': True,
                      'source': field['source'], 'options': ['작성함', '작성하지 않음']})
        if facts.get(row['presence_key'], {}).get('text') == '작성함':
            slots.extend(row['slots'])
    return slots


def check_education(slot, text):
    kind = slot['kind']
    if kind == 'education_presence' and text not in slot['options']:
        raise ValueError('choose whether to write this physical row')
    if kind == 'education_month' and text not in ('졸업 전', '표시하지 않음'):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', text):
            raise ValueError('report actual graduation year-month or graduation pending')
        date.fromisoformat(text + '-01')
    if kind == 'education_school' and (compact(text) == compact(slot['template']) or text in ('해당 없음', '표시하지 않음')):
        raise ValueError('report the actual school name, not the printed school template')
    if kind == 'education_status' and slot['original_options'] and text not in slot['original_options'] + ['표시하지 않음']:
        raise ValueError('choose a printed status or explicitly leave the original mark blank')


def clear_education_dependents(field, item, key):
    if (field.get('input_spec') or {}).get('version') != VERSION:
        return
    for row in field['input_spec']['education_rows']:
        if key == row['presence_key']:
            for slot in row['slots']:
                item['facts'].pop(slot['key'], None)


def audit_education(blocks, fields, source, inventory):
    groups, audit = native_education(blocks, source, inventory)
    for group in groups:
        target = group['field']
        actual = [f for f in fields if f['id'] == target['id']]
        if len(actual) != 1 or any(actual[0].get(k) != target.get(k) for k in ('source', 'input_spec', 'field_type')):
            audit['controls'][0].update(status='requires_original_review', issues=['spreadsheet_education_field_missing_or_conflicting'])
    audit['unresolved'] = sum(bool(c['issues']) for c in audit['controls'])
    return audit
