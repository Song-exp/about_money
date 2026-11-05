import json
from copy import deepcopy
from pathlib import Path

import pytest
from types import SimpleNamespace
from app.core.config import settings
from app.services import llm
from app.services.form_checklist_values import audit_checklist_values
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from reports.atomic_input_reference import record_of
from reports.remaining_branch_reference import build_branch_gold, audit_branches
from reports.measure_remaining_branches import updated_fields, measure_collection
from reports.measure_single_inputs import fresh_basic_fields, original_bytes
from tests.test_submission_consent_reference import api_session, finish_collection, exported_texts, at
from tests.test_writing_workflow import action

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def fixture(corrected=True):
    gold = json.loads((REPORTS / "scholarship-improvement-20261010-remaining-branches-verified/gold.json").read_text(encoding="utf-8"))
    fields = json.loads((REPORTS / "scholarship-improvement-20261010-grid-audit-verified/r1-prediction.json").read_text(encoding="utf-8"))["application_fields"]
    return gold, updated_fields(fields) if corrected else fields


def test_original_reference_rebuild_and_old_policy_gap_is_detected():
    gold, old = fixture(corrected=False)
    inventory = json.loads((REPORTS / "scholarship-improvement-20261010-source-records-verified/source-record-gold.json").read_text(encoding="utf-8"))
    assert build_branch_gold(inventory) == gold
    audit = audit_branches(gold, old)
    assert [(c["id"], c["issues"]) for c in audit["checks"] if c["issues"]] == [
        ("prompt.317", ["deselected_document_retains_old_answer"])]
    assert not gold["complete_atomic_gold"] and not gold["whole_form_complete"]


def test_corrected_contracts_and_exhaustive_mark_subsets_do_not_mutate(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    gold, fields = fixture()
    before = deepcopy((gold, fields))
    assert not audit_branches(gold, fields)["unresolved_groups"]
    result = measure_collection(fields)
    assert len(result["branch_cases"]) == 20 and result["invalid_values_rejected"] == 6
    assert result["body_grade_and_bracket_not_copied"]
    assert (gold, fields) == before


def test_production_audit_blocks_retained_document_answers_even_when_summary_is_cleared():
    _, fields = fixture()
    selected = [at(fields, r) for r in (232,317)]
    income = selected[1]
    income["applicability"]["discard_facts_on_change"] = False
    blocks, _ = fresh_basic_fields(original_bytes())
    audit = audit_checklist_values(blocks, selected, {"sha256": income["source"]["sha256"]})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="source", eligibility={
        "application_fields": selected, "form_coverage": [{"native_checklist_value_audit": audit}],
        "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("problem", ["missing", "duplicate", "wrong_source", "missing_rule", "hidden_rule", "paragraph_as_cell",
    "automatic_geometry", "automatic_layout", "missing_manual_layout", "missing_scale", "changed_scale_options",
    "changed_gpa_link", "changed_bracket_unit", "changed_bracket_maximum", "wrong_paragraph", "missing_document_condition",
    "wrong_condition_source", "retained_deselected_answer", "grade_condition", "invented_family", "missing_family",
    "single_family", "missing_empty_family", "invented_family_value", "wrong_original_mark", "missing_mark_source",
    "missing_support_condition", "missing_empty_bracket", "support_limit", "support_destination", "missing_evidence",
    "evidence_wrong_person", "evidence_automatic_destination", "invented_multiple_rule"])
