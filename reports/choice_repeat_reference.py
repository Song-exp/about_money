"""Bounded source-authored references for conditional values and repeat rows."""
import re

from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256

CHOICES = [
    {"id": "housing", "label": 501, "mode": "marks", "rules": [], "row": 2, "options": [
        ("자가(만원)", 506, "amount", "만원"), ("전세(만원)", 506, "amount", "만원"),
        ("월세(만원/월)", 510, "amount", "만원/월"), ("기타()", 510, "text", "")]},
    {"id": "first_care_choice", "label": 727, "mode": "multiple", "rules": [731], "row": 11, "options": [
        ("장기요양인정자(등급:)", 736, "grade", ""), ("중증장애인", 740, None, ""),
        ("정신건강관련질환으로치료중", 744, None, "")]},
    {"id": "second_care_choice", "label": 772, "mode": "multiple", "rules": [776], "row": 12, "options": [
        ("장기요양인정자(등급:)", 781, "grade", ""), ("중증장애인", 785, None, ""),
        ("정신건강관련질환으로치료중", 789, None, "")]},
    {"id": "uyang_history", "label": 415, "mode": "single", "rules": [429], "row": 0, "options": [
        ("있음", 420, None, ""), ("없음", 420, None, "")]},
]
REPEAT_COLUMNS = [("year", 843, "year"), ("provider", 848, "text"), ("name", 853, "text"),
    ("semester_amount", 858, "semester_amount"), ("total_amount", 867, "reported_amount"),
    ("start_date", 872, "date"), ("end_date", 872, "date"), ("nature", 877, "text")]
REPEAT_ROWS = [(886, 907, [884, 889, 893, 897, 901, 905, 914]),
    (920, 941, [918, 923, 927, 931, 935, 939, 948]),
    (954, 975, [952, 957, 961, 965, 969, 973, 982]),
    (988, 1009, [986, 991, 995, 999, 1003, 1007, 1016])]


def compact(text):
    return re.sub(r"\s+", "", text)


def build_choice_repeat_gold(inventory):
    if inventory["source_sha256"] != SOURCE_SHA256 or len(inventory["sections"]) != 1:
        raise ValueError("not the annotated source")
    section = inventory["sections"][0]
    texts = {t["record"]: t for t in section["texts"]}
    lists = {r["record"]: r for r in section["list_records"]}
    groups = []
    for definition in CHOICES:
        group = {"id": definition["id"], "label_source": texts[definition["label"]], "mode": definition["mode"],
                 "row": definition["row"], "rule_sources": [texts[r] for r in definition["rules"]], "choices": []}
        for index, (normalized, record, kind, unit) in enumerate(definition["options"]):
            source = texts[record]
            candidates = [p.strip() for p in re.split(r"[□☐]", source["text"])[1:]]
            captions = [caption for caption in candidates if compact(caption) == normalized]
            if len(captions) != 1:
                raise ValueError("source choice differs from authored grammar")
            values = [] if kind is None else [{"key": f"choice_{index}_value", "source": source, "kind": kind, "unit": unit}]
            if definition["id"] == "uyang_history" and normalized == "있음":
                values = [{"key": key, "source": texts[r], "kind": value_kind, "unit": "", "destination_record": r,
                           "placement": "separate_cell"} for key, r, value_kind in (
                    ("history_count", 434, "positive_integer"), ("history_year", 439, "text"), ("history_name", 444, "text"))]
            group["choices"].append({"caption": captions[0], "source": source, "values": values})
        groups.append(group)
    repeat = {"label_source": texts[830], "rule_sources": [texts[r] for r in (834, 838, 862, 881)],
        "columns": [{"key": key, "source": texts[r], "kind": kind, "unit": ""} for key, r, kind in REPEAT_COLUMNS],
        "templates": [{"year_source": texts[year], "date_source": texts[date], "cells": [lists[r] for r in cells]}
                      for year, date, cells in REPEAT_ROWS], "source_rows_are_not_reported_answers": True,
        "additional_rows_require_original_layout_review": True, "currency_unit_not_fixed_by_source": True}
    for template in repeat["templates"]:
        if any(c["kind"] != "cell_geometry_candidate" or len(c["geometry_candidates"]) != 1 for c in template["cells"]):
            raise ValueError("repeat row geometry not independently unambiguous")
    return {"version": "donghaeng-choice-repeat-reference-v1", "source_sha256": SOURCE_SHA256,
        "choice_groups": groups, "repeat_group": repeat, "scope": "four_choice_groups_one_repeat_group_not_whole_form",
        "complete_atomic_gold": False, "whole_form_complete": False, "independent_human_review_complete": False}


def proof_matches(actual, original, sha):
    return (record_of(actual) == original["record"] and (actual or {}).get("sha256") == sha and
            (actual or {}).get("evidence") == original["text"])


