"""Check conditional consent rows from source blocks, without restoring fields.

This audit does not call native_grids or infer relationships to other tables.
It is a semantic row contract, not a whole-form or visual placement audit.
"""
import hashlib
import json
import re

from app.services.document_layout import compact
from app.services.form_semantics import block_source

VERSION = "native-grid-condition-audit-v1"
HEADERS = ["구분", "성명", "신청자와의관계", "동의여부", "서명또는인"]


def audit_grid_conditions(blocks, fields, source):
    checks = []
    for subject in blocks:
        if subject.get("column") != 0 or not subject.get("row") or not re.search(r"[（(]\s*해당\s*시\s*[）)]", subject["text"]):
            continue
        table, row = subject.get("table"), subject["row"]
        texts = [b for b in blocks if table and b.get("table") == table]
        headers = [[b for b in texts if b.get("row") == 0 and b.get("column") == col] for col in range(5)]
        # Only the consent grid; do not reinterpret unrelated conditional text.
        if not any(compact(b["text"]) == "동의여부" for b in headers[3]) and sum(
            any(compact(b["text"]) == label for b in h) for h, label in zip(headers, HEADERS)) < 3:
            continue
        proof = block_source(subject, source)
        cid = hashlib.sha256(json.dumps({"source": source, "location": proof["location"], "table": table,
            "row": row, "evidence": subject["text"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        condition = {"id": cid, "mode": "user_confirmation", "evidence": subject["text"],
                     "source": proof, "discard_facts_on_change": True}
        issues, bound = [], []
        owners = [b for b in texts if b.get("table_structure")]
        shape = owners[0]["table_structure"] if len(owners) == 1 else {}
        if (subject.get("method") != "native" or shape.get("method") != "native" or shape.get("issues") or
            shape.get("columns") != 5 or not 1 <= row < shape.get("rows", 0) or
            any(len(h) != 1 or compact(h[0]["text"]) != label or h[0].get("method") != "native" for h, label in zip(headers, HEADERS)) or
            len([b for b in texts if b.get("row") == row and b.get("column") == 0]) != 1):
            issues.append("conditional_grid_source_not_proven")
        for col in range(1, 5):
            cells = [c for c in shape.get("cells", []) if c.get("row") == row and c.get("column") == col]
            cell = cells[0] if len(cells) == 1 else {}
            location = f"{table}/row:{row + 1}/cell:{col + 1}"
            peers = [b for b in texts if b.get("row") == row and b.get("column") == col]
            valid = (len(cells) == 1 and cell.get("cell_location") == location and cell.get("table") == table and
                     cell.get("row_span") == cell.get("column_span") == 1 and not cell.get("has_control") and
                     cell.get("text_locations") == [b["location"] for b in peers])
            if col == 3:
                valid = valid and len(peers) == 1 and peers[0].get("method") == "native" and compact(peers[0]["text"]).strip("()[]") in ("동의함□", "동의함☐")
            else:
                valid = valid and cell.get("blank") and not peers and bool(cell.get("paragraph_char_counts")) and all(n == 1 for n in cell["paragraph_char_counts"])
            actual = [f for f in fields if (f.get("input_cell_source") or {}).get("cell_location") == location or
                      ((f.get("layout") or {}).get("table"), (f.get("layout") or {}).get("row"), (f.get("layout") or {}).get("column")) == (table, row, col)]
            if not valid:
                issues.append(f"conditional_grid_cell_not_proven:{col}")
            if len(actual) != 1 or len(headers[col]) != 1:
                issues.append(f"conditional_grid_field_missing_or_duplicate:{col}")
                continue
            field, header = actual[0], headers[col][0]
            header_proof = block_source(header, source)
            expected_source = {**header_proof, "label_start": header["start"]}
            expected_type = "checkbox" if col == 3 else "signature" if col == 4 else "text"
            layout = field.get("layout") or {}
            contract = (field.get("repeating_grid") is True and field.get("applicability") == condition and
                        field.get("subject") == {"label": subject["text"], "source": proof} and
                        field.get("source") == expected_source and field.get("type_source") == header_proof and
                        field.get("label") == header["text"] and field.get("evidence") == header["text"] and
                        field.get("input_cell_source") == {**(source or {}), **cell} and
                        field.get("field_type") == expected_type and field.get("required") is None and field.get("role") == "unknown" and
                        all(layout.get(k) == cell.get(k) for k in ("table", "row", "column", "row_span", "column_span", "cell_location")) and
                        proof in field.get("instruction_sources", []) and header_proof in field.get("instruction_sources", []) and
                        not field.get("manual_spec") and not field.get("input_spec") and not field.get("choice_spec"))
            if col == 3:
                contract = contract and field.get("semantic_kind") == "consent" and field.get("selection_mode") == "single_mark" and field.get("options") == ["동의함"] and len(peers) == 1 and field.get("option_sources") == [block_source(peers[0], source)]
            else:
                contract = contract and field.get("options") == [] and not field.get("semantic_kind")
            if not contract:
                issues.append(f"conditional_grid_field_contract_conflict:{col}")
            elif valid:
                bound.append(field["id"])
        for field in fields:
            assigned = field.get("applicability") or {}
            if assigned.get("id") == cid or assigned.get("source", {}).get("location") == proof["location"]:
                layout = field.get("layout") or {}
                if layout.get("table") != table or layout.get("row") != row:
                    issues.append("conditional_grid_condition_leaks_to_other_subject")
        checks.append({"source": proof, "condition_id": cid, "table": table, "row": row,
                       "field_ids": bound, "status": "requires_original_review" if issues else "bound",
                       "issues": list(dict.fromkeys(issues))})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks),
            "scope": "conditional_consent_rows_only", "visual_review_complete": False}
