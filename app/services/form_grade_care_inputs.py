"""Source-bound academic and care recipient component destinations."""
from decimal import Decimal, InvalidOperation
import re

from app.services.document_layout import compact
from app.services.form_basic_inputs import _cell
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-grade-care-inputs-v1"


def native_grade_care_inputs(blocks, source):
    groups = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        if shape.get("method") != "native" or shape.get("columns") != 19:
            continue
        texts = [b for b in blocks if b.get("table") == shape["table"]]

        def title(row, column, text):
            found = [b for b in texts if b.get("row") == row and b.get("column") == column and compact(b["text"]) == compact(text)]
            spans = {(4, 3): 9, (4, 12): 7, (5, 3): 4, (5, 7): 5, (5, 12): 3, (5, 15): 4,
                     (6, 3): 4, (6, 7): 5, (6, 12): 3, (6, 15): 4, (11, 1): 2,
                     (11, 3): 3, (12, 3): 3, (11, 6): 2, (12, 6): 2, (11, 8): 2, (12, 8): 2}
            if len(found) != 1 or found[0].get("method") != "native" or found[0].get("column_span") != spans[(row, column)] or found[0].get("row_span") != (2 if (row, column) == (11, 1) else 1):
                return None
            cell = [c for c in shape["cells"] if c["row"] == row and c["column"] == column]
            if len(cell) != 1 or cell[0].get("has_control") or cell[0].get("blank") or cell[0].get("cell_location") != found[0].get("cell_location") or any(cell[0].get(k) != found[0].get(k) for k in ("table", "row_span", "column_span")):
                return None
            peers = [b for b in texts if b.get("cell_location") == cell[0]["cell_location"]]
            allowed = {"돌봄대상자", "질병ㆍ장애현황"} if (row, column) == (11, 1) else {"이름/", "본인과의관계"} if row in (11, 12) and column == 3 else {compact(text)}
            if {compact(b["text"]) for b in peers} != allowed or any(b.get("method") != "native" for b in peers) or set(cell[0].get("text_locations", [])) != {b["location"] for b in peers}:
                return None
            return found[0]

        def group(label, proofs, cells, slots, kind, subject=None):
            if not label or any(p is None for p in proofs) or any(c is None for c in cells):
                return
            field = _field(label, source, cells[0]["cell_location"])
            field["input_cell_sources"] = [{**(source or {}), **c} for c in cells]
            field["layout"].update({k: cells[0][k] for k in ("row", "column", "row_span", "column_span", "cell_location")})
            field["input_spec"] = {"version": VERSION, "slots": slots, "person_row": kind == "care"}
            field["instruction_sources"] = [block_source(p, source) for p in proofs]
            field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
            if subject:
                field["subject"] = {"label": subject["text"], "source": block_source(subject, source)}
            groups.append({"fields": [field], "locations": {p["location"] for p in proofs}, "kind": kind})

        def prompt_cell(row, col, span, prompt):
            cell = _cell(shape, row, col, span, blank=False)
            return cell if cell and prompt and cell.get("text_locations") == [prompt["location"]] and not cell.get("blank") and len(cell.get("paragraph_char_counts", [])) == 1 and cell["paragraph_char_counts"][0] > 1 else None

        for col, heading, credits_title, credits_span, gpa_col, gpa_span in (
                (3, "직전학기(26-1학기)", "이수학점", 4, 7, 5), (12, "전체학년", "총 이수학점", 3, 15, 4)):
            label = title(4, col, heading)
            credit = title(5, col, credits_title)
            gpa = title(5, gpa_col, "평점" if col == 3 else "평균평점")
            credit_prompt = title(6, col, "학점")
            gpa_prompt = title(6, gpa_col, "/4.5 or /4.3")
            cells = [prompt_cell(6, col, credits_span, credit_prompt), prompt_cell(6, gpa_col, gpa_span, gpa_prompt)]
            if not all((label, credit, gpa, credit_prompt, gpa_prompt, *cells)):
                continue
            slots = [
                {"key": "credits", "label": credit["text"], "kind": "nonnegative_number", "unit": "학점", "source": block_source(credit, source), "destination": cells[0]["cell_location"]},
                {"key": "gpa", "label": gpa["text"], "kind": "gpa", "unit": "", "scale_key": "scale", "source": block_source(gpa, source), "destination": cells[1]["cell_location"]},
                {"key": "scale", "label": "평점 만점 기준", "kind": "gpa_scale", "unit": "", "options": ["4.5", "4.3"], "source": block_source(gpa_prompt, source), "destination": cells[1]["cell_location"]}]
            group(label, [label, credit, gpa, credit_prompt, gpa_prompt], cells, slots, "grade")
        subject = title(11, 1, "돌봄대상자")
        context = title(11, 1, "질병ㆍ장애 현황")
        for row in (11, 12):
            name, relation = title(row, 3, "이름/"), title(row, 3, "본인과의 관계")
            slash, diagnosis = title(row, 6, "/"), title(row, 8, "주요 질환명·진단명")
            cells = [prompt_cell(row, 6, 2, slash), _cell(shape, row, 10, 2)]
            if not all((subject, context, name, relation, slash, diagnosis, *cells)):
                continue
            slots = [{"key": key, "label": label, "kind": "text", "unit": "", "source": block_source(proof, source), "destination": cells[index]["cell_location"]}
                     for key, label, proof, index in (("name", "이름", name, 0), ("relation", "본인과의 관계", relation, 0), ("diagnosis", diagnosis["text"], diagnosis, 1))]
            group(name, [subject, context, name, relation, slash, diagnosis], cells, slots, "care", subject)
    return groups


