"""Source-only cell geometry inventory. Blank areas are not declared user inputs."""
import hashlib
import io
import zipfile

from app.services.spreadsheet_parser import MAX_CELLS, biff_objects
from app.utils.file_parser import _guard_zip_size, UnsupportedFileType


def _value(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _areas(sheet, rows, columns, merged, get):
    if any(not (0 <= r0 < r1 <= rows and 0 <= c0 < c1 <= columns) for r0, r1, c0, c1 in merged):
        raise UnsupportedFileType('spreadsheet_invalid_merged_bounds')
    anchors = {(r0, c0): (r1, c1) for r0, r1, c0, c1 in merged}
    covered = {(r, c) for r0, r1, c0, c1 in merged for r in range(r0, r1) for c in range(c0, c1)
               if (r, c) != (r0, c0)}
    result = []
    for row in range(rows):
        for col in range(columns):
            if (row, col) in covered:
                continue
            value, styled, formula, hidden = get(row, col)
            if (row, col) not in anchors and value in (None, '') and not styled:
                continue
            end_row, end_col = anchors.get((row, col), (row + 1, col + 1))
            result.append({'location': f'sheet:{sheet}/row:{row + 1}/cell:{col + 1}',
                           'row': row, 'column': col, 'row_span': end_row - row,
                           'column_span': end_col - col, 'merged': (row, col) in anchors,
                           'value': _value(value), 'blank': value is None or not str(value).strip(),
                           'styled': styled, 'formula': formula, 'hidden': hidden})
    return result


def spreadsheet_inventory(content, extension):
    """Read bounded raw geometry independently of model fields; never execute formulas."""
    result = {'version': 'spreadsheet-source-inventory-v1', 'source_sha256': hashlib.sha256(content).hexdigest(),
              'sheets': [], 'objects': {}, 'input_semantics_verified': False, 'human_review_complete': False}
    if extension == '.xlsx':
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _guard_zip_size(archive)
            result['objects'] = {'drawing_parts': [n for n in archive.namelist() if n.startswith(('xl/media/', 'xl/drawings/'))],
                                 'macro_parts': [n for n in archive.namelist() if n.lower().endswith('vbaproject.bin')]}
        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=False, keep_links=False)
        try:
            if sum(s.max_row * s.max_column for s in workbook) > MAX_CELLS:
                raise UnsupportedFileType('spreadsheet_cell_limit_exceeded')
            for sheet in workbook:
                merged = [(r.min_row - 1, r.max_row, r.min_col - 1, r.max_col) for r in sheet.merged_cells.ranges]
                def get(row, col):
                    cell = sheet.cell(row + 1, col + 1)
                    return cell.value, cell.has_style, cell.data_type == 'f', bool(
                        sheet.sheet_state != 'visible' or sheet.row_dimensions[row + 1].hidden
                        or sheet.column_dimensions[cell.column_letter].hidden)
                result['sheets'].append({'name': sheet.title, 'rows': sheet.max_row, 'columns': sheet.max_column,
                    'hidden': sheet.sheet_state != 'visible', 'print_area': str(sheet.print_area),
                    'merged_ranges': sorted(merged), 'areas': _areas(sheet.title, sheet.max_row, sheet.max_column, merged, get)})
        finally:
            workbook.close()
    elif extension == '.xls':
        import xlrd
        workbook = xlrd.open_workbook(file_contents=content, formatting_info=True)
        try:
            if sum(s.nrows * s.ncols for s in workbook.sheets()) > MAX_CELLS:
                raise UnsupportedFileType('spreadsheet_cell_limit_exceeded')
            for sheet in workbook.sheets():
                merged = sheet.merged_cells
                def get(row, col):
                    cell = sheet.cell(row, col)
                    # xlrd does not identify all formula cells: retain uncertainty explicitly.
                    return cell.value, cell.xf_index not in (None, 0), None, bool(
                        getattr(sheet, 'visibility', 0) or getattr(sheet.rowinfo_map.get(row), 'hidden', False)
                        or getattr(sheet.colinfo_map.get(col), 'hidden', False))
                result['sheets'].append({'name': sheet.name, 'rows': sheet.nrows, 'columns': sheet.ncols,
                    'hidden': bool(getattr(sheet, 'visibility', 0)), 'print_area': None,
                    'merged_ranges': sorted(merged), 'areas': _areas(sheet.name, sheet.nrows, sheet.ncols, merged, get)})
        finally:
            workbook.release_resources()
        result['objects'] = biff_objects(content)
        result['requires_object_review'] = True
    else:
        raise UnsupportedFileType('unsupported_spreadsheet_inventory_format')
    return result
