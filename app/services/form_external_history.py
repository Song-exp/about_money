"""Source-grounded external benefit rows, with collection-only row count."""
import re
from datetime import date

from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-external-history-v1"
LABEL = "외부장학금수혜경험"
HEADERS = [(2, 2, "수혜연도", "year", "year"), (4, 3, "지급처", "provider", "text"),
    (7, 1, "장학명", "name", "text"), (8, 3, "학기당수혜금액", "semester_amount", "semester_amount"),
    (11, 3, "총(예정)수혜금액", "total_amount", "reported_amount"),
    (14, 3, "수혜기간", "period", "date"), (17, 2, "장학금성격", "nature", "text")]


def native_external_history(blocks, source):
    groups = []
    for label in blocks:
        if compact(label["text"]) != LABEL or label.get("method") != "native" or not label.get("table"):
            continue
        table = [b for b in blocks if b.get("table") == label["table"]]
        peers = [b for b in table if b.get("cell_location") == label.get("cell_location")]
        if [compact(b["text"]) for b in peers] != [LABEL, "※등록금으로납부된국가장학금,교내장학금제외", "※칸을추가하여작성가능"] or label.get("row_span") != 5 or label.get("column") != 0 or label.get("column_span") != 2:
            continue
        header_row = [b for b in table if b.get("row") == label["row"]]
        if len(header_row) != 12 or any(b.get("method") != "native" for b in header_row):
            continue
        columns, valid = [], True
        for col, span, title, key, kind in HEADERS:
            items = [b for b in header_row if b.get("column") == col]
            expected = [title] + (["※일시장학의경우‘일시’로기입"] if key == "semester_amount" else ["(생활비,월세지원등)"] if key == "nature" else [])
            if [compact(b["text"]) for b in items] != expected or any(b.get("column_span") != span or b.get("row_span") != 1 for b in items):
                valid = False
                break
            proof = block_source(items[0], source)
            if key == "period":
                columns.extend({"key": k, "label": title, "kind": "date", "unit": "", "source": proof,
                    "destination": items[0]["location"]} for k, title in (("start_date", "수혜시작일"), ("end_date", "수혜종료일")))
            else:
                columns.append({"key": key, "label": items[0]["text"], "kind": kind, "unit": "", "source": proof, "destination": items[0]["location"]})
        shapes = [b["table_structure"] for b in table if b.get("table_structure")]
        if not valid or len(shapes) != 1:
            continue
        templates, used = [], [*header_row]
        for offset, year in enumerate(("2026년", "2025년", "2024년", "2023년"), 1):
            row = label["row"] + offset
            texts = [b for b in table if b.get("row") == row]
            if len(texts) != 2 or [compact(b["text"]) for b in texts] != [year, "수혜시작일~수혜종료일"] or any(b.get("method") != "native" for b in texts):
                valid = False
                break
            cells = [c for c in shapes[0]["cells"] if c["row"] == row]
            if len(cells) != 7 or sorted((c["column"], c["column_span"], c["row_span"]) for c in cells) != [(h[0], h[1], 1) for h in HEADERS]:
                valid = False
                break
            for cell in cells:
                overlaps = [c for c in shapes[0]["cells"] if c["row"] <= row < c["row"] + c["row_span"] and c["column"] < cell["column"] + cell["column_span"] and cell["column"] < c["column"] + c["column_span"]]
                if len(overlaps) != 1 or (cell["column"] not in (2, 14) and (not cell.get("blank") or cell.get("has_control") or cell.get("text_locations") or cell.get("paragraph_char_counts") != [1])):
                    valid = False
            templates.append({"year_source": block_source(texts[0], source), "date_source": block_source(texts[1], source),
                "cells": [{**source, **c} for c in cells]})
            used.extend(texts)
        if not valid:
            continue
        field = _field(label, source, label["location"])
        field["input_spec"] = {"version": VERSION, "slots": [
            {"key": "scope_confirmed", "label": "원본 제외 조건 확인", "kind": "scope_confirmation", "unit": "", "source": block_source(peers[1], source)},
            {"key": "entry_count", "label": "작성할 수혜 내역 수(정보 수집용)", "kind": "row_count", "unit": "", "source": block_source(peers[2], source)}],
            "repeat": {"count_key": "entry_count", "columns": columns, "templates": templates,
                "addition_source": block_source(peers[2], source), "collection_limit": 100, "original_layout_review_required": True}}
        field["instruction_sources"] = [block_source(b, source) for b in used]
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        groups.append({"fields": [field], "locations": {b["location"] for b in used}})
    return groups


