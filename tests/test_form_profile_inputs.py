import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_profile_inputs import native_profile_inputs, audit_profile_inputs
from app.services.posting_extract import extract_structured
from app.services.form_control_audit import audit_controls
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_drafts import compose
from app.services.writing_chat import start_session
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from tests.test_writing_workflow import setup_writing, action


def fixture():
    blocks = []
    def add(text, table, row, column, span=1, paragraph=0):
        blocks.append({"text": text, "table": table, "row": row, "column": column, "column_span": span,
                       "row_span": 1, "location": f"{table}/r{row}/c{column}/p{paragraph}",
                       "cell_location": f"{table}/r{row}/c{column}", "method": "native"})
    for col, text in enumerate(("우양재단 장학 수혜 경험", "□있음   □없음", "우양재단 장학 수혜 내역", "횟수:", "연도:", "장학명:")):
        add(text, "history", 0, col)
        if col == 2:
            add("(해당자만 작성)", "history", 0, col, paragraph=1)
    add("학적사항", "academic", 0, 0)
    add("대학교(        캠퍼스)              과(부)      학년    학기", "academic", 0, 1)
    add("(2026-2학기 기준으로 작성)", "academic", 0, 1, paragraph=1)
    add("주 보호자", "guardian", 0, 0)
    for col, text in zip((1, 3, 5, 7), ("이름", "본인과의 관계", "나이", "직업")):
        add(text, "guardian", 0, col)
    cells = [{"table": "guardian", "row": 0, "column": col, "row_span": 1, "column_span": 1,
              "cell_location": f"guardian/r0/c{col}", "record": col, "tag": 72, "level": 2,
              "header_offset": 8, "payload_sha256": "fixture", "text_locations": [],
              "paragraph_char_counts": [1], "has_control": False, "blank": True} for col in (2, 4, 6, 8)]
    next(b for b in blocks if b["table"] == "guardian")["table_structure"] = {
        "table": "guardian", "method": "native", "rows": 1, "columns": 9, "cells": cells, "issues": []}
    return finalize_blocks(blocks)


def extracted():
    return [f for group in native_profile_inputs(fixture(), {"sha256": "hash"}) for f in group["fields"]]


