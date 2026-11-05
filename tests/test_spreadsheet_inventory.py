import hashlib
import io
from pathlib import Path

import openpyxl
import pytest

from app.services.document_parser import parse_document
from app.services.spreadsheet_inventory import spreadsheet_inventory
from app.utils.file_parser import UnsupportedFileType


def workbook_bytes():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = '신청서'
    sheet['A1'] = '가족사항'
    sheet.merge_cells('B2:D2')
    sheet['A2'] = '성명'
    sheet['B4'] = '=1+2'
    sheet.row_dimensions[2].hidden = True
    sheet.print_area = 'A1:D4'
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_blank_merged_area_and_formula_remain_source_records():
    content = workbook_bytes()
    inventory = spreadsheet_inventory(content, '.xlsx')
    assert inventory['source_sha256'] == hashlib.sha256(content).hexdigest()
    sheet = inventory['sheets'][0]
    blank = next(c for c in sheet['areas'] if (c['row'], c['column']) == (1, 1))
    assert blank['blank'] and blank['merged'] and blank['column_span'] == 3 and blank['hidden']
    assert not any(c['row'] == 1 and c['column'] in (2, 3) for c in sheet['areas'])
    formula = next(c for c in sheet['areas'] if c['formula'])
    assert formula['value'] == '=1+2'
    assert not inventory['input_semantics_verified'] and not inventory['human_review_complete']


def test_production_parse_preserves_inventory_without_inventing_blank_text_blocks():
    result = parse_document(workbook_bytes(), 'form.xlsx')
    assert result['parser_version'] == 'blocks-v5'
    assert result['spreadsheet_inventory']['sheets'][0]['merged_ranges'] == [(1, 2, 1, 4)]
    assert not any(b['row'] == 1 and b['column'] == 1 for b in result['blocks'])
    assert 'spreadsheet_formula_requires_review' in {i['error'] for i in result['issues']}


@pytest.mark.parametrize('suffix,sha,merges', [
    ('.xlsx', '2809616888977fb231041f69265e904a42cbf511794d1b0799f0deb5751f0b1f', 12),
    ('.xls', 'a0167c494ee640b8b408a72c815e30497da0c690496097b0f4200a0d7e5fae5a', 129),
])
def test_original_workbook_has_blank_geometry_and_raw_source_hash(suffix, sha, merges):
    root = Path('reports/scholarship-improvement-20261011-v36-actual/notice-new-555351')
    content = next(root.glob(f'asset-*-{sha[:12]}.bin')).read_bytes()
    inventory = spreadsheet_inventory(content, suffix)
    assert inventory['source_sha256'] == sha
    assert any(a['merged'] and a['blank'] for a in inventory['sheets'][0]['areas'])
    assert len(inventory['sheets'][0]['merged_ranges']) == merges
    if suffix == '.xls':
        assert inventory['requires_object_review']
    assert not inventory['human_review_complete']


def test_sparse_workbook_cannot_expand_past_limit():
    workbook = openpyxl.Workbook()
    workbook.active['A200001'] = 'large'
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    with pytest.raises(UnsupportedFileType, match='cell_limit'):
        spreadsheet_inventory(stream.getvalue(), '.xlsx')
