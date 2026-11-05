import io
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import HTTPException

from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services import llm
from app.services.form_checklist_values import native_checklist_values, audit_checklist_values
from app.services.form_semantics import active_fields
from app.services.posting_documents import merge_posting_documents
from app.services.posting_extract import extract_structured
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.services.writing_chat import start_session
from app.services.writing_drafts import compose
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact
from tests.test_form_submission_checklist import fixture
from tests.test_writing_workflow import setup_writing, action


def fields(blocks=None):
    original, source = fixture()
    return [f for g in native_checklist_values(blocks if blocks is not None else original, source) for f in g["fields"]]


def test_paragraph_destinations_are_not_blank_cells_or_rendered_controls():
    grade, income = fields()
    assert grade["source"]["location"].endswith("record:232")
    assert income["source"]["location"].endswith("record:317")
    assert all(not f.get("input_cell_source") and not f["original_destination"]["cell_geometry_verified"] for f in (grade, income))
    assert all(f["original_destination"]["contains_controls"] and not f["original_destination"]["visual_placement_verified"] for f in (grade, income))
    assert grade["required"] is income["required"] is None and grade["role"] == income["role"] == "unknown"
    assert len(requirements(grade)) == 2 and len(requirements(income)) == 1
    assert "26년 2학기" in income["label"] and "2분위" in requirements(income)[0]["question"]
    assert "직전학기 성적" in grade["instructions"] and "택1" in income["instructions"]


@pytest.mark.parametrize("problem", ["ocr", "pointer", "extra_paragraph", "gpa_scale", "order", "offset", "duplicate", "cell_alias", "blank", "income_period", "missing_choice", "missing_footer"])
def test_unproven_paragraph_stays_in_individual_audit(problem):
    blocks, source = fixture()
    block = lambda r: next(b for b in blocks if b["location"].endswith(f"record:{r}"))
    shape = next(b["table_structure"] for b in blocks if b.get("table_structure", {}).get("table") == "BodyText/Section0/table:94")
    cell = next(c for c in shape["cells"] if c["record"] == 226)
    if problem == "ocr":
        block(232)["method"] = "ocr"
    elif problem == "pointer":
        cell["text_locations"] = [block(228)["location"], "wrong", block(240)["location"]]
    elif problem == "extra_paragraph":
        blocks.insert(blocks.index(block(232)) + 1, {**block(232), "location": "unexpected", "text": "추가 작성 조건"})
    elif problem == "gpa_scale":
        block(232)["text"] = ": (작성하기) / 5.0"
    elif problem == "order":
        block(232)["start"] = block(228)["start"]
    elif problem == "offset":
        block(232)["end"] += 1
    elif problem == "duplicate":
        blocks.insert(blocks.index(block(232)) + 1, deepcopy(block(232)))
    elif problem == "cell_alias":
        block(232)["cell_location"] = "wrong"
    elif problem == "blank":
        cell["blank"] = True
    elif problem == "income_period":
        block(317)["text"] = "* 소득구간 (작성하기) 분위"
    elif problem == "missing_choice":
        blocks.remove(block(276))
    else:
        blocks.remove(block(383))
    actual = fields(blocks)
    assert len(actual) == 1
    assert audit_checklist_values(blocks, actual, source)["unresolved"] == 1


@pytest.mark.parametrize("key,text", [("gpa", "-1"), ("gpa", "NaN"), ("gpa", "1e1"), ("gpa", "네"), ("scale", "5.0"), ("scale", "4.50")])
def test_gpa_requires_actual_number_and_original_scale(key, text):
    with pytest.raises(HTTPException) as exc:
        _set_fact({}, fields()[0], key, text, {})
    assert exc.value.status_code == 422


def test_gpa_is_independent_and_over_scale_does_not_convert():
    grade = fields()[0]
    answers = {}
    _set_fact(answers, grade, "gpa", "4.4", {})
    with pytest.raises(HTTPException):
        _set_fact(answers, grade, "scale", "4.3", {})
    _set_fact(answers, grade, "scale", "4.5", {})
    rendered = compose(grade, answers[grade["id"]], SimpleNamespace(title="장학금"))
    assert rendered["text"] == "4.4 / 4.5"
    assert rendered["validation"]["warnings"] == ["original_paragraph_layout_check_required"]
    for fact in answers[grade["id"]]["facts"].values():
        fact["confirmed"] = True
    _set_fact(answers, grade, "gpa", "3.8", {})
    assert all(not f["confirmed"] for f in answers[grade["id"]]["facts"].values())


@pytest.mark.parametrize("text", ["3", "-1", "2.0", "2분위", "모름"])
def test_income_bracket_is_source_bounded_integer(text):
    with pytest.raises(HTTPException) as exc:
        _set_fact({}, fields()[1], "income_bracket", text, {})
    assert exc.value.status_code == 422


