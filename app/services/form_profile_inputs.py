"""Recover bounded native profile inputs without inferring people or conditions."""
import hashlib
import json
import re

from app.services.document_layout import compact
from app.services.form_consent import original_options
from app.services.form_semantics import block_source

VERSION = "native-profile-inputs-v1"


def _field(label, source, destination):
    proof = block_source(label, source)
    identity = {"source": source, "label": label["location"], "destination": destination}
    return {"id": hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24],
            "label": label["text"], "field_type": "text", "role": "unknown", "required": None,
            "min_length": None, "max_length": None, "length_unit": None, "includes_spaces": None,
            "options": [], "evidence": label["text"], "source": {**proof, "label_start": label["start"]},
            "layout": {k: label[k] for k in ("table", "table_fingerprint", "table_occurrence", "row", "column", "row_span", "column_span") if k in label},
            "instruction_sources": [proof], "instructions": label["text"]}


def native_profile_inputs(blocks, source):
    restored = []
    native = [b for b in blocks if b.get("method") == "native"]
    # One labelled academic cell with the exact five-component source grammar.
    for label in native:
        if compact(label["text"]) != "학적사항" or not label.get("table"):
            continue
        adjacent = [b for b in native if b.get("table") == label["table"] and b.get("row") == label.get("row")
                    and b.get("column") == label.get("column", -1) + label.get("column_span", 1)]
        prompts = [b for b in adjacent if compact(b["text"]) == "대학교(캠퍼스)과(부)학년학기"]
        if len(prompts) != 1:
            continue
        prompt = prompts[0]
        peers = [b for b in blocks if b.get("cell_location") == prompt.get("cell_location")]
        if any(b.get("method") != "native" for b in peers):
            continue
        rules = [b for b in peers if b is not prompt]
        # Do not silently discard an unknown extra paragraph or applicable rule.
        if len(rules) != 1 or compact(rules[0]["text"]) != "(2026-2학기기준으로작성)":
            continue
        field = _field(label, source, prompt["location"])
        field["input_spec"] = {"version": VERSION, "slots": [
            {"key": key, "label": title, "source": block_source(prompt, source), "destination": prompt["location"]}
            for key, title in (("school", "대학교"), ("campus", "캠퍼스"), ("department", "과(부)"), ("grade", "학년"), ("semester", "학기"))]}
        field["instruction_sources"].extend(block_source(b, source) for b in peers)
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        restored.append({"fields": [field], "locations": {b["location"] for b in [label, *peers]}})
    # A source row with four named labels followed by proven blank cells.
    for subject in native:
        if compact(subject["text"]) != "주보호자" or not subject.get("table"):
            continue
        rows = [b for b in blocks if b.get("table") == subject["table"] and b.get("row") == subject.get("row")]
        if any(b.get("method") != "native" for b in rows):
            continue
        labels = [[b for b in rows if compact(b["text"]) == title] for title in ("이름", "본인과의관계", "나이", "직업")]
        if any(len(items) != 1 for items in labels):
            continue
        labels = [items[0] for items in labels]
        shapes = [b["table_structure"] for b in blocks if b.get("table") == subject["table"] and b.get("table_structure")]
        if len(shapes) != 1:
            continue
        cells = shapes[0]["cells"]
        fields, valid = [], True
        for label in labels:
            matches = [c for c in cells if c["row"] == label["row"] and
                       c["column"] == label["column"] + label.get("column_span", 1)]
            if len(matches) != 1:
                valid = False
                break
            cell = matches[0]
            if (not cell.get("blank") or cell.get("has_control") or cell.get("text_locations") or
                    cell.get("row_span") != 1 or not cell.get("paragraph_char_counts") or
                    any(n != 1 for n in cell["paragraph_char_counts"]) or
                    cell["table"] != subject["table"] or cell["column"] + cell["column_span"] > shapes[0]["columns"]):
                valid = False
                break
            # Check all cells covering the proposed input; overlapping geometry
            # elsewhere in this table needn't suppress a separately proven row.
            overlaps = [c for c in cells if c["row"] <= cell["row"] < c["row"] + c["row_span"] and
                        c["column"] < cell["column"] + cell["column_span"] and cell["column"] < c["column"] + c["column_span"]]
            if len(overlaps) != 1:
                valid = False
                break
            field = _field(label, source, cell["cell_location"])
            field["subject"] = {"label": subject["text"], "source": block_source(subject, source)}
            field["input_cell_source"] = {**(source or {}), **cell}
            field["layout"].update({k: cell[k] for k in ("row", "column", "row_span", "column_span", "cell_location")})
            field["instruction_sources"].insert(0, block_source(subject, source))
            field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
            fields.append(field)
        if valid:
            restored.append({"fields": fields, "locations": {b["location"] for b in [subject, *labels]}})
    # A six-cell history row with a local, explicit 'applicable people only' rule.
    for label in native:
        if compact(label["text"]) != "우양재단장학수혜경험" or not label.get("table"):
            continue
        rows = [b for b in blocks if b.get("table") == label["table"] and b.get("row") == label.get("row")]
        if any(b.get("method") != "native" for b in rows):
            continue
        columns = {}
        for b in rows:
            columns.setdefault(b.get("column"), []).append(b)
        start = label["column"]
        if set(columns) != set(range(start, start + 6)) or any(b.get("column_span", 1) != 1 for b in rows):
            continue
        groups = [columns[start + i] for i in range(6)]
        if [compact(b["text"]) for b in groups[2]] != ["우양재단장학수혜내역", "(해당자만작성)"]:
            continue
        if len(groups[1]) != 1 or original_options(groups[1][0]["text"]) != ["있음", "없음"]:
            continue
        if any(len(items) != 1 or compact(items[0]["text"]) != title for items, title in zip(groups[3:], ("횟수:", "연도:", "장학명:"))):
            continue
        values = [{"key": key, "label": b["text"], "kind": "positive_integer" if key == "history_count" else "text",
                   "unit": "", "placement": "separate_cell", "source": block_source(b, source), "destination": b["location"]}
                  for key, items in zip(("history_count", "history_year", "history_name"), groups[3:]) for b in items]
        field = _field(label, source, groups[1][0]["location"])
        field.update(field_type="checkbox", options=["있음", "없음"], selection_mode="single",
                     option_sources=[block_source(groups[1][0], source)],
                     choice_spec={"version": "native-choice-values-v1", "multiple_explicit": False, "multiple_sources": [],
                                  "condition_sources": [block_source(groups[2][1], source)],
                                  "choices": [{"caption": caption, "source": block_source(groups[1][0], source),
                                               "values": values if caption == "있음" else []} for caption in ("있음", "없음")]})
        field["instruction_sources"] = [block_source(b, source) for b in rows]
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        restored.append({"fields": [field], "locations": {b["location"] for b in rows}})
    return restored


