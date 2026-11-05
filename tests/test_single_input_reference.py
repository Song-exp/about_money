import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import llm
from app.models.scholarship import PostingStatus
from app.services.form_basic_inputs import audit_basic_inputs
from app.services.posting_extract import extract_structured, EXTRACTOR_VERSION
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from reports.atomic_input_reference import record_of
from reports.single_input_reference import build_single_gold, audit_single_inputs, reconcile_scopes
from reports.measure_single_inputs import original_bytes, fresh_basic_fields, measure_collection
from tests.test_submission_consent_reference import api_session, finish_collection, exported_texts, at
from tests.test_writing_workflow import action
from app.core.config import settings

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def fixture():
    gold = json.loads((REPORTS / "scholarship-improvement-20261010-single-inputs-verified/gold.json").read_text(encoding="utf-8"))
    fields = json.loads((REPORTS / "scholarship-improvement-20261010-grid-audit-verified/r1-prediction.json").read_text(encoding="utf-8"))["application_fields"]
    return gold, fields


def failures(gold, fields):
    return {c["id"]: c["issues"] for c in audit_single_inputs(gold, fields)["checks"] if c["issues"]}


def test_original_contract_preserves_unproven_address_without_mutation(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    gold, fields = fixture()
    before = deepcopy((gold, fields))
    assert failures(gold, fields) == {"single.492": ["source_cell_geometry_requires_review"]}
    result = measure_collection(gold, fields)
    assert result["groups_collected"] == 12 and result["values_collected"] == 13
    assert result["cover_applicant_guardian_name_isolation"] and result["care_example_not_used_as_answer"]
    assert before == (gold, fields)


def test_source_scope_catalog_rebuilt_without_predictions_and_not_full_accuracy():
    inventory = json.loads((REPORTS / "scholarship-improvement-20261010-source-records-verified/source-record-gold.json").read_text(encoding="utf-8"))
    gold, fields = fixture()
    assert build_single_gold(inventory) == gold
    scope = reconcile_scopes(gold, fields)
    assert scope["catalog_entries"] == 60 and scope["covered_group_contracts"] == 52
    assert {e["label_source"]["record"] for e in scope["pending_group_contracts"]} == {232, 317, 637, 806, 1070, 1083, 1111, 1120}
    assert not scope["missing_entries"] and not scope["unexpected_entries"] and not scope["duplicate_entries"]
    assert not gold["complete_atomic_gold"] and not scope["whole_form_complete"]
    # Debt is listed in consent text, but is not a standalone question on this form.
    assert all("부채" not in e["label_source"]["text"] for e in gold["scope_catalog"])


@pytest.mark.parametrize("problem", ["missing", "duplicate", "wrong_label", "other_document", "invented_required", "invented_role",
    "missing_slot", "extra_slot", "swapped_grade_semester", "grade_source", "wrong_unit", "numeric_age_inferred",
    "cell_record", "cell_hash", "cell_table", "cell_row", "cell_span", "cell_control", "cell_text", "slot_destination",
    "visible_destination", "guardian_subject", "guardian_name", "guardian_as_applicant", "guardian_extra_slot",
    "missing_rule", "hidden_example", "copied_example_destination", "manual_bypass", "invented_condition"])
def test_single_input_corruption_is_not_hidden_by_expected_address_backlog(problem):
    gold, fields = fixture()
    name, grade, guardian, care = (at(fields, r) for r in (455, 58, 601, 794))
    if problem == "missing":
        fields.remove(name)
    elif problem == "duplicate":
        fields.append(deepcopy(name))
    elif problem == "wrong_label":
        name["label"] = "다른 항목"
    elif problem == "other_document":
        name["source"]["sha256"] = "wrong"
    elif problem == "invented_required":
        name["required"] = True
    elif problem == "invented_role":
        name["role"] = "applicant"
    elif problem == "missing_slot":
        grade["input_spec"]["slots"].pop()
    elif problem == "extra_slot":
        grade["input_spec"]["slots"].append(deepcopy(grade["input_spec"]["slots"][0]))
    elif problem == "swapped_grade_semester":
        grade["input_spec"]["slots"].reverse()
    elif problem == "grade_source":
        grade["input_spec"]["slots"][0]["source"]["location"] = "Section0/record:520"
    elif problem == "wrong_unit":
        name["input_spec"]["slots"][0]["unit"] = "만원"
    elif problem == "numeric_age_inferred":
        at(fields, 619)["field_type"] = "number"
    elif problem.startswith("cell_"):
        key, value = {"cell_record": ("record", 604), "cell_hash": ("payload_sha256", "wrong"),
            "cell_table": ("table", "wrong"), "cell_row": ("row", 7), "cell_span": ("column_span", 1),
            "cell_control": ("has_control", True), "cell_text": ("text_locations", ["Section0/record:799"])}[problem]
        name["input_cell_source"][key] = value
    elif problem == "slot_destination":
        name["input_spec"]["slots"][0]["destination"] = guardian["layout"]["cell_location"]
    elif problem == "visible_destination":
        name["layout"]["cell_location"] = guardian["layout"]["cell_location"]
    elif problem == "guardian_subject":
        guardian["subject"]["source"]["location"] = "Section0/record:695"
    elif problem == "guardian_name":
        guardian["subject"]["label"] = "신청자"
    elif problem == "guardian_as_applicant":
        guardian["role"] = "applicant"
    elif problem == "guardian_extra_slot":
        guardian["input_spec"] = deepcopy(name["input_spec"])
    elif problem == "missing_rule":
        care["instruction_sources"].pop()
    elif problem == "hidden_example":
        care["instructions"] = "설명 생략"
    elif problem == "copied_example_destination":
        care["input_spec"]["slots"][0]["destination"] = "Section0/record:794"
    elif problem == "manual_bypass":
        name["manual_spec"] = {"mode": "original_only"}
    else:
        guardian["applicability"] = {"mode": "user_confirmation"}
    assert set(failures(gold, fields)) - {"single.492"}


@pytest.mark.parametrize("problem", ["missing", "unexpected", "duplicate", "changed_person"])
def test_scope_catalog_detects_inventory_changes(problem):
    gold, fields = fixture()
    if problem == "missing":
        fields.remove(at(fields, 637))
    elif problem == "unexpected":
        at(fields, 637)["source"]["location"] = "Section0/record:685"
    elif problem == "duplicate":
        fields.append(deepcopy(at(fields, 455)))
    else:
        at(fields, 601)["subject"]["source"]["location"] = "Section0/record:1422"
    scope = reconcile_scopes(gold, fields)
    assert scope["missing_entries"] or scope["unexpected_entries"] or scope["duplicate_entries"]


def test_fresh_native_parser_keeps_address_fallback_unapproved():
    blocks, fields = fresh_basic_fields(original_bytes())
    address = at(fields, 492)
    assert len(fields) == 8  # Preserve reviewable source fields, including the address.
    assert address["input_cell_source"]["record"] == 495
    assert address["input_cell_source"]["geometry_proven"] is False
    assert all(f["input_cell_source"]["geometry_proven"] for f in fields if f is not address)
    audit = audit_basic_inputs(blocks, fields, {"sha256": address["source"]["sha256"]})
    assert audit["unresolved"] == 1
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="source", eligibility={
        "application_fields": fields, "form_coverage": [dict(native_basic_input_audit=audit)],
        "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]
    address.pop("constraint_issues")
    audit["unresolved"] = 0
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]  # Individual source audit still blocks.


def test_extractor_pipeline_preserves_uncertain_address_gate(monkeypatch):
    blocks, _ = fresh_basic_fields(original_bytes())
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "pipeline-source"}, blocks)
    address = at(result["application_fields"], 492)
    assert result["extractor_version"] == EXTRACTOR_VERSION
    assert "basic_input_geometry_requires_original_review" in address["constraint_issues"]
    assert result["coverage"]["native_basic_input_audit"]["unresolved"] == 1