def test_income_scope_is_user_selected_and_not_an_unconditional_requirement():
    grade, income = fields()
    assert active_fields([grade, income], {}) == [grade]
    condition = income["applicability"]["id"]
    assert active_fields([grade, income], {"applicability_answers": {condition: {"confirmed": True, "applies": False}}}) == [grade]
    assert active_fields([grade, income], {"applicability_answers": {condition: {"confirmed": True, "applies": True}}}) == [grade, income]


def test_omitted_model_prompts_are_restored_without_merging_body_answers(monkeypatch):
    blocks, source = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), source, blocks)
    actual = [f for f in result["application_fields"] if f.get("input_spec", {}).get("version") == "native-checklist-values-v1"]
    assert len(actual) == 2 and result["coverage"]["native_checklist_value_audit"]["unresolved"] == 0
    body_grade = next(f for f in result["application_fields"] if f["source"]["location"].endswith("record:529"))
    answers = {}
    _set_fact(answers, body_grade, "gpa", "3.9", {})
    assert actual[0]["id"] not in answers


def test_individual_prompt_failure_cannot_be_approved_by_cleared_summary():
    blocks, source = fixture()
    actual = fields()
    actual[1]["input_spec"]["slots"][0]["maximum"] = 8
    audit = audit_checklist_values(blocks, actual, source)
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": actual, "form_coverage": [{"native_checklist_value_audit": audit}],
        "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("key,value", [("field_type", "attachment"), ("manual_spec", {"mode": "original_only"}),
                                      ("semantic_kind", "submission_checklist"), ("required", False)])
def test_value_collection_cannot_be_hidden_by_altering_field_contract(key, value):
    blocks, source = fixture()
    actual = fields()
    actual[0][key] = value
    assert audit_checklist_values(blocks, actual, source)["unresolved"] == 1


@pytest.mark.parametrize("key", ["maximum", "original_layout_review_required", "cell_geometry_verified"])
def test_merge_preserves_original_bracket_and_layout_review_conflicts(key):
    first = fields()[1]
    second = deepcopy(first)
    if key == "maximum":
        second["input_spec"]["slots"][0][key] = 8
    elif key == "original_layout_review_required":
        second["input_spec"][key] = False
    else:
        second["original_destination"][key] = True
    result = merge_posting_documents(SimpleNamespace(source_documents=[{"structured": {"application_fields": [f]}} for f in (first, second)]))
    assert "conflicting_duplicate_form_field" in result["issues"]


def _session(auth_client, db):
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    row.eligibility = {**row.eligibility, "application_fields": fields(), "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile(gpa=4.1))
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    assert not session["answers"]
    return session


def test_api_collection_manual_layout_review_exports_and_revision(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = _session(auth_client, db)
    grade, income = fields()
    session = action(auth_client, session, "messages", {"message": "3.8"})
    action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "4.5"})
    session = action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True})
    assert session["next_action"]["kind"] == "applicability" and "제출 대상으로 선택" in session["next_action"]["question"]
    session = action(auth_client, session, "messages", {"message": "예"})
    assert income["id"] not in session["answers"]
    session = action(auth_client, session, "messages", {"message": "2"})
    session = action(auth_client, session, f"fields/{income['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert [d["text"] for d in session["drafts"]] == ["3.8 / 4.5", "2 분위"]
    action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": False}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    url = f"/writing-sessions/{session['id']}/export"
    txt, docx = auth_client.get(url + "?format=txt"), auth_client.get(url + "?format=docx")
    assert txt.status_code == docx.status_code == 200
    for text in (txt.content.decode("utf-8-sig"), "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)):
        assert "3.8 / 4.5" in text and "26년 2학기" in text and "2 분위" in text
    session = action(auth_client, session, f"fields/{grade['id']}/gpa", {"message": "3.7"}, method="put")
    assert not session["drafts"] and not session["draft_reviewed"]
    assert all(not f["confirmed"] for f in session["answers"][grade["id"]]["facts"].values())
    assert auth_client.get(url + "?format=txt").status_code == 409


def test_api_unselected_income_is_excluded_and_reselection_requires_confirmation(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = _session(auth_client, db)
    grade, income = fields()
    session = action(auth_client, session, "messages", {"message": "3.8"})
    session = action(auth_client, session, "messages", {"message": "4.3"})
    session = action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "messages", {"message": "아니요"})
    action(auth_client, session, f"fields/{income['id']}/income_bracket", {"message": "1"}, method="put", expected=409)
    session = action(auth_client, session, "draft")
    assert len(session["drafts"]) == 1 and session["drafts"][0]["field_id"] == grade["id"]
    session = action(auth_client, session, f"conditions/{income['applicability']['id']}", {"applies": True})
    assert not session["drafts"] and session["next_action"]["field_id"] == income["id"]