def check_grade_input(slot, text, answer, field):
    kind = slot.get("kind")
    if kind not in ("nonnegative_number", "gpa", "gpa_scale"):
        return
    if kind == "gpa_scale" and text not in slot["options"]:
        raise ValueError("choose the original GPA scale")
    if kind != "gpa_scale" and not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", text):
        raise ValueError("provide a nonnegative number")
    facts = (answer or {}).get("facts", {})
    values = {s["key"]: facts.get(s["key"], {}).get("text", "") for s in field["input_spec"]["slots"]}
    values[slot["key"]] = text
    gpa_slot = next((s for s in field["input_spec"]["slots"] if s.get("kind") == "gpa"), None)
    if not gpa_slot:
        return
    gpa_value = values.get(gpa_slot["key"])
    scale_value = values.get(gpa_slot["scale_key"])
    if kind in ("gpa", "gpa_scale") and gpa_value and scale_value:
        try:
            invalid = Decimal(gpa_value) > Decimal(scale_value)
        except InvalidOperation as exc:
            raise ValueError("invalid GPA components") from exc
        if invalid:
            raise ValueError("GPA exceeds the reported original scale")


def audit_grade_care_inputs(blocks, fields, source):
    groups = native_grade_care_inputs(blocks, source)
    controls = []
    for label in blocks:
        if compact(label["text"]) not in ("직전학기(26-1학기)", "전체학년", "이름/") or not label.get("table"):
            continue
        # Only the original known academic/recipient rows belong to this audit.
        if (label.get("row"), label.get("column")) not in ((4, 3), (4, 12), (11, 3), (12, 3)):
            continue
        expected = [g["fields"][0] for g in groups if g["fields"][0]["source"]["location"] == label["location"]]
        actual = [f for f in fields if f["source"]["location"] == label["location"]]
        valid = len(expected) == len(actual) == 1 and all(actual[0].get(k) == expected[0].get(k) for k in ("input_spec", "input_cell_sources", "instruction_sources", "role", "subject", "layout"))
        controls.append({"source": block_source(label, source), "status": "bound" if valid else "requires_original_review", "issues": [] if valid else ["grade_or_care_components_missing_or_conflicting"]})
    return {"version": VERSION, "controls": controls, "unresolved": sum(bool(c["issues"]) for c in controls), "scope": "two_academic_and_two_care_rows_not_whole_form"}