def test_source_omission_restores_all_profile_components(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    fields = result["application_fields"]
    assert len(fields) == 6
    academic = next(f for f in fields if f.get("input_spec"))
    assert [r["key"] for r in requirements(academic)] == ["school", "campus", "department", "grade", "semester"]
    assert "2026-2학기" in academic["instructions"]
    guardian = [f for f in fields if f.get("subject")]
    assert len(guardian) == 4 and all(f["subject"]["label"] == "주 보호자" for f in guardian)
    assert all(f["role"] == "unknown" and f["required"] is None for f in guardian)
    assert len({f["input_cell_source"]["cell_location"] for f in guardian}) == 4
    assert result["coverage"]["native_control_audit"]["unresolved"] == 0
    assert result["coverage"]["native_profile_audit"]["unresolved"] == 0


@pytest.mark.parametrize("problem", ["guardian_object", "guardian_not_blank", "guardian_overlap", "guardian_missing", "academic_ocr", "academic_unknown_rule", "history_missing_condition", "history_unknown_choice"])
def test_uncertain_native_input_groups_are_not_recovered(problem):
    blocks = fixture()
    owner = next(b for b in blocks if b.get("table_structure"))
    if problem.startswith("guardian"):
        cells = owner["table_structure"]["cells"]
        if problem == "guardian_object":
            cells[0]["has_control"] = True
        elif problem == "guardian_not_blank":
            cells[0]["paragraph_char_counts"] = [4]
        elif problem == "guardian_overlap":
            cells.append(deepcopy(cells[0]))
        else:
            cells.pop()
    elif problem == "academic_ocr":
        next(b for b in blocks if "캠퍼스" in b["text"])["method"] = "ocr"
    elif problem == "academic_unknown_rule":
        next(b for b in blocks if "기준으로" in b["text"])["text"] = "다른 기준 적용"
    elif problem == "history_missing_condition":
        next(b for b in blocks if "해당자만" in b["text"])["text"] = "설명"
    else:
        next(b for b in blocks if "□" in b["text"])["text"] = "□ 다른 항목"
    groups = native_profile_inputs(blocks, {"sha256": "hash"})
    assert len(groups) == 2


def test_history_choice_removes_dependent_values_and_cannot_select_both():
    history = next(f for f in extracted() if f.get("choice_spec"))
    answers = {}
    with pytest.raises(HTTPException):
        _check_choice(history, '["있음", "없음"]')
    _set_fact(answers, history, "value", "있음", {"kind": "conversation"})
    assert [r["key"] for r in requirements(history, answers[history["id"]])] == ["value", "history_count", "history_year", "history_name"]
    for text in ("0", "-1", "2회", "네"):
        with pytest.raises(HTTPException):
            _check_choice(history, text, "history_count", answers[history["id"]])
    for key, value in (("history_count", "2"), ("history_year", "2023, 2024"), ("history_name", "동행장학")):
        _set_fact(answers, history, key, value, {"kind": "conversation"})
    draft = compose(history, answers[history["id"]], SimpleNamespace(title="장학금"))
    assert draft["text"] == "있음\n횟수: 2\n연도: 2023, 2024\n장학명: 동행장학"
    _set_fact(answers, history, "value", "없음", {"kind": "conversation_revision"})
    assert set(answers[history["id"]]["facts"]) == {"value"}
    assert requirements(history, answers[history["id"]])[0]["key"] == "value"
    assert compose(history, answers[history["id"]], SimpleNamespace(title="장학금"))["text"] == "없음"
    with pytest.raises(HTTPException):
        _check_choice(history, "2", "history_count", answers[history["id"]])


def test_academic_components_are_individual_facts_and_cannot_be_acknowledgements():
    academic = next(f for f in extracted() if f.get("input_spec"))
    answers = {}
    for key, value in (("school", "경희대학교"), ("campus", "서울"), ("department", "경제학부"), ("grade", "3학년"), ("semester", "2학기")):
        _set_fact(answers, academic, key, value, {"kind": "conversation"})
    draft = compose(academic, answers[academic["id"]], SimpleNamespace(title="장학금"))
    assert "대학교: 경희대학교" in draft["text"] and "학기: 2학기" in draft["text"]
    for fact in answers[academic["id"]]["facts"].values():
        fact["confirmed"] = True
    _set_fact(answers, academic, "campus", "국제", {"kind": "conversation_revision"})
    assert all(not fact["confirmed"] for fact in answers[academic["id"]]["facts"].values())
    with pytest.raises(HTTPException):
        _check_choice(academic, "네", "school")


def session_for(client, db, kind):
    row, original = setup_writing(client, db)
    old = db.get(WritingSession, uuid.UUID(original["id"]))
    fields = [f for f in extracted() if bool(f.get("input_spec"))] if kind == "academic" else [f for f in extracted() if f.get("choice_spec")]
    row.eligibility = {**row.eligibility, "application_fields": fields, "form_groups": [], "form_exclusions": [], "extraction_issues": [], "form_coverage": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, old.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건을 확인했습니다."})
    return session


def test_api_academic_missing_components_block_confirmation_and_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = session_for(auth_client, db, "academic")
    field = session["fields"][0]
    session = action(auth_client, session, "messages", {"message": "경희대학교"})
    action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True}, expected=409)
    action(auth_client, session, "draft", expected=409)
    for value in ("서울", "경제학부", "3학년", "2학기"):
        session = action(auth_client, session, "messages", {"message": value})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "과(부): 경제학부" in exported.content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{field['id']}/campus", {"message": "국제"}, method="put")
    assert not session["drafts"] and all(not f["confirmed"] for f in session["answers"][field["id"]]["facts"].values())
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409


def test_api_history_changed_to_none_cannot_reuse_old_details(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = session_for(auth_client, db, "history")
    field = session["fields"][0]
    for value in ("있음", "1", "2024", "동행장학"):
        session = action(auth_client, session, "messages", {"message": value})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert "2024" in session["drafts"][0]["text"]
    session = action(auth_client, session, f"fields/{field['id']}/value", {"message": "없음"}, method="put")
    assert set(session["answers"][field["id"]]["facts"]) == {"value"}
    action(auth_client, session, f"fields/{field['id']}/history_year", {"message": "2025"}, method="put", expected=422)
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert session["drafts"][0]["text"] == "없음"


def test_aggregate_label_and_review_fingerprint_cannot_hide_missing_components():
    blocks = fixture()
    fields = extracted()
    next(f for f in fields if f.get("input_spec")).pop("input_spec")
    audit = audit_profile_inputs(blocks, fields, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": fields, "form_coverage": [{"native_profile_audit": audit}],
        "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert readiness(row)["writing_ready"] is False
    audit["unresolved"] = 0
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert readiness(row)["writing_ready"] is False
