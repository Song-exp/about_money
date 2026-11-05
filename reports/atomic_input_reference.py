"""Source-authored atomic contracts for selected compound HWP inputs.

This is a bounded reference. No production restoration or predicted field
inventory is used to build the gold; visual placement remains unverified.
"""
from reports.form_semantic_reference import SOURCE_SHA256

# field-label record, source rules, (semantic key, source record, destination
# text/list record, kind, unit). Component counts come from the original grammar.
GROUPS = [
    {"id": "academic", "label": 515, "rules": [520, 524], "atoms": [
        ("school", 520, "text", 520, "text", ""), ("campus", 520, "text", 520, "text", ""),
        ("department", 520, "text", 520, "text", ""), ("grade", 520, "text", 520, "text", ""),
        ("semester", 520, "text", 520, "text", "")]},
    {"id": "previous_grade", "label": 529, "rules": [529, 539, 544, 559, 564], "atoms": [
        ("credits", 539, "cell", 557, "nonnegative_number", "학점"),
        ("gpa", 544, "cell", 562, "gpa", ""), ("scale", 564, "cell", 562, "gpa_scale", "")]},
    {"id": "total_grade", "label": 534, "rules": [534, 549, 554, 569, 574], "atoms": [
        ("credits", 549, "cell", 567, "nonnegative_number", "학점"),
        ("gpa", 554, "cell", 572, "gpa", ""), ("scale", 574, "cell", 572, "gpa_scale", "")]},
    {"id": "first_recipient", "label": 704, "rules": [695, 699, 704, 708, 713, 718], "person_row": 11, "atoms": [
        ("name", 704, "cell", 711, "text", ""), ("relation", 708, "cell", 711, "text", ""),
        ("diagnosis", 718, "cell", 721, "text", "")]},
    {"id": "second_recipient", "label": 749, "rules": [695, 699, 749, 753, 758, 763], "person_row": 12, "atoms": [
        ("name", 749, "cell", 756, "text", ""), ("relation", 753, "cell", 756, "text", ""),
        ("diagnosis", 763, "cell", 766, "text", "")]},
    {"id": "siblings", "label": 651, "rules": [651], "atoms": [
        ("male_count", 651, "text", 651, "nonnegative_integer", ""),
        ("female_count", 651, "text", 651, "nonnegative_integer", ""),
        ("birth_order", 651, "text", 651, "positive_integer", "")]},
    {"id": "net_family_income", "label": 666, "rules": [666, 670, 675], "atoms": [
        ("monthly_net_income", 675, "text", 675, "amount", "만원")]},
    {"id": "income_sources", "label": 680, "rules": [680, 685], "atoms": [
        ("government_income", 685, "text", 685, "amount", "원"),
        ("work_income", 685, "text", 685, "amount", "원"),
        ("donation_income", 685, "text", 685, "amount", "원")]},
    {"id": "other_family", "label": 656, "rules": [656, 661], "condition": 661, "atoms": [
        ("other_family", 661, "text", 661, "text", "")]},
]


def build_atomic_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated original")
    section = inventory["sections"][0]
    texts = {t["record"]: t for t in section["texts"]}
    lists = {r["record"]: r for r in section["list_records"]}
    groups = []
    for definition in GROUPS:
        group = {"id": definition["id"], "label_source": texts[definition["label"]],
                 "rule_sources": [texts[r] for r in definition["rules"]], "atoms": [],
                 "person_row": definition.get("person_row"), "condition_source": texts.get(definition.get("condition"))}
        for key, record, destination_kind, destination_record, kind, unit in definition["atoms"]:
            destination = texts[destination_record] if destination_kind == "text" else lists[destination_record]
            if destination_kind == "cell" and (destination["kind"] != "cell_geometry_candidate" or len(destination["geometry_candidates"]) != 1):
                raise ValueError("atomic cell destination is not independently unambiguous")
            group["atoms"].append({"id": f"{group['id']}.{key}", "component": key, "source": texts[record],
                "destination_kind": destination_kind, "destination": destination, "kind": kind, "unit": unit,
                "options": ["4.5", "4.3"] if kind == "gpa_scale" else [],
                "scale_component": "scale" if kind == "gpa" else None,
                "visual_placement_verified": False})
        groups.append(group)
    return {"version": "donghaeng-atomic-reference-v1", "source_sha256": SOURCE_SHA256, "groups": groups,
        "scope": "nine_selected_compound_groups_not_all_original_inputs", "complete_atomic_gold": False,
        "independent_human_review_complete": False, "whole_form_complete": False}


def record_of(proof):
    try:
        return int((proof or {})["location"].rsplit("record:", 1)[1])
    except (KeyError, ValueError, IndexError):
        return None


