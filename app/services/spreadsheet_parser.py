"""공식 XLSX/XLS 양식의 시트·셀·병합 위치를 추출한다. 수식은 실행하지 않는다."""
import io
import zipfile
import struct
from collections import Counter

from app.utils.file_parser import _guard_zip_size, UnsupportedFileType

MAX_CELLS = 200_000


def biff_objects(content):
    """Inventory unsupported objects/formulas without executing or declaring them restored."""
    import olefile
    names = {0x0006: "formula", 0x005D: "object", 0x00EC: "drawing", 0x00EB: "drawing_group",
             0x01B6: "text_object", 0x001C: "note"}
    counts = Counter()
    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        entry = "Workbook" if ole.exists("Workbook") else "Book"
        data = ole.openstream(entry).read()
    position = 0
    while position < len(data):
        if len(data) - position < 4:
            if not any(data[position:]):
                break
            raise ValueError("truncated_biff_record")
        tag, size = struct.unpack_from("<HH", data, position)
        position += 4
        if position + size > len(data):
            raise ValueError("truncated_biff_record")
        if tag in names:
            counts[names[tag]] += 1
        position += size
    return dict(counts)


def _cell(sheet, row, column, value, spans):
    if value is None or not str(value).strip():
        return None
    row_span, column_span = spans.get((row, column), (1, 1))
    return {"text": str(value), "location": f"sheet:{sheet}/row:{row + 1}/cell:{column + 1}",
            "sheet": sheet, "table": f"sheet:{sheet}", "row": row, "column": column,
            "row_span": row_span, "column_span": column_span, "kind": "cell", "method": "native"}


def spreadsheet_blocks(content, extension):
    blocks, issues = [], []
    if extension == ".xlsx":
        import openpyxl
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _guard_zip_size(archive)
            if any(n.startswith(("xl/media/", "xl/drawings/")) for n in archive.namelist()):
                issues.append({"location": "workbook", "error": "spreadsheet_drawings_require_review"})
            if any(n.lower().endswith("vbaproject.bin") for n in archive.namelist()):
                issues.append({"location": "workbook", "error": "spreadsheet_macros_not_executed"})
        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=False, keep_links=False)
        try:
            if sum(s.max_row * s.max_column for s in workbook) > MAX_CELLS:
                raise UnsupportedFileType("spreadsheet_cell_limit_exceeded")
            for sheet in workbook:
                spans = {(r.min_row - 1, r.min_col - 1): (r.max_row - r.min_row + 1, r.max_col - r.min_col + 1)
                         for r in sheet.merged_cells.ranges}
                for row in sheet:
                    for cell in row:
                        if cell.data_type == "f":
                            issues.append({"location": f"sheet:{sheet.title}/{cell.coordinate}", "error": "spreadsheet_formula_requires_review"})
                            continue
                        block = _cell(sheet.title, cell.row - 1, cell.column - 1, cell.value, spans)
                        if block:
                            if sheet.sheet_state != "visible" or sheet.row_dimensions[cell.row].hidden or sheet.column_dimensions[cell.column_letter].hidden:
                                block["hidden"] = True
                                issues.append({"location": block["location"], "error": "hidden_sheet_requires_review"})
                            blocks.append(block)
        finally:
            workbook.close()
    else:
        import xlrd
        workbook = xlrd.open_workbook(file_contents=content, formatting_info=True)
        try:
            if sum(s.nrows * s.ncols for s in workbook.sheets()) > MAX_CELLS:
                raise UnsupportedFileType("spreadsheet_cell_limit_exceeded")
            for sheet in workbook.sheets():
                spans = {(r0, c0): (r1 - r0, c1 - c0) for r0, r1, c0, c1 in sheet.merged_cells}
                for row in range(sheet.nrows):
                    for column in range(sheet.ncols):
                        cell = sheet.cell(row, column)
                        value = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, workbook.datemode).isoformat()
                        elif isinstance(value, float) and value.is_integer():
                            value = int(value)
                        block = _cell(sheet.name, row, column, value, spans)
                        if block:
                            hidden = (getattr(sheet, "visibility", 0) or
                                      getattr(sheet.rowinfo_map.get(row), "hidden", False) or
                                      getattr(sheet.colinfo_map.get(column), "hidden", False))
                            if hidden:
                                block["hidden"] = True
                                issues.append({"location": block["location"], "error": "hidden_sheet_requires_review"})
                            blocks.append(block)
        finally:
            workbook.release_resources()
        # BIFF 수식·그림 전체를 아직 복원하지 못하므로 내용 완전성은 별도 검토한다.
        try:
            inventory = biff_objects(content)
        except Exception as exc:
            inventory = {"inventory_error": type(exc).__name__}
        issues.append({"location": "workbook", "error": "xls_objects_require_review", "records": inventory})
    return blocks, issues
