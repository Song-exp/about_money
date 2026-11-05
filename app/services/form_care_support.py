"""Bounded native care prompt and mutually conditional support inputs."""
from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-care-support-v1"
CARE = "현재돌봄대상자를위해본인이직접돌봄하고있는내용을간략히작성해주세요."
SUPPORT = "①수급자,차상위에해당하는경우해당사항표시"
BRACKET = "②학자금지원구간(①에해당하지않을경우작성)"


def native_care_support(blocks, source):
    groups = []
    for label in blocks:
        name = compact(label["text"])
        if name not in (CARE, SUPPORT) or label.get("method") != "native" or not label.get("table"):
            continue
        row = [b for b in blocks if b.get("table") == label["table"] and b.get("row") == label.get("row")]
        peers = [b for b in row if b.get("cell_location") == label.get("cell_location")]
        adjacent = [b for b in row if b.get("column") == label["column"] + label.get("column_span", 1)]
        if any(b.get("method") != "native" for b in row):
            continue
        if name == CARE:
            if len(row) != 2 or len(peers) != 1 or len(adjacent) != 1 or compact(adjacent[0]["text"]) != "예:병원동행,식사·복약관리,이동보조등":
                continue
            field = _field(label, source, adjacent[0]["location"])
            field["input_spec"] = {"version": VERSION, "slots": [{"key": "current_care", "label": "본인이 현재 직접 수행하는 돌봄 내용",
                "kind": "text", "unit": "", "source": block_source(label, source), "destination": adjacent[0]["location"]}]}
            used = row
        else:
            if len(row) != 3 or [compact(b["text"]) for b in peers] != [SUPPORT, "*증빙서류제출가능한경우만인정"] or len(adjacent) != 1 or compact(adjacent[0]["text"]) != "①수급자②차상위":
                continue
            following = [b for b in blocks if b.get("table") == label["table"] and b.get("row") == label["row"] + 1]
            if len(following) != 2 or any(b.get("method") != "native" for b in following):
                continue
            condition = [b for b in following if compact(b["text"]) == BRACKET and b.get("column") == label["column"] and b.get("column_span", 1) == label.get("column_span", 1)]
            prompt = [b for b in following if compact(b["text"]) == "구간(2구간이내)" and b.get("column") == adjacent[0]["column"] and b.get("column_span", 1) == adjacent[0].get("column_span", 1)]
            if len(condition) != 1 or len(prompt) != 1:
                continue
            field = _field(label, source, adjacent[0]["location"])
            field.update(field_type="checkbox", options=["①수급자", "②차상위"], selection_mode="marks",
                option_sources=[block_source(adjacent[0], source)],
                choice_spec={"version": VERSION, "allow_empty": True, "multiple_explicit": False, "multiple_sources": [],
                    "condition_sources": [block_source(condition[0], source)],
                    "empty_values": [{"key": "support_bracket", "label": "학자금 지원 구간", "kind": "integer_upper_bound", "maximum": 2,
                        "unit": "구간", "placement": "separate_cell", "source": block_source(prompt[0], source), "destination": prompt[0]["location"]}],
                    "choices": [{"caption": caption, "source": block_source(adjacent[0], source), "values": [
                        {"key": key, "label": caption + " 증빙서류 제출 가능 여부(사용자 응답)", "kind": "evidence_availability", "unit": "",
                         "placement": "separate_cell", "source": block_source(peers[1], source)}]}
                        for caption, key in (("①수급자", "recipient_evidence"), ("②차상위", "near_poverty_evidence"))]})
            used = [*row, *following]
        field["instruction_sources"] = [block_source(b, source) for b in used]
        field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
        groups.append({"fields": [field], "locations": {b["location"] for b in used}})
    return groups


def audit_care_support(blocks, fields, source):
    recovered = native_care_support(blocks, source)
    checks = []
    for label in blocks:
        if label.get("method") != "native" or not label.get("table") or compact(label["text"]) not in (CARE, SUPPORT, BRACKET):
            continue
        matches = [g for g in recovered if label["location"] in g["locations"]]
        target = matches[0]["fields"][0] if len(matches) == 1 else None
        actual = [f for f in fields if target and f["id"] == target["id"]]
        valid = len(actual) == 1 and all(actual[0].get(k) == target.get(k) for k in (
            "source", "input_spec", "choice_spec", "selection_mode", "instructions", "options"))
        checks.append({"source": block_source(label, source), "status": "bound" if valid else "requires_original_review",
            "issues": [] if valid else ["care_support_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
        "scope": "care_prompt_and_support_branch_not_whole_form"}
