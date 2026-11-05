import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_care_support import native_care_support, audit_care_support
from app.services.posting_extract import extract_structured
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_requirements import requirements
from app.services.writing_drafts import compose
from app.services.writing_chat import start_session
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.models.writing_session import WritingSession
from app.models.scholarship import PostingStatus
from app.schemas.schemas import UserProfile
from tests.test_writing_workflow import setup_writing, action


def fixture():
    blocks = []
    def add(text, row, col, span, paragraph=0):
        blocks.append({"text": text, "table": "care", "row": row, "column": col, "column_span": span,
            "row_span": 1, "method": "native", "location": f"care/r{row}/c{col}/p{paragraph}",
            "cell_location": f"care/r{row}/c{col}"})
    add("현재 돌봄대상자를 위해 본인이 직접 돌봄하고 있는 내용을 간략히 작성해 주세요.", 0, 1, 7)
    add("예: 병원 동행, 식사·복약 관리, 이동 보조 등", 0, 8, 11)
    add("① 수급자, 차상위에 해당하는 경우 해당사항 표시", 1, 0, 10)
    add("*증빙서류 제출 가능한 경우만 인정", 1, 0, 10, 1)
    add("①수급자          ②차상위", 1, 10, 9)
    add("② 학자금 지원 구간 (①에 해당하지 않을 경우 작성)", 2, 0, 10)
    add("구간 (2구간 이내)", 2, 10, 9)
    return finalize_blocks(blocks)


def fields():
    return [g["fields"][0] for g in native_care_support(fixture(), {"sha256": "hash"})]


def test_model_omission_recovered_and_examples_not_drafted(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert len(result["application_fields"]) == 2
    assert result["coverage"]["native_care_support_audit"]["unresolved"] == 0
    care = fields()[0]
    answers = {}
    _set_fact(answers, care, "current_care", "매주 가족의 생활 지원을 합니다.", {"kind": "conversation"})
    text = compose(care, answers[care["id"]], SimpleNamespace(title="장학금"))["text"]
    assert "매주 가족의 생활 지원" in text and "병원 동행" not in text


@pytest.mark.parametrize("problem", ["ocr", "unknown_example", "missing_evidence", "unknown_limit", "wrong_condition", "wrong_span", "extra_paragraph"])
def test_unproven_source_remains_review(problem):
    blocks = fixture()
    if problem == "ocr":
        blocks[4]["method"] = "ocr"
    elif problem == "unknown_example":
        blocks[1]["text"] = "다른 원문 지침"
    elif problem == "missing_evidence":
        blocks[3]["text"] = "다른 조건"
    elif problem == "unknown_limit":
        blocks[6]["text"] = "구간 (3구간 이내)"
    elif problem == "wrong_condition":
        blocks[5]["text"] = "항상 작성"
    elif problem == "wrong_span":
        blocks[5]["column_span"] = 9
    else:
        blocks.append({**blocks[6], "text": "추가 지침", "location": "extra"})
    selected = [g["fields"][0] for g in native_care_support(blocks, {})]
    assert audit_care_support(blocks, selected, {})["unresolved"] > 0


def test_branch_values_original_limit_and_evidence_confirmation():
    support = fields()[1]
    assert support["options"] == ["①수급자", "②차상위"]
    answers = {}
    _set_fact(answers, support, "value", "표시하지 않음", {})
    assert [s["key"] for s in requirements(support, answers[support["id"]])] == ["value", "support_bracket"]
    with pytest.raises(ValueError):
        compose(support, answers[support["id"]], SimpleNamespace(title="장학금"))
    for value in ("3", "-1", "2구간", "1.5", "네"):
        with pytest.raises(HTTPException):
            _check_choice(support, value, "support_bracket", answers[support["id"]])
    _set_fact(answers, support, "support_bracket", "2", {})
    assert "학자금 지원 구간: 2 구간" in compose(support, answers[support["id"]], SimpleNamespace(title="장학금"))["text"]
    _set_fact(answers, support, "value", "①수급자", {})
    assert set(answers[support["id"]]["facts"]) == {"value"}
    with pytest.raises(HTTPException):
        _check_choice(support, "2", "support_bracket", answers[support["id"]])
    for value in ("네", "제출 불가", "모름"):
        with pytest.raises(HTTPException):
            _check_choice(support, value, "recipient_evidence", answers[support["id"]])
    _set_fact(answers, support, "recipient_evidence", "제출 가능", {})
    text = compose(support, answers[support["id"]], SimpleNamespace(title="장학금"))["text"]
    assert "사용자 응답" in text and "학자금 지원 구간" not in text
    _set_fact(answers, support, "value", "표시하지 않음", {})
    assert set(answers[support["id"]]["facts"]) == {"value"}


def test_each_selected_status_needs_own_evidence_answer():
    support = fields()[1]
    answers = {}
    _set_fact(answers, support, "value", '["①수급자", "②차상위"]', {})
    _set_fact(answers, support, "recipient_evidence", "제출 가능", {})
    with pytest.raises(ValueError):
        compose(support, answers[support["id"]], SimpleNamespace(title="장학금"))
    _set_fact(answers, support, "near_poverty_evidence", "제출 가능", {})
    assert "②차상위" in compose(support, answers[support["id"]], SimpleNamespace(title="장학금"))["text"]


def api_session(client, db):
    row, initial = setup_writing(client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    row.eligibility = {**row.eligibility, "application_fields": [fields()[1]], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    return session


def test_api_branch_change_invalidates_export_and_asks_new_evidence(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db)
    field = session["fields"][0]
    session = action(auth_client, session, "messages", {"message": "표시하지 않음"})
    action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "2"})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "학자금 지원 구간: 2 구간" in exported.content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{field['id']}/value", {"message": "①수급자"}, method="put")
    assert session["drafts"] == [] and session["next_action"]["slot_key"] == "recipient_evidence"
    assert "support_bracket" not in session["answers"][field["id"]]["facts"]
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409


def test_audit_summary_cannot_hide_missing_branch():
    selected = fields()
    selected[1]["choice_spec"]["empty_values"] = []
    audit = audit_care_support(fixture(), selected, {"sha256": "hash"})
    assert audit["unresolved"] > 0
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": selected, "form_coverage": [{"native_care_support_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


def test_api_support_requires_evidence_before_generation(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db)
    field = session["fields"][0]
    session = action(auth_client, session, "messages", {"message": "①수급자"})
    action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True}, expected=409)
    action(auth_client, session, "messages", {"message": "제출 불가"}, expected=422)
    session = action(auth_client, session, "messages", {"message": "제출 가능"})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert "사용자 응답" in session["drafts"][0]["text"]
    assert "학자금 지원 구간" not in session["drafts"][0]["text"]


@pytest.mark.parametrize("difference", ["maximum", "missing_branch"])
def test_duplicate_source_cannot_hide_branch_conflict(difference):
    from copy import deepcopy
    from app.services.posting_documents import merge_posting_documents
    from tests.test_posting_documents import raw_posting
    original = fields()[1]
    original["layout"]["table_fingerprint"] = "same-table"
    variant = deepcopy(original)
    variant["id"] = "different-source-id"
    if difference == "maximum":
        variant["choice_spec"]["empty_values"][0]["maximum"] = 3
    else:
        variant["choice_spec"]["empty_values"] = []
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [original]}}, {"structured": {"application_fields": [variant]}}]
    assert "conflicting_duplicate_form_field" in merge_posting_documents(raw)["issues"]