def audit_choice_repeat(gold, fields):
    sha, checks = gold["source_sha256"], []
    for group in gold["choice_groups"]:
        actual = [f for f in fields if record_of(f.get("source")) == group["label_source"]["record"]]
        issues = []
        if len(actual) != 1:
            issues.append("choice_group_missing_or_duplicate")
        else:
            field, spec = actual[0], actual[0].get("choice_spec") or {}
            if (not proof_matches(field.get("source"), group["label_source"], sha) or field.get("field_type") != "checkbox" or
                field.get("selection_mode") != group["mode"] or field.get("required") is not None or
                field.get("manual_spec") or field.get("input_spec") or field.get("applicability") or
                field.get("layout", {}).get("row") != group["row"] or
                field.get("layout", {}).get("table") != f"BodyText/Section0/table:{group['label_source']['table_parent']}" or
                field.get("options") != [c["caption"] for c in group["choices"]]):
                issues.append("choice_policy_or_scope_changed")
            if spec.get("multiple_explicit") is not (group["mode"] == "multiple"):
                issues.append("explicit_multiple_rule_changed")
            rules = spec.get("multiple_sources", []) if group["mode"] == "multiple" else spec.get("condition_sources", [])
            if any(not any(proof_matches(p, r, sha) for p in rules) for r in group["rule_sources"]):
                issues.append("choice_rule_missing")
            choices = spec.get("choices", [])
            if len(choices) != len(group["choices"]):
                issues.append("choices_missing_or_extra")
            for expected in group["choices"]:
                selected = [c for c in choices if c.get("caption") == expected["caption"]]
                if len(selected) != 1 or not proof_matches(selected[0].get("source"), expected["source"], sha):
                    issues.append("choice_source_or_caption_changed")
                    continue
                values = selected[0].get("values", [])
                if len(values) != len(expected["values"]):
                    issues.append("conditional_values_missing_or_extra")
                for value in expected["values"]:
                    matches = [v for v in values if v.get("key") == value["key"]]
                    if len(matches) != 1 or not proof_matches(matches[0].get("source"), value["source"], sha) or any(
                        matches[0].get(k, "") != value[k] for k in ("kind", "unit")):
                        issues.append("conditional_value_contract_changed")
                    elif value.get("destination_record") and (record_of({"location": matches[0].get("destination", "")}) != value["destination_record"] or matches[0].get("placement") != value["placement"]):
                        issues.append("conditional_value_destination_changed")
        checks.append({"id": group["id"], "issues": list(dict.fromkeys(issues))})
    reference = gold["repeat_group"]
    actual = [f for f in fields if record_of(f.get("source")) == reference["label_source"]["record"]]
    issues = []
    if len(actual) != 1:
        issues.append("repeat_group_missing_or_duplicate")
    else:
        field = actual[0]
        input_spec = field.get("input_spec") or {}
        repeat = input_spec.get("repeat") or {}
        if not proof_matches(field.get("source"), reference["label_source"], sha) or field.get("field_type") != "text" or field.get("manual_spec") or field.get("applicability"):
            issues.append("repeat_collection_policy_changed")
        if repeat.get("original_layout_review_required") is not True:
            issues.append("additional_row_layout_review_missing")
        slots = input_spec.get("slots", [])
        if repeat.get("count_key") != "entry_count" or len(slots) != 2:
            issues.append("collection_scope_or_count_changed")
        for key, record, kind in (("scope_confirmed", 834, "scope_confirmation"), ("entry_count", 838, "row_count")):
            matches = [s for s in slots if s.get("key") == key]
            rule = next(r for r in reference["rule_sources"] if r["record"] == record)
            if len(matches) != 1 or matches[0].get("kind") != kind or not proof_matches(matches[0].get("source"), rule, sha):
                issues.append("collection_scope_or_count_changed")
        columns = repeat.get("columns", [])
        if len(columns) != 8 or {c.get("key") for c in columns} != {c["key"] for c in reference["columns"]}:
            issues.append("repeat_columns_missing_duplicate_or_extra")
        for expected in reference["columns"]:
            matches = [c for c in columns if c.get("key") == expected["key"]]
            if len(matches) != 1 or not proof_matches(matches[0].get("source"), expected["source"], sha) or any(matches[0].get(k, "") != expected[k] for k in ("kind", "unit")):
                issues.append("repeat_column_contract_changed")
        if any(not any(proof_matches(p, r, sha) for p in field.get("instruction_sources", [])) for r in reference["rule_sources"]):
            issues.append("repeat_source_rules_missing")
        templates = repeat.get("templates", [])
        if len(templates) != 4:
            issues.append("source_template_rows_missing_or_extra")
        for index, expected in enumerate(reference["templates"]):
            if index >= len(templates):
                continue
            template = templates[index]
            if not proof_matches(template.get("year_source"), expected["year_source"], sha) or not proof_matches(template.get("date_source"), expected["date_source"], sha):
                issues.append("source_template_year_or_period_changed")
            cells = template.get("cells", [])
            if len(cells) != 7:
                issues.append("source_template_cells_missing_or_extra")
            for cell in expected["cells"]:
                matches = [c for c in cells if c.get("record") == cell["record"]]
                geom = cell["geometry_candidates"][0]
                table = f"BodyText/Section0/table:{cell['table_parent']}"
                location = f"{table}/row:{geom['row'] + 1}/cell:{geom['column'] + 1}"
                if len(matches) != 1 or matches[0].get("table") != table or matches[0].get("cell_location") != location or matches[0].get("sha256") != sha or matches[0].get("payload_sha256") != cell["payload_sha256"] or any(matches[0].get(k) != geom[k] for k in ("row", "column", "row_span", "column_span")):
                    issues.append("repeat_cell_proof_or_row_changed")
    checks.append({"id": "external_history", "issues": list(dict.fromkeys(issues))})
    return {"scope": gold["scope"], "groups": checks, "unresolved_groups": sum(bool(c["issues"]) for c in checks),
            "whole_form_complete": False, "complete_atomic_gold": False, "independent_human_review_complete": False}
