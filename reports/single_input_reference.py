"""Source-authored cover/contact/guardian/current-care contracts."""
from reports.atomic_input_reference import record_of, build_atomic_gold
from reports.choice_repeat_reference import proof_matches, build_choice_repeat_gold
from reports.submission_consent_reference import cell_matches, build_submission_consent_gold
from reports.form_semantic_reference import SOURCE_SHA256

# label, destination list/text record, (key, source text record), subject
INPUTS = [(40, 43, [("school", 40)], None), (49, 52, [("department", 49)], None),
    (58, 61, [("grade", 63), ("semester", 63)], None), (68, 71, [("name", 68)], None),
    (455, 458, [("name", 455)], None), (474, 477, [("phone", 474)], None),
    (483, 486, [("email", 483)], None), (492, 495, [("address", 492)], None),
    (601, 604, [("value", 601)], 596), (610, 613, [("value", 610)], 596),
    (619, 622, [("value", 619)], 596), (628, 631, [("value", 628)], 596),
    (794, 799, [("current_care", 794)], None)]
UNFINISHED = [(232, "checklist_grade"), (317, "checklist_income_bracket"),
    (637, "cohabiting_family_marks"), (806, "economic_support_branch"),
    (1070, "essay_family_care"), (1083, "essay_prior_support"),
    (1111, "essay_scholarship_use"), (1120, "essay_career")]


def build_single_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated original")
    texts = {t["record"]: t for t in inventory["sections"][0]["texts"]}
    lists = {c["record"]: c for c in inventory["sections"][0]["list_records"]}
    groups = []
    for label, destination, atoms, subject in INPUTS:
        cell = lists.get(destination)
        verified = cell is not None and cell["kind"] == "cell_geometry_candidate" and len(cell["geometry_candidates"]) == 1
        rules = [texts[r] for r in ([subject, label] if subject else [label, 63] if label == 58 else [794, 799] if label == 794 else [label])]
        groups.append({"id": f"single.{label}", "label_source": texts[label],
            "subject_source": texts.get(subject), "rules": rules,
            "destination": cell if cell is not None else texts[destination],
            "destination_kind": "cell" if cell is not None else "paragraph",
            "independent_geometry_proven": verified if cell is not None else None,
            "collection_mode": "generic_value" if subject else "structured_slots",
            "atoms": [{"key": key, "source": texts[r], "kind": "text", "unit": ""} for key, r in atoms]})
    # Scope reconciliation is source-derived; it is not a complete atomic gold.
    catalog = []
    def entry(source, subject, scope):
        catalog.append({"label_source": source, "subject_record": subject, "contract_scope": scope})
    for g in build_atomic_gold(inventory)["groups"]:
        entry(g["label_source"], 695 if g["person_row"] is not None else None, "atomic")
    choices = build_choice_repeat_gold(inventory)
    for g in choices["choice_groups"]:
        entry(g["label_source"], None, "choice_repeat")
    entry(choices["repeat_group"]["label_source"], None, "choice_repeat")
    submission = build_submission_consent_gold(inventory)
    for g in submission["checklist"]:
        entry(g["labels"][0], None, "submission_consent")
    for g in submission["consents"]:
        entry(g["labels"][0], None, "submission_consent")
    for g in submission["people"]:
        entry(g["header"], g["subject"]["record"], "submission_consent")
    for g in submission["originals"]:
        entry(g["source"], 1135 if g["source"]["record"] == 1493 else None, "submission_consent")
    for g in groups:
        entry(g["label_source"], g["subject_source"]["record"] if g["subject_source"] else None, "single")
    for record, scope in UNFINISHED:
        entry(texts[record], None, f"pending:{scope}")
    identities = [(e["label_source"]["record"], e["subject_record"]) for e in catalog]
    if len(set(identities)) != len(identities):
        raise ValueError("source scope overlaps")
    return {"version": "donghaeng-single-reference-v1", "source_sha256": SOURCE_SHA256, "groups": groups,
        "scope_catalog": catalog, "unproven_destination_records": [495],
        "complete_atomic_gold": False, "whole_form_complete": False, "independent_human_review_complete": False}


