"""Source-authored, bounded contracts; no production form restoration calls."""
from reports.atomic_input_reference import record_of
from reports.choice_repeat_reference import proof_matches
from reports.form_semantic_reference import SOURCE_SHA256

# Annotated against the original source records, before reading predictions.
CHECKLIST = [
    ([136, 140], 143, [150, 154], []), ([168], 171, [177, 181], [186]),
    ([196], 199, [], [209]), ([219], 222, [228, 232, 240], [245]),
    ([281, 285], 288, [294, 298], [303]), ([308], 311, [317, 323], [328]),
    ([338, 342], 345, [351, 355, 359, 363, 367], [372]),
]
CONSENTS = [(1207, 1211, 1216, 1157, "collection_use"),
            (1262, 1266, 1271, 1227, "identity_processing"),
            (1370, 1374, 1379, 1282, "provision_inquiry")]
PEOPLE = [(1422, [1425, 1429, None, 1438], "applicant"),
          (1444, [1447, 1451, 1455, 1460], "unknown"),
          (1466, [1469, 1473, 1477, 1482], "unknown")]
ORIGINAL = [(464, "text", None, None, [464, 469]),
            (1035, "text", None, "paragraph_template", [1035, 1042]),
            (1042, "signature", True, "paragraph_template", [1035, 1042]),
            (1493, "text", None, "paragraph_in_cell_template", [1135, 1386, 1493, 1500])]


def build_submission_consent_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated original")
    section = inventory["sections"][0]
    texts = {t["record"]: t for t in section["texts"]}
    lists = {c["record"]: c for c in section["list_records"]}

    def cell(record):
        c = lists[record]
        if c["kind"] != "cell_geometry_candidate" or len(c["geometry_candidates"]) != 1:
            raise ValueError("destination is not independently unambiguous")
        return c

    checklist = []
    for row, (labels, destination, notes, dates) in enumerate(CHECKLIST, 1):
        economic = row in (5, 6)
        rules = [79, 83, 87, *notes, *dates, 383, 387, 391, *([276] if economic else [])]
        checklist.append({"id": f"checklist.{row}", "labels": [texts[r] for r in labels],
            "cell": cell(destination), "rules": [texts[r] for r in rules], "row": row,
            "headers": [texts[r] for r in (108, 112)], "example": texts[145] if row == 1 else None,
            "economic_review": economic, "status_options": ["준비됨", "준비 중"] + (["해당 없음"] if economic else [])})
    consents = [{"id": f"consent.{kind}", "labels": [texts[label], texts[second]],
        "option_source": texts[option], "heading": texts[heading], "kind": kind,
        "context": [t for t in section["texts"] if heading <= t["record"] <= option],
        "options": ["동의함", "동의하지 않음"]} for label, second, option, heading, kind in CONSENTS]
    people = []
    for row, (subject, destinations, role) in enumerate(PEOPLE, 1):
        for column, (header, destination, kind) in enumerate(zip(
                (1402, 1407, 1412, 1417), destinations, ("text", "text", "checkbox", "signature")), 1):
            if destination is None:  # Printed reference 1435 is not an independent input.
                continue
            people.append({"id": f"person.{row}.{column}", "header": texts[header], "subject": texts[subject],
                "cell": cell(destination), "row": row, "column": column, "kind": kind, "role": role,
                "condition": texts[1466] if row == 3 else None,
                "option_source": texts[1457 if row == 2 else 1479] if kind == "checkbox" else None})
    originals = [{"id": f"original.{record}", "source": texts[record], "kind": kind, "required": required,
        "destination_kind": destination, "rules": [texts[r] for r in rules],
        "cell": cell(467) if record == 464 else None} for record, kind, required, destination, rules in ORIGINAL]
    return {"version": "donghaeng-submission-consent-reference-v1", "source_sha256": SOURCE_SHA256,
        "checklist": checklist, "consents": consents, "people": people, "originals": originals,
        "reference_source": texts[1435], "reference_automatic_inheritance": False,
        "scope": "seven_checklist_three_consent_eleven_person_four_original_items",
        "whole_form_complete": False, "complete_atomic_gold": False, "independent_human_review_complete": False}


def cell_matches(actual, original, sha):
    geometry = original["geometry_candidates"][0]
    return (actual.get("sha256") == sha and actual.get("record") == original["record"] and
        actual.get("payload_sha256") == original["payload_sha256"] and
        actual.get("table") == f"BodyText/Section0/table:{original['table_parent']}" and
        all(actual.get(k) == geometry[k] for k in ("row", "column", "row_span", "column_span")) and
        actual.get("has_control") is original["has_control"] and
        actual.get("blank") is original["blank_candidate"] and
        actual.get("paragraph_char_counts") == original["paragraph_char_counts"] and
        [record_of({"location": p}) for p in actual.get("text_locations", [])] == original["content_records"])


