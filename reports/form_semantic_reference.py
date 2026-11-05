"""Source-authored classifications for the five-page Donghaeng HWP fixture.

These record lists were annotated from original text and rendered pages. They
are not generated from predicted fields and are not a complete atomic gold.
"""
from collections import Counter

SOURCE_SHA256 = "28834b458a0dc397de06a91329eb6e5406e3228866ab51e05b1fcba5fbf2ca0f"
CLASSIFICATIONS = {
    "title": [22, 379, 395, 1052, 1135],
    "structural_label": [58, 98, 103, 108, 117, 122, 131, 163, 191, 214, 250, 255, 259, 263, 267, 271,
        333, 515, 529, 534, 579, 583, 587, 591, 596, 637, 680, 690, 695, 699, 830,
        1162, 1172, 1197, 1211, 1232, 1242, 1252, 1266, 1287, 1291, 1321, 1331, 1335, 1360, 1374,
        1397, 1402, 1407, 1412, 1417],
    "document_label": [136, 140, 168, 196, 219, 281, 285, 308, 338, 342],
    "input_label": [40, 49, 68, 415, 425, 434, 439, 444, 455, 474, 483, 492, 501, 539, 544, 549, 554,
        601, 610, 619, 628, 642, 651, 656, 666, 685, 704, 708, 718, 727, 749, 753, 763, 772,
        806, 820, 843, 848, 853, 858, 867, 872, 877, 1207, 1262, 1370],
    "input_template": [63, 232, 317, 469, 520, 559, 564, 569, 574, 675, 713, 758, 825,
        886, 907, 920, 941, 954, 975, 988, 1009],
    "choice_options": [420, 506, 510, 736, 740, 744, 781, 785, 789, 815, 1216, 1271, 1379, 1457, 1479],
    "writing_prompt": [794, 1070, 1083, 1111, 1120],
    "detail_prompt": [1074, 1087],
    "instruction": [26, 79, 83, 87, 112, 126, 150, 154, 177, 181, 186, 209, 228, 240, 245, 276,
        294, 298, 303, 323, 328, 351, 355, 359, 363, 367, 372, 383, 387, 391, 409, 429, 524,
        646, 661, 670, 731, 776, 810, 834, 838, 862, 881, 1056, 1091, 1105, 1146, 1157, 1227, 1282],
    "example": [145, 799],
    "declaration": [1024, 1028, 1386],
    "eligibility_context": [399],
    "manual_original": [464, 1035, 1042, 1493],
    "consent_context": [1167, 1177, 1188, 1192, 1202, 1237, 1247, 1257, 1296, 1300, 1304, 1308,
        1312, 1316, 1326, 1340, 1351, 1355, 1365],
    "subject_label": [1422, 1444],
    "conditional_subject": [1466],
    "unresolved_reference": [1435],
    "non_input": [1500],
}


def page_for_record(record):
    # Page 4's footer precedes its prompts in HWP record order.
    if record <= 391:
        return 1
    if record <= 1042:
        return 2
    if record <= 1091:
        return 3
    if record <= 1120:
        return 4
    return 5