def audit_single_inputs(gold, fields):
    checks, sha = [], gold["source_sha256"]
    for g in gold["groups"]:
        actual = [f for f in fields if record_of(f.get("source")) == g["label_source"]["record"]]
        issues = []
        if len(actual) != 1:
            issues.append("missing_or_duplicate_group")
        else:
            f = actual[0]
            if (f.get("field_type") != "text" or f.get("required") is not None or f.get("role") != "unknown" or
                f.get("manual_spec") or f.get("choice_spec") or f.get("applicability") or
                f.get("label") != g["label_source"]["text"] or not proof_matches(f.get("source"), g["label_source"], sha)):
                issues.append("collection_or_title_changed")
            subject = (f.get("subject") or {}).get("source")
            if g["subject_source"]:
                if not proof_matches(subject, g["subject_source"], sha) or f["subject"].get("label") != g["subject_source"]["text"]:
                    issues.append("guardian_subject_changed")
            elif subject:
                issues.append("invented_subject")
            proofs = f.get("instruction_sources", [])
            if len(proofs) != len(g["rules"]) or any(not proof_matches(a, b, sha) for a, b in zip(proofs, g["rules"])):
                issues.append("source_rules_changed")
            if f.get("instructions") != "\n".join(r["text"] for r in g["rules"]):
                issues.append("visible_rules_changed")
            slots = (f.get("input_spec") or {}).get("slots", [])
            if g["collection_mode"] == "generic_value":
                if f.get("input_spec"):
                    issues.append("guardian_value_contract_changed")
            elif len(slots) != len(g["atoms"]) or any(s.get("key") != a["key"] or
                s.get("kind") != "text" or s.get("unit") != "" or not proof_matches(s.get("source"), a["source"], sha)
                for s, a in zip(slots, g["atoms"])):
                issues.append("component_contract_changed")
            if g["destination_kind"] == "cell":
                if not g["independent_geometry_proven"]:
                    issues.append("source_cell_geometry_requires_review")
                else:
                    if not cell_matches(f.get("input_cell_source") or {}, g["destination"], sha):
                        issues.append("cell_proof_changed")
                    geometry = g["destination"]["geometry_candidates"][0]
                    layout = f.get("layout") or {}
                    expected_address = f"BodyText/Section0/table:{g['destination']['table_parent']}/row:{geometry['row']+1}/cell:{geometry['column']+1}"
                    if (layout.get("cell_location") != expected_address or
                        any(layout.get(k) != geometry[k] for k in ("row", "column", "row_span", "column_span")) or
                        any(s.get("destination") != expected_address for s in slots)):
                        issues.append("destination_or_visible_scope_changed")
            elif any(record_of({"location": s.get("destination", "")}) != g["destination"]["record"] for s in slots):
                issues.append("care_paragraph_destination_changed")
        checks.append({"id": g["id"], "atoms": len(g["atoms"]), "issues": issues,
                       "status": "requires_review" if issues else "matched"})
    return {"checks": checks, "groups": len(checks), "atoms": sum(c["atoms"] for c in checks),
        "unresolved_groups": sum(bool(c["issues"]) for c in checks), "whole_form_complete": False}


def reconcile_scopes(gold, fields):
    expected = {(e["label_source"]["record"], e["subject_record"]): e for e in gold["scope_catalog"]}
    actual = [(record_of(f.get("source")), record_of((f.get("subject") or {}).get("source"))) for f in fields]
    return {"catalog_entries": len(expected), "covered_group_contracts": sum(not e["contract_scope"].startswith("pending:") for e in expected.values()),
        "pending_group_contracts": [e for e in expected.values() if e["contract_scope"].startswith("pending:")],
        "missing_entries": [key for key in expected if key not in actual], "unexpected_entries": [key for key in actual if key not in expected],
        "duplicate_entries": [key for key in set(actual) if actual.count(key) > 1],
        "scope": "source_group_membership_not_full_atomic_accuracy", "whole_form_complete": False}