def audit_atomic_inputs(gold, fields):
    checks = []
    for group in gold["groups"]:
        actual = [f for f in fields if record_of(f.get("source")) == group["label_source"]["record"]]
        if len(actual) != 1:
            checks.extend({"atom_id": a["id"], "status": "requires_review", "issues": ["group_field_missing_or_duplicate"]} for a in group["atoms"])
            continue
        field = actual[0]
        slots = (field.get("input_spec") or {}).get("slots", [])
        common = []
        if field.get("field_type") != "text" or field.get("required") is not None or field.get("role") != "unknown" or field.get("manual_spec") or field.get("choice_spec"):
            common.append("collection_contract_changed")
        if field.get("source", {}).get("sha256") != gold["source_sha256"]:
            common.append("field_from_different_source")
        if field.get("evidence") != group["label_source"]["text"]:
            common.append("field_label_evidence_changed")
        if {s.get("key") for s in slots} != {a["component"] for a in group["atoms"]} or len(slots) != len(group["atoms"]):
            common.append("compound_components_missing_duplicate_or_extra")
        rules = field.get("instruction_sources", [])
        if not all(any(record_of(p) == rule["record"] and p.get("evidence") == rule["text"] and p.get("sha256") == gold["source_sha256"]
                       for p in rules) for rule in group["rule_sources"]):
            common.append("source_rules_missing")
        condition = field.get("applicability") or {}
        if group["condition_source"]:
            original = group["condition_source"]
            if (condition.get("mode") != "user_confirmation" or condition.get("discard_facts_on_change") is not True or
                record_of(condition.get("source")) != original["record"] or condition.get("evidence") != original["text"] or
                condition.get("source", {}).get("evidence") != original["text"] or
                condition.get("source", {}).get("sha256") != gold["source_sha256"] or not condition.get("id")):
                common.append("explicit_condition_missing_or_changed")
        elif condition:
            common.append("condition_not_stated_for_this_group")
        if group["person_row"] is not None:
            if (record_of((field.get("subject") or {}).get("source")) != 695 or
                (field.get("subject") or {}).get("source", {}).get("sha256") != gold["source_sha256"] or
                field.get("layout", {}).get("row") != group["person_row"] or
                field.get("layout", {}).get("table") != f"BodyText/Section0/table:{group['label_source']['table_parent']}" or
                (field.get("input_spec") or {}).get("person_row") is not True):
                common.append("recipient_scope_changed")
        for atom in group["atoms"]:
            issues = list(common)
            matches = [s for s in slots if s.get("key") == atom["component"]]
            if len(matches) != 1:
                issues.append("component_missing_or_duplicate")
            else:
                slot, destination = matches[0], atom["destination"]
                if record_of(slot.get("source")) != atom["source"]["record"] or slot.get("source", {}).get("sha256") != gold["source_sha256"] or slot.get("source", {}).get("evidence") != atom["source"]["text"]:
                    issues.append("component_source_changed")
                if slot.get("kind", "text") != atom["kind"] or slot.get("unit", "") != atom["unit"] or slot.get("options", []) != atom["options"] or slot.get("scale_key") != atom["scale_component"]:
                    issues.append("value_kind_unit_or_scale_changed")
                if atom["destination_kind"] == "text":
                    if record_of({"location": slot.get("destination", "")}) != destination["record"]:
                        issues.append("paragraph_destination_changed")
                else:
                    geometry = destination["geometry_candidates"][0]
                    expected = f"BodyText/Section0/table:{destination['table_parent']}/row:{geometry['row'] + 1}/cell:{geometry['column'] + 1}"
                    if slot.get("destination") != expected:
                        issues.append("cell_destination_changed")
                    cells = [c for c in field.get("input_cell_sources", []) if c.get("record") == destination["record"]]
                    if (len(cells) != 1 or cells[0].get("payload_sha256") != destination.get("payload_sha256") or
                        any(cells[0].get(k) != geometry[k] for k in ("row", "column", "row_span", "column_span")) or
                        cells[0].get("table") != f"BodyText/Section0/table:{destination['table_parent']}" or
                        cells[0].get("sha256") != gold["source_sha256"] or cells[0].get("cell_location") != expected):
                        issues.append("cell_byte_proof_missing_or_changed")
            checks.append({"atom_id": atom["id"], "field_id": field.get("id"), "status": "requires_review" if issues else "bound",
                           "issues": list(dict.fromkeys(issues))})
    return {"scope": gold["scope"], "atoms": checks, "checked_atoms": len(checks),
            "unresolved_atoms": sum(c["status"] != "bound" for c in checks), "complete_atomic_gold": False,
            "independent_human_review_complete": False, "whole_form_complete": False}
