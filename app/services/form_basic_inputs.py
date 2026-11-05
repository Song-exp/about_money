"""Bounded cover academic cells and basic contact destinations."""
from app.services.document_layout import compact
from app.services.form_profile_inputs import _field
from app.services.form_semantics import block_source

VERSION = "native-basic-inputs-v1"


def _cell(shape, row, column, span, blank=True):
    matches = [c for c in shape["cells"] if c["row"] == row and c["column"] == column]
    if len(matches) != 1:
        return None
    cell = matches[0]
    if cell.get("table") != shape["table"] or column + span > shape["columns"] or cell.get("column_span") != span or cell.get("row_span") != 1 or cell.get("has_control"):
        return None
    if blank and (not cell.get("blank") or cell.get("text_locations") or not cell.get("paragraph_char_counts") or any(n != 1 for n in cell["paragraph_char_counts"])):
        return None
    overlaps = [c for c in shape["cells"] if c["row"] <= row < c["row"] + c["row_span"] and c["column"] < column + span and column < c["column"] + c["column_span"]]
    return cell if len(overlaps) == 1 else None


def _input(label, cell, source, key, title=None):
    field = _field(label, source, cell["cell_location"])
    field["input_cell_source"] = {**(source or {}), **cell}
    # A bounded fallback coordinate keeps the source visible for review, but
    # cannot establish that this is the application's actual input destination.
    if cell.get("geometry_proven") is False:
        field["constraint_issues"] = ["basic_input_geometry_requires_original_review"]
    field["layout"].update({k: cell[k] for k in ("row", "column", "row_span", "column_span", "cell_location")})
    field["input_spec"] = {"version": VERSION, "slots": [{"key": key, "label": title or label["text"], "kind": "text", "unit": "",
        "source": block_source(label, source), "destination": cell["cell_location"]}]}
    return field


def native_basic_inputs(blocks, source):
    groups = []
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        if shape.get("method") != "native":
            continue
        table = shape["table"]
        texts = [b for b in blocks if b.get("table") == table]
        # Complete cover geometry plus original grade/semester prompt.
        if shape.get("rows") == 2 and shape.get("columns") == 4 and len(shape["cells"]) == 8:
            if shape.get("issues"):
                continue
            if any(b.get("method") != "native" for b in texts) or [(b["row"], b["column"], compact(b["text"])) for b in texts] != [
                (0, 0, "학교"), (0, 2, "학과"), (1, 0, "학년"), (1, 1, "학년/학기"), (1, 2, "성명")]:
                continue
            if any(c.get("row_span") != 1 or c.get("column_span") != 1 or c.get("has_control") for c in shape["cells"]):
                continue
            fields = []
            for label, key in ((texts[0], "school"), (texts[1], "department"), (texts[4], "name")):
                cell = _cell(shape, label["row"], label["column"] + 1, 1)
                if not cell:
                    break
                fields.append(_input(label, cell, source, key))
            cell = _cell(shape, 1, 1, 1, blank=False)
            if len(fields) != 3 or not cell or cell.get("blank") or cell.get("text_locations") != [texts[3]["location"]]:
                continue
            field = _input(texts[2], cell, source, "grade")
            field["input_spec"]["slots"] = [{"key": key, "label": title, "kind": "text", "unit": "",
                "source": block_source(texts[3], source), "destination": cell["cell_location"]}
                for key, title in (("grade", "학년"), ("semester", "학기"))]
            field["instruction_sources"].append(block_source(texts[3], source))
            field["instructions"] = "\n".join(p["evidence"] for p in field["instruction_sources"])
            fields.append(field)
            groups.append({"fields": fields, "locations": {b["location"] for b in texts}, "kind": "cover"})
        # Known basic row titles and adjacent merged blank destinations.
        if shape.get("columns") != 19:
            continue
        rows = [b for b in texts if b.get("row", 99) < 3]
        if any(b.get("method") != "native" for b in rows):
            continue
        anchors = {(b["row"], b["column"]): compact(b["text"]) for b in rows if b.get("column") in (0, 10)}
        if anchors != {(0, 0): "성명", (0, 10): "주민등록번호", (1, 0): "연락처", (1, 10): "이메일", (2, 0): "주소", (2, 10): "주거형태"}:
            continue
        fields, labels = [], []
        for row, col, label_span, destination, span, key in ((0, 0, 3, 3, 7, "name"), (1, 0, 3, 3, 7, "phone"),
            (1, 10, 5, 15, 4, "email"), (2, 0, 3, 3, 7, "address")):
            peers = [b for b in rows if b["row"] == row and b["column"] == col]
            cell = _cell(shape, row, destination, span)
            if len(peers) != 1 or peers[0].get("column_span") != label_span or peers[0].get("row_span") != 1 or not cell:
                break
            labels.append(peers[0])
            fields.append(_input(peers[0], cell, source, key))
        if len(fields) == 4:
            groups.append({"fields": fields, "locations": {b["location"] for b in labels}, "kind": "contact"})
    return groups


def audit_basic_inputs(blocks, fields, source):
    groups = native_basic_inputs(blocks, source)
    checks = []
    # Detect known cover/contact structures even when blank recovery fails.
    for owner in blocks:
        shape = owner.get("table_structure") or {}
        texts = [b for b in blocks if b.get("table") == shape.get("table")]
        cover = shape.get("rows") == 2 and shape.get("columns") == 4 and {compact(b["text"]) for b in texts} >= {"학교", "학과", "성명"}
        contact = shape.get("columns") == 19 and {compact(b["text"]) for b in texts if b.get("row", 99) < 3} >= {"성명", "연락처", "이메일", "주소"}
        if not (cover or contact):
            continue
        kind = "cover" if cover else "contact"
        matching = [g for g in groups if g["kind"] == kind and g["fields"][0]["layout"]["table"] == shape["table"]]
        expected = matching[0]["fields"] if len(matching) == 1 else []
        valid = bool(expected) and all(target.get("input_cell_source", {}).get("geometry_proven") is not False and
            len(actual := [f for f in fields if f["id"] == target["id"]]) == 1 and
            all(actual[0].get(k) == target.get(k) for k in ("source", "input_spec", "input_cell_source", "instructions", "role")) for target in expected)
        checks.append({"source": block_source(owner, source), "kind": kind, "status": "bound" if valid else "requires_original_review",
            "issues": [] if valid else ["basic_input_destination_missing_or_conflicting"]})
    return {"version": VERSION, "controls": checks, "unresolved": sum(bool(c["issues"]) for c in checks), "scope": "cover_and_four_contact_inputs_not_whole_form"}