def check_input(field, key, text, answer=None):
    from app.services.form_external_history import input_slots, check_external
    slots = input_slots(field, answer)
    if key not in {s["key"] for s in slots} or not text.strip() or compact(text) in ("네", "예", "확인", "모름"):
        raise ValueError("provide the original component")
    slot = next(s for s in slots if s["key"] == key)
    if field['input_spec'].get('version') == 'spreadsheet-recommendation-v1':
        from app.services.form_spreadsheet_recommendation import check_recommendation
        check_recommendation(slot, text)
    if field['input_spec'].get('version') == 'spreadsheet-education-v1':
        from app.services.form_spreadsheet_education import check_education
        check_education(slot, text)
    check_external(slot, text, answer, field)
    from app.services.form_grade_care_inputs import check_grade_input
    check_grade_input(slot, text, answer, field)
    from app.services.form_submission_checklist import check_preparation
    check_preparation(slot, text)
    from app.services.form_checklist_values import check_checklist_value
    check_checklist_value(slot, text)
    if slot.get("kind") in ("nonnegative_integer", "amount") and not re.fullmatch(r"(?:0|[1-9]\d*)" + (r"(?:\.\d+)?" if slot["kind"] == "amount" else ""), text):
        raise ValueError("provide a number in the original unit")
    if slot.get("kind") == "positive_integer" and not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError("provide the actual ordinal")


def audit_profile_inputs(blocks, fields, source):
    recovered = native_profile_inputs(blocks, source)
    checks = []
    for label in blocks:
        if label.get("method") != "native" or not label.get("table") or compact(label["text"]) not in (
                "학적사항", "주보호자", "우양재단장학수혜경험"):
            continue
        candidates = [g for g in recovered if label["location"] in g["locations"]]
        expected = candidates[0]["fields"] if len(candidates) == 1 else []
        correct = bool(expected) and all(
            len(matches := [f for f in fields if f["id"] == target["id"]]) == 1 and
            all(matches[0].get(k) == target.get(k) for k in (
                "source", "input_spec", "choice_spec", "input_cell_source", "subject", "selection_mode"))
            for target in expected)
        checks.append({"source": block_source(label, source), "field_ids": [f["id"] for f in expected],
                       "status": "bound" if correct else "requires_original_review",
                       "issues": [] if correct else ["profile_inputs_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
            "scope": "three_known_native_profile_groups_not_whole_form"}


def render_input(field, answer):
    from app.services.form_external_history import input_slots
    rendered = []
    for slot in input_slots(field, answer):
        value = answer.get("facts", {}).get(slot["key"], {}).get("text", "")
        check_input(field, slot["key"], value, answer)
        if slot.get('kind') == 'education_presence':
            if value == '작성하지 않음':
                rendered.append(f"{slot['label']}: 원본 행을 비워 둠(사용자 응답)")
            continue
        if slot.get('kind') in ('education_month', 'education_status') and value in ('졸업 전', '표시하지 않음'):
            rendered.append(f"{slot['label']}: 원본 칸을 비워 둠(사용자 응답: {value})")
            continue
        if field["input_spec"].get("repeat") and slot["key"] in ("entry_count", "scope_confirmed"):
            continue
        rendered.append(f"{slot['label']}: {value}" + (f" {slot['unit']}" if slot.get("unit") else ""))
    if field["input_spec"].get("repeat") and not rendered:
        if field['input_spec'].get('version') == 'spreadsheet-repeating-v1':
            return f"{compact(field['label'])}: 작성 내역 없음(사용자 응답)"
        return "작성 대상 외부 장학 수혜 내역 없음(사용자 응답)"
    if field["input_spec"].get("version") == "native-checklist-values-v1":
        from app.services.form_checklist_values import render_checklist_value
        return render_checklist_value(field, {k: f["text"] for k, f in answer["facts"].items()})
    return "\n".join(rendered)
