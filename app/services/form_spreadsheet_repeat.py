"""Bind three proven Songhwa blank repeating grids to conversation facts."""
from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = 'spreadsheet-repeating-v1'
SOURCE_SHA = 'a0167c494ee640b8b408a72c815e30497da0c690496097b0f4200a0d7e5fae5a'
DEFINITIONS = [
    ('자격사항', 19, 3, [(3,13,'자격[어학]명','name','text'), (16,5,'등급[점수]','level','text'),
                          (21,7,'취득 년.월.일','date','date'), (28,6,'발행[시행]기관','issuer','text')]),
    ('수상내역', 24, 3, [(3,11,'수상명','name','text'), (14,9,'수상일자','date','date'),
                          (23,11,'시상기관','issuer','text')]),
    ('가족사항', 32, 5, [(3,4,'관계','relation','text'), (7,8,'성명','name','text'),
                          (15,5,'동거여부[Y/N]','living_together','sheet_enum'),
                          (20,9,'특이사항','notes','text'), (29,5,'비고','remarks','text')]),
]


def native_spreadsheet_repeats(blocks, source, inventory):
    groups, checks = [], []
    for title, row, count, definition in DEFINITIONS:
        labels = [b for b in blocks if b.get('sheet') == '신상카드양식' and compact(b['text']) == title]
        if not labels:
            continue
        label = labels[0]
        valid = (len(labels) == 1 and inventory and inventory.get('source_sha256') == (source or {}).get('sha256') == SOURCE_SHA
                 and inventory.get('version') == 'spreadsheet-source-inventory-v1')
        sheets = [s for s in (inventory or {}).get('sheets', []) if s['name'] == '신상카드양식']
        valid = valid and len(sheets) == 1 and not sheets[0].get('hidden')
        sheet = sheets[0] if sheets else {}
        areas = sheet.get('areas', [])
        cells = {(a['row'], a['column']): a for a in areas}
        valid = valid and len(cells) == len(areas) and label.get('method') == 'native' and label.get('row') == row
        columns, used, templates = [], [label], []
        for col, span, text, key, kind in definition:
            headers = [b for b in blocks if b.get('sheet') == '신상카드양식' and b.get('row') == row and b.get('column') == col]
            area = cells.get((row, col), {})
            if (len(headers) != 1 or headers[0].get('method') != 'native' or compact(headers[0]['text']) != compact(text)
                    or area.get('value') != text or area.get('hidden') or area.get('formula')
                    or area.get('row_span') != 1 or area.get('column_span') != span):
                valid = False
                continue
            header = headers[0]
            columns.append({'key': key, 'label': text, 'kind': kind, 'unit': '', 'source': block_source(header, source),
                            **({'options': ['Y', 'N']} if kind == 'sheet_enum' else {})})
            used.append(header)
        for target_row in range(row + 1, row + count + 1):
            destinations = []
            for col, span, text, key, kind in definition:
                cell = cells.get((target_row, col), {})
                overlaps = [a for a in areas if a['row'] <= target_row < a['row'] + a['row_span']
                            and a['column'] < col + span and col < a['column'] + a['column_span']]
                if (not cell.get('blank') or cell.get('hidden') or cell.get('formula')
                        or cell.get('row_span') != 1 or cell.get('column_span') != span or len(overlaps) != 1):
                    valid = False
                destinations.append({**(source or {}), **cell, 'column_key': key})
            templates.append({'cells': destinations})
        checks.append({'source': block_source(label, source), 'status': 'bound' if valid else 'requires_original_review',
                       'issues': [] if valid else ['spreadsheet_repeat_geometry_missing_or_conflicting']})
        if not valid:
            continue
        field = _field(label, source, label['location'])
        field['field_type'] = 'table'
        field['input_spec'] = {'version': VERSION, 'slots': [{'key': 'entry_count', 'label': '작성할 내역 수',
                'kind': 'row_count', 'unit': '', 'source': block_source(label, source)}],
            'repeat': {'count_key': 'entry_count', 'columns': columns, 'templates': templates,
                       'collection_limit': count, 'fixed_physical_capacity': True, 'original_layout_review_required': True}}
        field['instruction_sources'] = [block_source(b, source) for b in used]
        field['instructions'] = '\n'.join(b['text'] for b in used)
        groups.append({'field': field, 'locations': {b['location'] for b in used}})
    return groups, {'version': VERSION, 'controls': checks, 'unresolved': sum(bool(c['issues']) for c in checks),
                    'scope': 'three_known_songhwa_grids_not_whole_spreadsheet'}


def repeat_slots(field, answer=None):
    spec = field['input_spec']
    slots = list(spec['slots'])
    count = (answer or {}).get('facts', {}).get('entry_count', {}).get('text', '')
    if count.isascii() and count.isdigit() and str(int(count)) == count and int(count) <= spec['repeat']['collection_limit']:
        for row in range(int(count)):
            cells = {c['column_key']: c for c in spec['repeat']['templates'][row]['cells']}
            for column in spec['repeat']['columns']:
                slots.append({**column, 'key': f"entry_{row}_{column['key']}",
                              'label': f"{row+1}번째 내역 · {column['label']}", 'row_index': row,
                              'column_key': column['key'], 'destination': cells[column['key']]})
    return slots


def audit_spreadsheet_repeats(blocks, fields, source, inventory):
    groups, audit = native_spreadsheet_repeats(blocks, source, inventory)
    for group in groups:
        expected = group['field']
        matches = [f for f in fields if f['id'] == expected['id']]
        if len(matches) != 1 or any(matches[0].get(k) != expected.get(k) for k in ('source', 'input_spec', 'field_type')):
            control = next(c for c in audit['controls'] if c['source']['location'] == expected['source']['location'])
            control.update(status='requires_original_review', issues=['spreadsheet_repeat_field_missing_or_conflicting'])
    audit['unresolved'] = sum(bool(c['issues']) for c in audit['controls'])
    return audit