def test_branch_contract_corruption_is_detected(problem):
    gold, fields = fixture()
    grade, income, family, support = (at(fields, r) for r in (232,317,637,806))
    if problem == "missing":
        fields.remove(grade)
    elif problem == "duplicate":
        fields.append(deepcopy(grade))
    elif problem == "wrong_source":
        grade["source"]["sha256"] = "other-document"
    elif problem == "missing_rule":
        income["instruction_sources"].pop()
    elif problem == "hidden_rule":
        income["instructions"] = "지시 생략"
    elif problem == "paragraph_as_cell":
        grade["input_cell_source"] = {"record": 226}
    elif problem == "automatic_geometry":
        grade["original_destination"]["cell_geometry_verified"] = True
    elif problem == "automatic_layout":
        grade["original_destination"]["visual_placement_verified"] = True
    elif problem == "missing_manual_layout":
        grade["input_spec"]["original_layout_review_required"] = False
    elif problem == "missing_scale":
        grade["input_spec"]["slots"].pop()
    elif problem == "changed_scale_options":
        grade["input_spec"]["slots"][1]["options"] = ["4.0"]
    elif problem == "changed_gpa_link":
        grade["input_spec"]["slots"][0]["scale_key"] = "other"
    elif problem == "changed_bracket_unit":
        income["input_spec"]["slots"][0]["unit"] = "구간"
    elif problem == "changed_bracket_maximum":
        income["input_spec"]["slots"][0]["maximum"] = 3
    elif problem == "wrong_paragraph":
        income["input_spec"]["slots"][0]["destination"] = "Section0/record:825"
    elif problem == "missing_document_condition":
        income.pop("applicability")
    elif problem == "wrong_condition_source":
        income["applicability"]["sources"][0]["location"] = "Section0/record:806"
    elif problem == "retained_deselected_answer":
        income["applicability"]["discard_facts_on_change"] = False
    elif problem == "grade_condition":
        grade["applicability"] = deepcopy(income["applicability"])
    elif problem == "invented_family":
        family["options"].append("형제")
    elif problem == "missing_family":
        family["choice_spec"]["choices"].pop()
    elif problem == "single_family":
        family["selection_mode"] = "single"
    elif problem == "missing_empty_family":
        family["choice_spec"]["allow_empty"] = False
    elif problem == "invented_family_value":
        family["choice_spec"]["choices"][0]["values"] = [{"key": "name"}]
    elif problem == "wrong_original_mark":
        family["choice_spec"]["mark"] = "√"
    elif problem == "missing_mark_source":
        family["choice_spec"].pop("mark_source")
    elif problem == "missing_support_condition":
        support["choice_spec"]["condition_sources"] = []
    elif problem == "missing_empty_bracket":
        support["choice_spec"]["empty_values"] = []
    elif problem == "support_limit":
        support["choice_spec"]["empty_values"][0]["maximum"] = 3
    elif problem == "support_destination":
        support["choice_spec"]["empty_values"][0]["destination"] = "Section0/record:317"
    elif problem == "missing_evidence":
        support["choice_spec"]["choices"][0]["values"] = []
    elif problem == "evidence_wrong_person":
        support["choice_spec"]["choices"][0]["values"][0]["key"] = "near_poverty_evidence"
    elif problem == "evidence_automatic_destination":
        support["choice_spec"]["choices"][0]["values"][0]["destination"] = "Section0/record:815"
    else:
        support["choice_spec"]["multiple_explicit"] = True
    assert audit_branches(gold, fields)["unresolved_groups"] > 0


@pytest.fixture(autouse=True)
def accelerated_conversation_without_model(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    monkeypatch.setattr(settings, "chat_rate_limit_per_min", 1000)


def reviewed(client, session):
    session = action(client, session, "draft")
    return action(client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})