def input_slots(field, answer=None):
    if field['input_spec'].get('version') == 'spreadsheet-education-v1':
        from app.services.form_spreadsheet_education import education_slots
        return education_slots(field, answer)
    if field['input_spec'].get('version') == 'spreadsheet-repeating-v1':
        from app.services.form_spreadsheet_repeat import repeat_slots
        return repeat_slots(field, answer)
    spec = field["input_spec"]
    slots = list(spec["slots"])
    if spec.get("preparation_only") and (answer or {}).get("facts", {}).get("preparation_status", {}).get("text") == "준비됨":
        slots.extend(spec.get("ready_slots", []))
    repeat = spec.get("repeat")
    if repeat:
        count = (answer or {}).get("facts", {}).get(repeat["count_key"], {}).get("text", "")
        if re.fullmatch(r"(?:0|[1-9]\d{0,2})", count) and int(count) <= repeat["collection_limit"]:
            for index in range(int(count)):
                slots.extend({**column, "key": f"entry_{index}_{column['key']}", "label": f"{index + 1}번째 내역 · {column['label']}",
                    "row_index": index, "column_key": column["key"]} for column in repeat["columns"])
    return slots


def check_external(slot, text, answer, field):
    kind = slot.get("kind")
    if kind == "scope_confirmation" and compact(text) != "제외조건확인":
        raise ValueError("confirm the original exclusion rule")
    if kind == "row_count" and (not re.fullmatch(r"(?:0|[1-9]\d{0,2})", text) or int(text) > field["input_spec"]["repeat"]["collection_limit"]):
        raise ValueError(f"collection supports 0 to {field['input_spec']['repeat']['collection_limit']} entries")
    if kind == 'sheet_enum' and text not in slot['options']:
        raise ValueError('choose the original Y/N value')
    if kind == "year" and not re.fullmatch(r"\d{4}", text):
        raise ValueError("enter the reported four-digit year")
    if kind in ("reported_amount", "semester_amount"):
        if not (kind == "semester_amount" and text == "일시") and not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?\s*(?:원|만원)", text):
            raise ValueError("report the amount with its actual currency unit")
    if kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("enter an actual calendar date")
        parsed = date.fromisoformat(text)
        if slot["column_key"] == "end_date":
            start = (answer or {}).get("facts", {}).get(f"entry_{slot['row_index']}_start_date", {}).get("text")
            if start and parsed < date.fromisoformat(start):
                raise ValueError("end date precedes start date")


def audit_external_history(blocks, fields, source):
    recovered = native_external_history(blocks, source)
    checks = []
    for label in blocks:
        if compact(label["text"]) != LABEL or label.get("method") != "native" or not label.get("table"):
            continue
        candidates = [g for g in recovered if label["location"] in g["locations"]]
        target = candidates[0]["fields"][0] if len(candidates) == 1 else None
        actual = [f for f in fields if target and f["id"] == target["id"]]
        valid = len(actual) == 1 and all(actual[0].get(k) == target.get(k) for k in ("source", "input_spec", "instructions"))
        checks.append({"source": block_source(label, source), "status": "bound" if valid else "requires_original_review",
            "issues": [] if valid else ["external_history_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks), "scope": "external_history_not_whole_form"}