def test_real_names_grade_and_care_api_revision_preserves_other_people(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    monkeypatch.setattr(settings, "chat_rate_limit_per_min", 1000)
    _, fields = fixture()
    selected = [at(fields, r) for r in (68, 455, 601, 58, 794)]
    session = api_session(auth_client, db, selected)
    cover, applicant, guardian, grade, care = selected
    values = {(cover["id"], "name"): "표지확인이름", (applicant["id"], "name"): "본문확인이름",
        (guardian["id"], "value"): "주보호자확인이름", (grade["id"], "grade"): "3학년",
        (grade["id"], "semester"): "2학기", (care["id"], "current_care"): "사용자돌봄고유응답"}
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    for text in exported_texts(auth_client, session):
        assert all(value in text for value in values.values()) and "병원 동행" not in text
    retained = deepcopy(session["answers"])
    session = action(auth_client, session, f"fields/{applicant['id']}/name", {"message": "정정본문고유이름"}, method="put")
    assert not session["drafts"] and not session["answers"][applicant["id"]]["facts"]["name"]["confirmed"]
    assert all(answer == session["answers"][fid] for fid, answer in retained.items() if fid != applicant["id"])
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded["answers"] == session["answers"]
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    for text in exported_texts(auth_client, session):
        assert "정정본문고유이름" in text and "본문확인이름" not in text
        assert "표지확인이름" in text and "주보호자확인이름" in text