def test_real_checklist_deselection_deletes_only_selected_document_answer(auth_client, db):
    _, fields = fixture()
    selected = [at(fields, r) for r in (529,232,317,806)]
    body, grade, income, support = selected
    session = api_session(auth_client, db, selected)
    values = {(body["id"], "credits"): "18", (body["id"], "gpa"): "4.1", (body["id"], "scale"): "4.3",
        (grade["id"], "gpa"): "3.8", (grade["id"], "scale"): "4.5", (income["id"], "income_bracket"): "1",
        (support["id"], "value"): "표시하지 않음", (support["id"], "support_bracket"): "2"}
    session = finish_collection(auth_client, session, values)
    session = reviewed(auth_client, session)
    for text in exported_texts(auth_client, session):
        assert "3.8 / 4.5" in text and "1 분위" in text and "학자금 지원 구간: 2 구간" in text
    before = session
    retained = {k: deepcopy(v) for k, v in session["answers"].items() if k != income["id"]}
    cid = income["applicability"]["id"]
    session = action(auth_client, session, f"conditions/{cid}", {"applies": False})
    assert session["answers"] == retained and not session["drafts"]
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    action(auth_client, before, f"conditions/{cid}", {"applies": True}, expected=409)
    action(auth_client, session, f"fields/{income['id']}/income_bracket", {"message": "1"}, method="put", expected=409)
    session = reviewed(auth_client, session)
    for text in exported_texts(auth_client, session):
        assert "1 분위" not in text and "3.8 / 4.5" in text
    session = action(auth_client, session, f"conditions/{cid}", {"applies": True})
    assert session["answers"] == retained and session["next_action"]["kind"] == "field"
    assert session["next_action"]["slot_key"] == "income_bracket"
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded["answers"] == retained and loaded["next_action"] == session["next_action"]
    values[(income["id"], "income_bracket")] = "2"
    session = finish_collection(auth_client, session, values)
    session = reviewed(auth_client, session)
    repeated = action(auth_client, session, f"conditions/{cid}", {"applies": True})
    assert repeated["answers"] == session["answers"] and repeated["drafts"] == session["drafts"] and repeated["draft_reviewed"]
    for text in exported_texts(auth_client, repeated):
        assert "2 분위" in text and "1 분위" not in text


def test_real_support_branch_recollects_evidence_and_family_choice_is_independent(auth_client, db):
    _, fields = fixture()
    family, support = (at(fields, r) for r in (637,806))
    session = api_session(auth_client, db, [family, support])
    values = {(family["id"], "value"): '["부", "모"]', (support["id"], "value"): "표시하지 않음",
              (support["id"], "support_bracket"): "2"}
    session = finish_collection(auth_client, session, values)
    session = reviewed(auth_client, session)
    family_answer = deepcopy(session["answers"][family["id"]])
    session = action(auth_client, session, f"fields/{support['id']}/value", {"message": "①수급자"}, method="put")
    assert set(session["answers"][support["id"]]["facts"]) == {"value"}
    assert session["answers"][family["id"]] == family_answer and not session["drafts"]
    action(auth_client, session, f"fields/{support['id']}/support_bracket", {"message": "2"}, method="put", expected=422)
    action(auth_client, session, "messages", {"message": "제출 불가"}, expected=422)
    action(auth_client, session, f"fields/{support['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "제출 가능"})
    session = action(auth_client, session, f"fields/{support['id']}/confirm", {"confirmed": True})
    session = reviewed(auth_client, session)
    for text in exported_texts(auth_client, session):
        assert "증빙서류 제출 가능 여부(사용자 응답): 제출 가능" in text and "학자금 지원 구간: 2 구간" not in text
    session = action(auth_client, session, f"fields/{support['id']}/value", {"message": '["①수급자", "②차상위"]'}, method="put")
    assert set(session["answers"][support["id"]]["facts"]) == {"value"}
    session = action(auth_client, session, "messages", {"message": "제출 가능"})
    action(auth_client, session, f"fields/{support['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "제출 가능"})
    session = action(auth_client, session, f"fields/{support['id']}/confirm", {"confirmed": True})
    saved = deepcopy(session["answers"][support["id"]])
    session = action(auth_client, session, f"fields/{family['id']}/value", {"message": "표시하지 않음"}, method="put")
    assert session["answers"][support["id"]] == saved
    session = action(auth_client, session, f"fields/{family['id']}/confirm", {"confirmed": True})
    session = reviewed(auth_client, session)
    family_draft = next(d for d in session["drafts"] if d["field_id"] == family["id"])
    assert family_draft["text"] == "표시하지 않음"
