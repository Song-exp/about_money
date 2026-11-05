"""Restore inputs only in a fully proven native repeating consent grid."""
import hashlib
import json
import re

from app.services.document_layout import compact
from app.services.form_semantics import block_source
from app.services.form_consent import original_options

HEADERS = ["구분", "성명", "신청자와의관계", "동의여부", "서명또는인"]


def native_grids(blocks, source=None):
    restored = []
    for owner in blocks:
        structure = owner.get("table_structure") or {}
        if structure.get("method") != "native" or structure.get("issues"):
            continue
        rows, columns = structure.get("rows", 0), structure.get("columns", 0)
        if not 2 <= rows <= 128 or columns != 5 or len(structure.get("cells", [])) != rows * columns:
            continue
        table = structure["table"]
        native = [b for b in blocks if b.get("table") == table and b.get("method") == "native"]
        if any(b.get("method") != "native" for b in blocks if b.get("table") == table):
            continue
        cells = {(c["row"], c["column"]): c for c in structure["cells"]}
        if len(cells) != rows * columns or any(c.get("row_span") != 1 or c.get("column_span") != 1 or c.get("has_control")
            or c.get("table") != table or c.get("cell_location") != f"{table}/row:{c['row'] + 1}/cell:{c['column'] + 1}"
            for c in cells.values()) or set(cells) != {(r, c) for r in range(rows) for c in range(columns)}:
            continue
        by_cell = {key: [b for b in native if (b.get("row"), b.get("column")) == key] for key in cells}
        if any([b["location"] for b in by_cell[key]] != cell.get("text_locations") for key, cell in cells.items()):
            continue
        headers = [by_cell[(0, c)] for c in range(columns)]
        if any(len(h) != 1 or compact(h[0]["text"]) != expected for h, expected in zip(headers, HEADERS)):
            continue
        headers = [h[0] for h in headers]
        fields, references, valid = [], [], True
        for row in range(1, rows):
            subjects = by_cell[(row, 0)]
            if len(subjects) != 1 or len(compact(subjects[0]["text"])) > 60:
                valid = False
                break
            subject = subjects[0]
            condition = None
            if re.search(r"[（(]\s*해당\s*시\s*[）)]", subject["text"]):
                proof = block_source(subject, source)
                identity = hashlib.sha256(json.dumps({"source": source, "location": proof["location"],
                    "table": table, "row": row, "evidence": subject["text"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                condition = {"id": identity, "mode": "user_confirmation", "evidence": subject["text"],
                             "source": proof, "discard_facts_on_change": True}
            for column in range(1, columns):
                cell = cells[(row, column)]
                header = headers[column]
                choices = by_cell[(row, column)]
                if column != 3:
                    if not cell.get("blank") or cell.get("text_locations") or not cell.get("paragraph_char_counts") or any(n != 1 for n in cell["paragraph_char_counts"]):
                        valid = False
                        break
                    field_type, options = ("signature" if column == 4 else "text"), []
                elif len(choices) == 1 and compact(choices[0]["text"]) == "위표시와같음" and compact(subject["text"]) in ("신청자", "신청인"):
                    references.append({"reason": "repeated_table_reference_requires_review", "source": block_source(choices[0], source),
                                       "subject_source": block_source(subject, source), "input_cell": cell})
                    continue
                elif len(choices) == 1 and original_options(choices[0]["text"]) == ["동의함"] and compact(choices[0]["text"]).strip("()[]") in ("동의함□", "동의함☐"):
                    field_type, options = "checkbox", ["동의함"]
                else:
                    valid = False
                    break
                proof = block_source(header, source)
                identity = {"source": source, "input_cell": cell["cell_location"], "label": header["text"]}
                field = {"id": hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24],
                         "label": header["text"], "field_type": field_type, "required": None,
                         "min_length": None, "max_length": None, "length_unit": None, "includes_spaces": None,
                         "options": options, "evidence": header["text"], "source": {**proof, "label_start": header["start"]},
                         "role": "applicant" if compact(subject["text"]) in ("신청자", "신청인") else "unknown",
                         "subject": {"label": subject["text"], "source": block_source(subject, source)},
                         "layout": {**{k: owner[k] for k in ("table_fingerprint", "table_occurrence")},
                                    **{k: cell[k] for k in ("table", "row", "column", "row_span", "column_span", "cell_location")},
                                    "cell_paragraph": 0, "block_offset": 0},
                         "input_cell_source": {**(source or {}), **cell},
                         "type_source": proof, "repeating_grid": True,
                         "instruction_sources": [block_source(subject, source), proof]}
                if condition:
                    field["applicability"] = condition
                if options:
                    field.update(semantic_kind="consent", selection_mode="single_mark",
                                 option_sources=[block_source(choices[0], source)])
                    field["instruction_sources"].append(block_source(choices[0], source))
                field["instructions"] = "\n".join(s["evidence"] for s in field["instruction_sources"])
                field["instruction_source"] = field["instruction_sources"][0]
                fields.append(field)
            if not valid:
                break
        if valid:
            # Reference text does not identify which earlier mark(s) it inherits.
            # Preserve the ambiguity; never copy an earlier person's consent.
            for reference in references:
                signature = next(f for f in fields if f["layout"]["row"] == reference["input_cell"]["row"] and f["field_type"] == "signature")
                signature.setdefault("constraint_issues", []).append("consent_reference_requires_original_review")
                signature["instruction_sources"].append(reference["source"])
                signature["instructions"] += "\n" + reference["source"]["evidence"]
            restored.append({"table": table, "fields": fields, "references": references, "blocks": native})
    return restored
