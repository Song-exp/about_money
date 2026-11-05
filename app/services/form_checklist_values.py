"""Collect native checklist write prompts without claiming control/cell rendering."""
import hashlib
import re

from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-checklist-values-v1"


def _candidate(shape, texts):
    return shape.get("rows") == 8 and shape.get("columns") == 7 and any(
        compact(b["text"]) in ("성적증명서(국문)", "학자금지원구간통지서") for b in texts)


def native_checklist_values(blocks, source):
    groups = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        texts = [b for b in blocks if b.get("table") == shape.get("table")]
        if not _candidate(shape, texts) or shape.get("method") != "native":
            continue
        for kind, row, column, title in (("grade", 4, 1, "성적증명서(국문)"), ("income", 6, 3, "학자금지원구간통지서")):
            labels = [b for b in texts if (b.get("row"), b.get("column"), compact(b["text"])) == (row, column, title)]
            peers = [b for b in texts if b.get("row") == row and b.get("column") == 5]
            cells = [c for c in shape.get("cells", []) if c.get("row") == row and c.get("column") == 5]
            if len(labels) != 1 or len(cells) != 1 or any(b.get("method") != "native" for b in [*labels, *peers]):
                continue
            cell = cells[0]
            if (cell.get("blank") or cell.get("table") != shape["table"] or cell.get("row_span") != 1 or cell.get("column_span") != 1 or
                cell.get("text_locations") != [b["location"] for b in peers] or
                any(b.get("cell_location") != cell.get("cell_location") or b.get("row_span") != 1 or b.get("column_span") != 1 for b in peers)):
                continue
            if kind == "grade":
                if [compact(b["text"]) for b in peers] != ["*직전학기성적", ":(작성하기)/4.5or(작성하기)/4.3", "*전체학기열람가능해야함"]:
                    continue
                prompt = peers[1]
            else:
                if len(peers) != 2 or compact(peers[1]["text"]) != "*전체학기열람으로발급":
                    continue
                prompt = peers[0]
                match = re.fullmatch(r"\*(\d{2,4})년([12])학기소득구간\(작성하기\)분위\((\d+)분위이하\)", compact(prompt["text"]))
                if not match:
                    continue
            if not all(isinstance(b.get("start"), int) and isinstance(b.get("end"), int) and b["end"] - b["start"] == len(b["text"]) for b in peers):
                continue
            if any(a["end"] >= b["start"] for a, b in zip(peers, peers[1:])):
                continue
            proof = block_source(prompt, source)
            field = _field(prompt, source, prompt["location"])
            field["label"] = "제출 체크리스트 · " + ("직전학기 성적" if kind == "grade" else "학자금지원구간통지서 소득구간")
            field["input_spec"] = {"version": VERSION, "paragraph_template": True, "original_layout_review_required": True, "slots": []}
            if kind == "grade":
                field["input_spec"]["slots"] = [
                    {"key": "gpa", "label": "직전학기 성적", "kind": "gpa", "unit": "", "scale_key": "scale", "source": proof, "destination": prompt["location"]},
                    {"key": "scale", "label": "원문 만점 기준", "kind": "gpa_scale", "unit": "", "options": ["4.5", "4.3"], "source": proof, "destination": prompt["location"]}]
                rules = [labels[0], *peers]
            else:
                choices = [b for b in texts if compact(b["text"]) == "택1" and (b.get("row"), b.get("column"), b.get("row_span")) == (5, 2, 2)]
                indices = [i for i, b in enumerate(blocks) if b.get("table") == shape["table"]]
                footnotes = [b for b in blocks[max(indices) + 1:max(indices) + 5] if compact(b["text"]).startswith("※가정경제형편을증명하는서류인5번")]
                if len(choices) != 1 or len(footnotes) != 1 or any(b.get("method") != "native" for b in choices + footnotes):
                    continue
                field["input_spec"]["slots"] = [{"key": "income_bracket", "label": f"{match[1]}년 {match[2]}학기 소득구간", "kind": "checklist_income_bracket",
                    "unit": "분위", "maximum": int(match[3]), "source": proof, "destination": prompt["location"]}]
                field["label"] = f"제출 체크리스트 · {match[1]}년 {match[2]}학기 소득구간"
                rules = [labels[0], *peers, *choices, *footnotes]
                condition_source = block_source(labels[0], source)
                field["applicability"] = {"id": hashlib.sha256((field["id"] + ":selected-document").encode()).hexdigest()[:24],
                    "discard_facts_on_change": True,
                    "mode": "user_selected_document", "evidence": "\n".join(b["text"] for b in [labels[0], *choices, *footnotes]),
                    "source": condition_source, "sources": [block_source(b, source) for b in [labels[0], *choices, *footnotes]]}
            field["instruction_sources"] = [block_source(b, source) for b in rules]
            field["instructions"] = "\n".join(s["evidence"] for s in field["instruction_sources"])
            field["original_destination"] = {"version": VERSION, "kind": "paragraph_in_cell_template", "source": proof,
                "visual_placement_verified": False, "cell_geometry_verified": False, "contains_controls": bool(cell.get("has_control"))}
            # The paragraph is proven text; surrounding control placement is still manual review.
            groups.append({"fields": [field], "locations": {prompt["location"]}, "kind": kind})
    return groups


def check_checklist_value(slot, text):
    if slot.get("kind") == "checklist_income_bracket" and (not re.fullmatch(r"(?:0|[1-9]\d*)", text) or int(text) > slot["maximum"]):
        raise ValueError("report the source semester's actual bracket within the original upper bound")


def render_checklist_value(field, values):
    if field["input_spec"]["slots"][0]["kind"] == "gpa":
        return f"{values['gpa']} / {values['scale']}"
    return f"{values['income_bracket']} 분위"


def audit_checklist_values(blocks, fields, source):
    groups = native_checklist_values(blocks, source)
    checks = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        texts = [b for b in blocks if b.get("table") == shape.get("table")]
        if not _candidate(shape, texts):
            continue
        for kind in ("grade", "income"):
            targets = [g["fields"][0] for g in groups if g["kind"] == kind and g["fields"][0]["layout"]["table"] == shape["table"]]
            actual = [f for f in fields if targets and f["id"] == targets[0]["id"]]
            valid = len(targets) == len(actual) == 1 and all(actual[0].get(k) == targets[0].get(k) for k in
                ("source", "label", "field_type", "layout", "input_spec", "choice_spec", "manual_spec", "semantic_kind",
                 "original_destination", "instruction_sources", "instructions", "applicability", "role", "required"))
            checks.append({"source": block_source(owner, source), "kind": kind, "status": "bound" if valid else "requires_original_review",
                "issues": [] if valid else ["checklist_write_prompt_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
        "scope": "two_checklist_paragraph_prompts_not_cell_geometry_or_control_rendering", "visual_review_complete": False}