def audit_submission_consent(gold, fields, coverage=None):
    sha, checks = gold["source_sha256"], []

    def check(item, candidates, validate):
        issues = ["missing_or_duplicate_item"] if len(candidates) != 1 else validate(candidates[0])
        checks.append({"id": item["id"], "status": "requires_review" if issues else "matched", "issues": issues})

    def by_source(source):
        return [f for f in fields if record_of(f.get("source")) == source["record"]]

    def proofs(actual, expected):
        return len(actual) == len(expected) and all(proof_matches(a, e, sha) for a, e in zip(actual, expected))

    for item in gold["checklist"]:
        def validate(f):
            problems = []
            prep, spec = f.get("preparation_spec") or {}, f.get("input_spec") or {}
            if (f.get("semantic_kind") != "submission_checklist" or f.get("field_type") != "text" or
                f.get("required") is not None or f.get("role") != "unknown" or
                f.get("label") != " ".join(t["text"] for t in item["labels"]) or
                not proof_matches(f.get("source"), item["labels"][0], sha) or
                (f.get("manual_spec") or {}).get("mode") != "original_submission_check"):
                problems.append("preparation_collection_policy_changed")
            if (prep.get("user_report_only") is not True or prep.get("file_verified") is not False or
                prep.get("automatic_marking") is not False or prep.get("original_marks") != ["O", "√"] or
                prep.get("economic_rule_review_required") is not item["economic_review"] or
                prep.get("example_only") is not bool(item["example"])):
                problems.append("unverified_completion_or_rule_resolution")
            if not cell_matches(f.get("input_cell_source") or {}, item["cell"], sha):
                problems.append("submission_mark_destination_changed")
            if not proofs(prep.get("document_sources", []), item["labels"]) or not proofs(prep.get("header_sources", []), item["headers"]):
                problems.append("submission_document_or_header_changed")
            if item["example"] and not proof_matches(prep.get("example_source"), item["example"], sha):
                problems.append("example_source_changed")
            if not item["example"] and prep.get("example_source") is not None:
                problems.append("example_copied_to_blank_row")
            if not proofs(f.get("instruction_sources", []), item["rules"]):
                problems.append("submission_rules_changed")
            if f.get("instructions") != "\n".join(r["text"] for r in item["rules"]):
                problems.append("submission_rules_not_visible")
            slots, ready = spec.get("slots", []), spec.get("ready_slots", [])
            expected = [("preparation_status", "preparation_status", item["status_options"], item["labels"][0]),
                        ("rules_ack", "document_rule_confirmation", ["원문 조건 확인"], item["rules"][2])]
            if spec.get("preparation_only") is not True or len(slots) != 1 or len(ready) != 1:
                problems.append("preparation_slot_contract_changed")
            elif any(s.get("key") != key or s.get("kind") != kind or s.get("options") != options or
                     s.get("service_only") is not True or s.get("destination") is not None or
                     not proof_matches(s.get("source"), source, sha)
                     for s, (key, kind, options, source) in zip(slots + ready, expected)):
                problems.append("preparation_service_value_became_original_mark")
            return problems
        check(item, by_source(item["labels"][0]), validate)
    for item in gold["consents"]:
        def validate(f):
            problems = []
            section, context = f.get("section") or {}, f.get("consent_context") or {}
            if (f.get("field_type") != "checkbox" or f.get("semantic_kind") != "consent" or
                f.get("required") is not True or f.get("role") != "unknown" or f.get("options") != item["options"] or
                f.get("input_spec") or f.get("choice_spec") or f.get("manual_spec") or f.get("applicability") or
                f.get("evidence") != "\n".join(t["text"] for t in item["labels"]) or
                f.get("label") != "\n".join(t["text"] for t in item["labels"]) or
                f.get("layout", {}).get("table") != f"BodyText/Section0/table:{item['labels'][0]['table_parent']}" or
                f.get("source", {}).get("sha256") != sha):
                problems.append("consent_choice_contract_changed")
            if (section.get("kind") != item["kind"] or not proof_matches(section.get("source"), item["heading"], sha) or
                not proof_matches(f.get("required_source"), item["heading"], sha) or
                not proofs(f.get("option_sources", []), [item["option_source"]])):
                problems.append("consent_section_or_choice_source_changed")
            if not proofs(context.get("sources", []), item["context"]) or context.get("text") != "\n".join(t["text"] for t in item["context"]):
                problems.append("consent_context_missing_or_crossed")
            return problems
        check(item, by_source(item["labels"][0]), validate)
    for item in gold["people"]:
        def validate(f):
            problems = []
            if (not proof_matches(f.get("source"), item["header"], sha) or
                not proof_matches((f.get("subject") or {}).get("source"), item["subject"], sha) or
                (f.get("subject") or {}).get("label") != item["subject"]["text"] or
                f.get("label") != item["header"]["text"] or
                f.get("field_type") != item["kind"] or f.get("role") != item["role"] or f.get("required") is not None or
                f.get("input_spec") or f.get("choice_spec") or f.get("manual_spec") or
                not cell_matches(f.get("input_cell_source") or {}, item["cell"], sha) or
                f.get("layout", {}).get("row") != item["row"] or f.get("layout", {}).get("column") != item["column"] or
                f.get("layout", {}).get("table") != "BodyText/Section0/table:1393"):
                problems.append("person_or_destination_changed")
            condition = f.get("applicability") or {}
            if item["condition"]:
                if (condition.get("mode") != "user_confirmation" or condition.get("discard_facts_on_change") is not True or
                    not proof_matches(condition.get("source"), item["condition"], sha)):
                    problems.append("extra_person_condition_changed")
            elif condition:
                problems.append("condition_leaked_to_other_person")
            if item["kind"] == "checkbox" and (f.get("options") != ["동의함"] or
                f.get("selection_mode") != "single_mark" or f.get("semantic_kind") != "consent" or
                not proofs(f.get("option_sources", []), [item["option_source"]])):
                problems.append("person_consent_options_changed")
            return problems
        candidates = [f for f in by_source(item["header"]) if record_of((f.get("subject") or {}).get("source")) == item["subject"]["record"]]
        check(item, candidates, validate)
    for item in gold["originals"]:
        def validate(f):
            problems = []
            manual, dest = f.get("manual_spec") or {}, f.get("original_destination") or {}
            if (not proof_matches(f.get("source"), item["source"], sha) or f.get("field_type") != item["kind"] or
                f.get("label") != item["source"]["text"] or
                f.get("required") is not item["required"] or manual.get("mode") != "original_only" or
                not proofs(manual.get("sources", []), item["rules"]) or f.get("input_spec") or f.get("choice_spec")):
                problems.append("original_only_policy_or_source_changed")
            if item["cell"]:
                if not cell_matches(f.get("input_cell_source") or {}, item["cell"], sha):
                    problems.append("identity_destination_changed")
            elif (dest.get("kind") != item["destination_kind"] or dest.get("visual_placement_verified") is not False or
                  not proof_matches(dest.get("source"), item["source"], sha) or f.get("input_cell_source")):
                problems.append("original_paragraph_destination_changed")
            if item["source"]["record"] == 1493 and not proof_matches((f.get("subject") or {}).get("source"), item["rules"][0], sha):
                problems.append("consent_date_subject_changed")
            return problems
        check(item, by_source(item["source"]), validate)
    # The applicant's printed reference must not become a copied consent input.
    reference_inputs = [f for f in fields if record_of(f.get("source")) == 1435 or
        (f.get("layout", {}).get("table") == "BodyText/Section0/table:1393" and
         f.get("layout", {}).get("row") == 1 and f.get("layout", {}).get("column") == 3)]
    checks.append({"id": "reference.1435", "status": "requires_review" if reference_inputs else "matched",
                   "issues": ["reference_became_independent_input"] if reference_inputs else []})
    if coverage is not None:
        audit = coverage.get("native_reference_audit") or {}
        controls = audit.get("controls", [])
        problems = []
        if len(controls) != 1:
            problems.append("reference_audit_missing_or_duplicate")
        else:
            control = controls[0]
            if (not proof_matches(control.get("source"), gold["reference_source"], sha) or
                not proof_matches(control.get("subject_source"), gold["people"][0]["subject"], sha) or
                control.get("status") != "requires_original_review" or control.get("target_field_ids") != [] or
                control.get("automatic_inheritance") is not False or control.get("source_method") != "native" or
                "reference_target_not_explicit" not in control.get("issues", []) or audit.get("unresolved") != 1):
                problems.append("ambiguous_reference_automatically_resolved")
            candidates = control.get("candidates", [])
            if len(candidates) != 3 or any(
                candidate.get("section") != item["kind"] or
                not proof_matches(candidate.get("section_source"), item["heading"], sha) or
                not proofs(candidate.get("option_sources", []), [item["option_source"]])
                for candidate, item in zip(candidates, gold["consents"])):
                problems.append("reference_candidates_changed")
        checks.append({"id": "reference.audit", "status": "requires_review" if problems else "matched", "issues": problems})
    return {"version": gold["version"], "checks": checks,
        "unresolved_contracts": sum(c["status"] != "matched" for c in checks),
        "printed_reference_still_requires_review": True, "complete_atomic_gold": False,
        "whole_form_complete": False, "independent_human_review_complete": False}
