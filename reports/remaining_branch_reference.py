"""Independent original contracts for checklist values and remaining marks."""
from reports.atomic_input_reference import record_of
from reports.choice_repeat_reference import proof_matches
from reports.form_semantic_reference import SOURCE_SHA256


def build_branch_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated original")
    texts = {t["record"]: t for t in inventory["sections"][0]["texts"]}
    prompts = []
    for record, rules, atoms, condition in (
        (232, [219, 228, 232, 240], [("gpa", "gpa", "", None), ("scale", "gpa_scale", "", None)], []),
        (317, [308, 317, 323, 276, 383], [("income_bracket", "checklist_income_bracket", "분위", 2)], [308, 276, 383])):
        prompts.append({"id": f"prompt.{record}", "source": texts[record], "rules": [texts[r] for r in rules],
            "atoms": [{"key": key, "kind": kind, "unit": unit, "maximum": maximum,
                       "options": ["4.5", "4.3"] if key == "scale" else []} for key, kind, unit, maximum in atoms],
            "condition_sources": [texts[r] for r in condition], "condition_mode": "user_selected_document" if condition else None,
            "discard_facts_on_change": True if condition else None, "paragraph_not_verified_cell": True})
    family = {"id": "family.637", "source": texts[637], "rules": [texts[r] for r in (637, 642, 646)],
        "option_source": texts[642], "mark_source": texts[646], "options": ["조부", "조모", "부", "모"],
        "allow_empty": True, "mark": "O"}
    support = {"id": "support.806", "source": texts[806], "rules": [texts[r] for r in (806, 810, 815, 820, 825)],
        "options": ["①수급자", "②차상위"], "option_source": texts[815], "condition_source": texts[820],
        "evidence_source": texts[810], "evidence_keys": ["recipient_evidence", "near_poverty_evidence"],
        "empty_value": {"key": "support_bracket", "kind": "integer_upper_bound", "unit": "구간", "maximum": 2,
                        "source": texts[825]}, "evidence_is_user_report_not_file_verification": True}
    return {"version": "donghaeng-remaining-branch-reference-v1", "source_sha256": SOURCE_SHA256,
        "prompts": prompts, "family": family, "support": support,
        "scope": "two_checklist_prompts_family_marks_support_branch", "complete_atomic_gold": False,
        "whole_form_complete": False, "independent_human_review_complete": False}


def audit_branches(gold, fields):
    sha, checks = gold["source_sha256"], []

    def proofs(actual, expected):
        return len(actual) == len(expected) and all(proof_matches(a, e, sha) for a, e in zip(actual, expected))

    for group in [*gold["prompts"], gold["family"], gold["support"]]:
        candidates = [f for f in fields if record_of(f.get("source")) == group["source"]["record"]]
        issues = []
        if len(candidates) != 1:
            issues.append("group_missing_or_duplicate")
        else:
            f = candidates[0]
            if (not proof_matches(f.get("source"), group["source"], sha) or f.get("required") is not None or
                f.get("role") != "unknown" or f.get("manual_spec") or f.get("subject") or
                not proofs(f.get("instruction_sources", []), group["rules"]) or
                f.get("instructions") != "\n".join(r["text"] for r in group["rules"])):
                issues.append("source_scope_or_rules_changed")
            if group in gold["prompts"]:
                spec, dest = f.get("input_spec") or {}, f.get("original_destination") or {}
                if (f.get("field_type") != "text" or f.get("choice_spec") or
                    spec.get("paragraph_template") is not True or spec.get("original_layout_review_required") is not True or
                    dest.get("kind") != "paragraph_in_cell_template" or dest.get("visual_placement_verified") is not False or
                    dest.get("cell_geometry_verified") is not False or dest.get("contains_controls") is not True or
                    not proof_matches(dest.get("source"), group["source"], sha) or f.get("input_cell_source")):
                    issues.append("paragraph_review_contract_changed")
                slots = spec.get("slots", [])
                if len(slots) != len(group["atoms"]) or any(s.get("key") != a["key"] or s.get("kind") != a["kind"] or
                    s.get("unit") != a["unit"] or s.get("options", []) != a["options"] or
                    s.get("maximum") != a["maximum"] or not proof_matches(s.get("source"), group["source"], sha) or
                    record_of({"location": s.get("destination", "")}) != group["source"]["record"]
                    for s, a in zip(slots, group["atoms"])):
                    issues.append("paragraph_value_contract_changed")
                if group["source"]["record"] == 232 and slots and slots[0].get("scale_key") != "scale":
                    issues.append("gpa_scale_link_changed")
                condition = f.get("applicability") or {}
                if group["condition_sources"]:
                    if (condition.get("mode") != group["condition_mode"] or
                        not proof_matches(condition.get("source"), group["condition_sources"][0], sha) or
                        not proofs(condition.get("sources", []), group["condition_sources"])):
                        issues.append("selected_document_condition_changed")
                    if condition.get("discard_facts_on_change") is not True:
                        issues.append("deselected_document_retains_old_answer")
                elif condition:
                    issues.append("condition_added_to_grade")
            else:
                spec = f.get("choice_spec") or {}
                if (f.get("field_type") != "checkbox" or f.get("selection_mode") != "marks" or
                    f.get("options") != group["options"] or spec.get("allow_empty") is not True or
                    spec.get("multiple_explicit") is not False or spec.get("multiple_sources") != [] or
                    f.get("input_spec") or f.get("applicability")):
                    issues.append("mark_selection_contract_changed")
                choices = spec.get("choices", [])
                if len(choices) != len(group["options"]) or any(c.get("caption") != caption or
                    not proof_matches(c.get("source"), group["option_source"], sha)
                    for c, caption in zip(choices, group["options"])):
                    issues.append("choice_caption_or_source_changed")
                if group["id"] == "family.637":
                    if (spec.get("mark") != "O" or not proof_matches(spec.get("mark_source"), group["mark_source"], sha) or
                        any(c.get("values") != [] for c in choices) or spec.get("empty_values")):
                        issues.append("family_original_mark_changed")
                else:
                    empty = spec.get("empty_values", [])
                    if not proofs(spec.get("condition_sources", []), [group["condition_source"]]):
                        issues.append("support_branch_condition_changed")
                    expected = group["empty_value"]
                    if (len(empty) != 1 or any(empty[0].get(k) != expected[k] for k in ("key", "kind", "unit", "maximum")) or
                        not proof_matches(empty[0].get("source"), expected["source"], sha) or
                        record_of({"location": empty[0].get("destination", "")}) != expected["source"]["record"]):
                        issues.append("unselected_support_bracket_changed")
                    for c, key in zip(choices, group["evidence_keys"]):
                        values = c.get("values", [])
                        if (len(values) != 1 or values[0].get("key") != key or values[0].get("kind") != "evidence_availability" or
                            values[0].get("unit") != "" or values[0].get("destination") is not None or
                            not proof_matches(values[0].get("source"), group["evidence_source"], sha)):
                            issues.append("support_evidence_report_became_original_value")
        checks.append({"id": group["id"], "issues": issues, "status": "requires_review" if issues else "matched"})
    return {"checks": checks, "unresolved_groups": sum(bool(c["issues"]) for c in checks),
            "whole_form_complete": False, "complete_atomic_gold": False}
