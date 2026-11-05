"""Keep ambiguous original consent references separate from recovered inputs."""
from app.services.document_layout import compact
from app.services.form_consent import native_consent_controls, consent_context
from app.services.form_semantics import block_source

VERSION = "native-reference-audit-v1"


def audit_references(blocks, source):
    blocks = [{**b, "block_id": b.get("block_id", f"b{index:04d}")} for index, b in enumerate(blocks)]
    controls = native_consent_controls(blocks)
    checks = []
    for reference in blocks:
        if compact(reference["text"]) != "위표시와같음":
            continue
        # A previous position is only a candidate, never a reference definition.
        candidates = []
        for control in controls:
            label = next(b for b in blocks if b["block_id"] == control["label_block_id"])
            if label["end"] > reference["start"]:
                continue
            context = consent_context(label, blocks, source)
            candidates.append({"label": control["label"], "source": block_source(label, source),
                "label_sources": [block_source(b, source) for b in blocks if b.get("method") == "native" and b.get("cell_location") == label.get("cell_location")],
                "options": control["options"], "option_sources": [block_source(b, source) for b in blocks if b["block_id"] in control["option_block_ids"]],
                "section": context["kind"] if context else None,
                "section_source": context["source"] if context else None})
        subjects = [b for b in blocks if b.get("table") == reference.get("table") and b.get("row") == reference.get("row")
            and b.get("column") == 0 and b.get("method") == "native"] if reference.get("table") else []
        checks.append({"source": block_source(reference, source), "source_method": reference.get("method"),
            "subject_source": block_source(subjects[0], source) if len(subjects) == 1 else None,
            "candidates": candidates, "status": "requires_original_review", "target_field_ids": [], "automatic_inheritance": False,
            "issues": ["reference_target_not_explicit"] + ([] if reference.get("method") == "native" else ["reference_source_not_native"])})
    return {"version": VERSION, "controls": checks, "unresolved": len(checks), "scope": "original_same_as_above_references_not_whole_form"}