def build_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated original")
    texts = inventory["sections"][0]["texts"]
    classified = [(record, kind) for kind, records in CLASSIFICATIONS.items() for record in records]
    ids = [record for record, _ in classified]
    original_ids = [t["record"] for t in texts]
    if len(set(ids)) != len(ids) or len(set(original_ids)) != len(original_ids) or set(ids) != set(original_ids):
        raise ValueError(f"annotation must classify every original text exactly once; missing={sorted(set(original_ids) - set(ids))}; "
                         f"extra={sorted(set(ids) - set(original_ids))}; duplicates={[r for r, n in Counter(ids).items() if n > 1]}")
    classes = dict(classified)
    return {"version": "donghaeng-semantic-reference-v1", "source_sha256": SOURCE_SHA256,
        "annotation_method": "assistant_source_text_and_five_original_rendered_pages",
        "independent_human_review_complete": False, "complete_atomic_gold": False, "whole_form_complete": False,
        "scope": "classification_of_all_preserved_text_records_not_all_atomic_destinations",
        "items": [{"record": t["record"], "page": page_for_record(t["record"]), "kind": classes[t["record"]],
                   "source": t} for t in texts],
        "relations": [
            {"kind": "essay_detail", "prompt": 1070, "details": [1074], "page": 3},
            {"kind": "essay_detail", "prompt": 1083, "details": [1087, 1091], "page": 3},
            {"kind": "shared_essay_rules", "rule": 1056, "prompts": [1070, 1083, 1111, 1120]},
            {"kind": "source_instruction_conflict", "records": [154, 1056],
             "reason": "cover_says_about_three_pages_essay_says_within_three_pages", "resolution": "human_review"},
            {"kind": "conditional_consent_row", "condition": 1466, "headers": [1402, 1407, 1412, 1417],
             "choice": 1479, "automatic_cross_table_person_link": False},
            {"kind": "ambiguous_reference", "reference": 1435, "candidate_choices": [1216, 1271, 1379],
             "automatic_inheritance": False, "resolution": "human_review"},
            {"kind": "manual_separate_dates", "records": [1035, 1493], "automatic_copy": False},
            {"kind": "economic_document_rules", "records": [276, 383],
             "reason": "table_says_select_one_footer_says_at_least_one", "automatic_resolution": False},
            {"kind": "body_recipient_rows", "first": [704, 708, 718, 727], "second": [749, 753, 763, 772],
             "explicit_link_to_consent_condition": False}],
        "category_counts": dict(Counter(classes.values()))}


def audit_semantic_reference(gold, blocks, fields):
    """Measure source preservation and known non-input misuse; never full accuracy."""
    originals = {item["record"]: item for item in gold["items"]}
    original_body = "\n".join(item["source"]["text"] for item in gold["items"])
    offsets, position = {}, 0
    for item in gold["items"]:
        offsets[item["record"]] = position
        position += len(item["source"]["text"]) + 1
    by_record, issues = {}, []
    for block in blocks:
        try:
            record = int(block["location"].rsplit("record:", 1)[1])
        except (KeyError, ValueError, IndexError):
            issues.append({"reason": "source_record_address_invalid"})
            continue
        by_record.setdefault(record, []).append(block)
    for record, item in originals.items():
        found = by_record.get(record, [])
        if (len(found) != 1 or found[0].get("text") != item["source"]["text"] or
            found[0].get("start") != offsets[record] or found[0].get("end") != offsets[record] + len(item["source"]["text"])):
            issues.append({"reason": "source_text_missing_duplicate_or_changed", "record": record})
    for record in by_record.keys() - originals.keys():
        issues.append({"reason": "source_record_not_in_original", "record": record})
    forbidden = {"example", "instruction", "detail_prompt", "consent_context", "declaration", "title",
                 "non_input", "unresolved_reference", "subject_label", "conditional_subject", "eligibility_context"}
    # The service represents original date templates as text fields; the gold's
    # semantic category remains manual_original, irrespective of that enum.
    manual_types = {464: "text", 1035: "text", 1042: "signature", 1493: "text"}
    for field in fields:
        proof = field.get("source") or {}
        try:
            record = int(proof["location"].rsplit("record:", 1)[1])
        except (KeyError, ValueError, IndexError):
            issues.append({"reason": "field_source_address_invalid", "field_id": field.get("id")})
            continue
        item = originals.get(record)
        start, end = proof.get("text_start"), proof.get("text_end")
        evidence = proof.get("evidence", field.get("evidence"))
        valid_span = (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(original_body) and
                      record in offsets and offsets[record] <= start < offsets[record] + len(item["source"]["text"]) and
                      original_body[start:end] == evidence)
        if item is None or proof.get("sha256") != gold["source_sha256"] or not valid_span:
            issues.append({"reason": "field_source_not_original", "record": record, "field_id": field.get("id")})
        elif item["kind"] in forbidden:
            issues.append({"reason": "non_input_record_used_as_field", "record": record, "field_id": field.get("id")})
        if record in manual_types and (field.get("field_type") != manual_types[record] or
                                      (field.get("manual_spec") or {}).get("mode") != "original_only"):
            issues.append({"reason": "manual_original_policy_changed", "record": record, "field_id": field.get("id")})
    return {"scope": gold["scope"], "original_text_records": len(originals), "predicted_fields": len(fields),
            "issues": issues, "issue_count": len(issues), "complete_atomic_gold": False,
            "whole_form_complete": False, "independent_human_review_complete": False}
