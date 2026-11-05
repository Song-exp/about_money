"""Audit source checkbox cells independently of the model's proposed fields.

This detects gaps; it does not infer requiredness, exclusivity or collect values
inside choice captions. Unsupported compound controls keep original review.
"""
import re

from app.services.form_consent import MARKERS, original_options
from app.services.form_semantics import block_source
from app.services.document_layout import compact

AUDIT_VERSION = "native-controls-v1"
_VALUE = re.compile(r"[（(][^()（）]*\s{2,}[^()（）]*[）)]")


def audit_controls(blocks, fields, source=None):
    from app.services.form_choice_values import native_value_controls
    from app.services.form_profile_inputs import native_profile_inputs
    specs = {c["field"]["option_sources"][0]["location"]: c["field"]["choice_spec"]
             for c in native_value_controls(blocks, source)}
    specs.update({f["option_sources"][0]["location"]: f["choice_spec"]
                  for group in native_profile_inputs(blocks, source) for f in group["fields"] if f.get("choice_spec")})
    cells = {}
    for block in blocks:
        if block.get("method") == "native" and block.get("cell_location"):
            cells.setdefault(block["cell_location"], []).append(block)
    controls = []
    for location, peers in cells.items():
        choices = [b for b in peers if re.search(f"[{MARKERS}]", b["text"]) and original_options(b["text"])]
        if not choices:
            continue
        options = [o for b in choices for o in original_options(b["text"])]
        row = [b for b in blocks if b.get("method") == "native" and b.get("table") == peers[0].get("table")
               and b.get("row") == peers[0].get("row")]
        multiple = [b for b in row if re.search(r"중복\s*(?:체크|선택)\s*가능|복수\s*선택\s*가능", b["text"])]
        matches = []
        for field in fields:
            if field.get("field_type") != "checkbox":
                continue
            proofs = [field.get("source") or {}, *field.get("option_sources", [])]
            # All paragraphs of the choice cell must be proven, not just its row.
            if all(any(p.get("location") == b["location"] and
                       p.get("sha256") == (source or {}).get("sha256") and
                       p.get("text_start") == b["start"] and p.get("text_end") == b["end"]
                       for p in proofs) for b in choices):
                matches.append(field)
        issues = []
        if len(matches) != 1:
            issues.append("native_choice_missing_or_ambiguous")
        elif [compact(o) for o in matches[0].get("options", [])] != [compact(o) for o in options]:
            issues.append("native_choice_options_mismatch")
        supported = len(matches) == 1 and matches[0].get("choice_spec") == specs.get(choices[0]["location"]) and matches[0].get("choice_spec") is not None
        if not supported and any(not re.search(f"[{MARKERS}]", m.group()) for b in choices for m in _VALUE.finditer(b["text"])):
            issues.append("choice_inline_values_require_review")
        if multiple and not supported:
            # The current writing workflow only validates one whole option.
            issues.append("multiple_choice_collection_requires_review")
        controls.append({"cell_location": location, "sources": [block_source(b, source) for b in choices],
                         "options": options, "field_ids": [f["id"] for f in matches],
                         "multiple_selection_sources": [block_source(b, source) for b in multiple],
                         "issues": issues, "status": "requires_original_review" if issues else "bound"})
    return {"version": AUDIT_VERSION, "controls": controls,
            "unresolved": sum(bool(c["issues"]) for c in controls),
            "scope": "native_checkbox_cells_only_not_whole_form"}
