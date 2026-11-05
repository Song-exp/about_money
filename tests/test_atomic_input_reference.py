import json
from copy import deepcopy
from pathlib import Path

import pytest

from reports.atomic_input_reference import audit_atomic_inputs, build_atomic_gold, record_of

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fixture():
    gold = read(REPORTS / "scholarship-improvement-20261010-atomic-inputs-verified/gold.json")
    fields = read(REPORTS / "scholarship-improvement-20261010-grid-audit-verified/r1-prediction.json")["application_fields"]
    return gold, fields


def field_at(fields, record):
    return next(f for f in fields if record_of(f.get("source")) == record)


def test_source_atoms_bind_without_promoting_full_accuracy_and_do_not_mutate():
    gold, fields = fixture()
    before = deepcopy((gold, fields))
    audit = audit_atomic_inputs(gold, fields)
    assert audit["checked_atoms"] == 25 and audit["unresolved_atoms"] == 0
    assert not audit["complete_atomic_gold"] and not audit["independent_human_review_complete"]
    assert not audit["whole_form_complete"] and (gold, fields) == before


@pytest.mark.parametrize("problem", ["missing_group", "duplicate_group", "missing_component", "duplicate_component", "extra_component",
    "foreign_source", "wrong_quote", "wrong_source_record", "wrong_unit", "wrong_numeric_kind", "gpa_scale_options",
    "gpa_scale_link", "wrong_cell", "wrong_cell_record", "wrong_cell_hash", "wrong_cell_geometry", "wrong_cell_table",
    "wrong_paragraph", "missing_rule", "changed_rule", "other_person_row", "other_person_subject", "person_scope_disabled",
    "invented_person_condition", "missing_condition", "automatic_condition", "keep_previous_person", "manual_collection_bypass"])
def test_atomic_value_condition_person_and_destination_changes_are_detected(problem):
    gold, fields = fixture()
    grade = field_at(fields, 529)
    person = field_at(fields, 704)
    condition = field_at(fields, 656)
    if problem == "missing_group":
        fields.remove(grade)
    elif problem == "duplicate_group":
        fields.append(deepcopy(grade))
    elif problem == "missing_component":
        grade["input_spec"]["slots"].pop()
    elif problem == "duplicate_component":
        grade["input_spec"]["slots"].append(deepcopy(grade["input_spec"]["slots"][0]))
    elif problem == "extra_component":
        grade["input_spec"]["slots"].append({"key": "invented"})
    elif problem == "foreign_source":
        grade["input_spec"]["slots"][0]["source"]["sha256"] = "other-document"
    elif problem == "wrong_quote":
        grade["input_spec"]["slots"][0]["source"]["evidence"] = "다른 내용"
    elif problem == "wrong_source_record":
        grade["input_spec"]["slots"][0]["source"]["location"] = "Section0/record:549"
    elif problem == "wrong_unit":
        field_at(fields, 666)["input_spec"]["slots"][0]["unit"] = "원"
    elif problem == "wrong_numeric_kind":
        grade["input_spec"]["slots"][0]["kind"] = "text"
    elif problem == "gpa_scale_options":
        grade["input_spec"]["slots"][2]["options"] = ["4.0"]
    elif problem == "gpa_scale_link":
        grade["input_spec"]["slots"][1]["scale_key"] = "other_scale"
    elif problem == "wrong_cell":
        person["input_spec"]["slots"][0]["destination"] = "BodyText/Section0/table:451/row:13/cell:7"
    elif problem in ("wrong_cell_record", "wrong_cell_hash", "wrong_cell_geometry", "wrong_cell_table"):
        cell = person["input_cell_sources"][0]
        key, value = {"wrong_cell_record": ("record", 756), "wrong_cell_hash": ("payload_sha256", "wrong"),
                      "wrong_cell_geometry": ("row", 12), "wrong_cell_table": ("table", "wrong")}[problem]
        cell[key] = value
    elif problem == "wrong_paragraph":
        field_at(fields, 515)["input_spec"]["slots"][0]["destination"] = "Section0/record:675"
    elif problem == "missing_rule":
        field_at(fields, 515)["instruction_sources"].pop()
    elif problem == "changed_rule":
        field_at(fields, 515)["instruction_sources"][-1]["evidence"] = "2025-1학기 기준"
    elif problem == "other_person_row":
        person["layout"]["row"] = 12
    elif problem == "other_person_subject":
        person["subject"]["source"]["location"] = "Section0/record:1444"
    elif problem == "person_scope_disabled":
        person["input_spec"]["person_row"] = False
    elif problem == "invented_person_condition":
        person["applicability"] = deepcopy(condition["applicability"])
    elif problem == "missing_condition":
        condition.pop("applicability")
    elif problem == "automatic_condition":
        condition["applicability"]["mode"] = "automatic"
    elif problem == "keep_previous_person":
        condition["applicability"]["discard_facts_on_change"] = False
    else:
        grade["manual_spec"] = {"mode": "original_only"}
    audit = audit_atomic_inputs(gold, fields)
    assert audit["unresolved_atoms"] > 0


@pytest.mark.parametrize("problem", ["foreign_document", "uncertain_geometry"])
def test_gold_cannot_be_built_from_different_source_or_uncertain_destination(problem):
    inventory = read(REPORTS / "scholarship-improvement-20261010-source-records-verified/source-record-gold.json")
    if problem == "foreign_document":
        inventory["source_sha256"] = "wrong"
    else:
        cell = next(c for c in inventory["sections"][0]["list_records"] if c["record"] == 557)
        cell["geometry_candidates"] = []
    with pytest.raises(ValueError):
        build_atomic_gold(inventory)
