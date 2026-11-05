import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_grade_care_inputs import native_grade_care_inputs, audit_grade_care_inputs
from app.services.form_profile_inputs import check_input
from app.services.posting_extract import extract_structured
from app.services.writing_workflow import _set_fact
from app.services.writing_requirements import requirements, field_label
from app.services.writing_drafts import compose
from app.services.writing_chat import start_session
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.models.writing_session import WritingSession
from app.models.scholarship import PostingStatus
from app.schemas.schemas import UserProfile
from tests.test_writing_workflow import setup_writing, action


def fixture():
    blocks = []
    shape = {"method": "native", "table": "application", "rows": 13, "columns": 19, "issues": ["unrelated_row_issue"], "cells": []}

    def cell(row, column, span, texts, row_span=1):
        loc = f"application/r{row}/c{column}"
        shape["cells"].append({"table": "application", "row": row, "column": column, "column_span": span, "row_span": row_span,
                               "cell_location": loc, "blank": not texts, "has_control": False, "record": len(shape["cells"]),
                               "paragraph_char_counts": [len(t) + 1 for t in texts] or [1], "text_locations": [f"{loc}/text{i}" for i in range(len(texts))]})
        for i, text in enumerate(texts):
            blocks.append({"table": "application", "row": row, "column": column, "column_span": span, "row_span": row_span,
                           "cell_location": loc, "location": f"{loc}/text{i}", "text": text, "method": "native"})

    cell(4, 3, 9, ["직전학기(26-1학기)"])
    cell(4, 12, 7, ["전체학년"])
    for col, span, text in ((3, 4, "이수학점"), (7, 5, "평점"), (12, 3, "총 이수학점"), (15, 4, "평균평점")):
        cell(5, col, span, [text])
        cell(6, col, span, ["학점" if col in (3, 12) else "/4.5 or    /4.3"])
    cell(11, 1, 2, ["돌봄대상자", "질병ㆍ장애 현황"], 2)
    for row in (11, 12):
        cell(row, 3, 3, ["이름/", "본인과의 관계"])
        cell(row, 6, 2, ["/"])
        cell(row, 8, 2, ["주요 질환명·진단명"])
        cell(row, 10, 2, [])
    blocks[0]["table_structure"] = shape
    return finalize_blocks(blocks)


def fields(blocks=None):
    return [f for g in native_grade_care_inputs(blocks or fixture(), {"sha256": "hash"}) for f in g["fields"]]


def test_restore_missing_model_fields_with_exact_components(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    actual = [f for f in result["application_fields"] if f.get("input_spec", {}).get("version") == "native-grade-care-inputs-v1"]
    assert len(actual) == 4 and sum(len(f["input_spec"]["slots"]) for f in actual) == 12
    assert result["coverage"]["native_grade_care_audit"]["unresolved"] == 0
    assert all(f["role"] == "unknown" and f["required"] is None and not f.get("applicability") for f in actual)


@pytest.mark.parametrize("problem", ["ocr", "object", "overlap", "wrong_span", "changed_scale", "wrong_pointer", "extra_paragraph", "empty_paragraph"])
def test_unproven_grade_components_fail_closed(problem):
    blocks = fixture()
    prompt = next(b for b in blocks if b["row"] == 6 and b["column"] == 7)
    shape = blocks[0]["table_structure"]
    target = next(c for c in shape["cells"] if c["row"] == 6 and c["column"] == 7)
    if problem == "ocr":
        prompt["method"] = "ocr"
    elif problem == "object":
        target["has_control"] = True
    elif problem == "overlap":
        shape["cells"].append(deepcopy(target))
    elif problem == "wrong_span":
        prompt["column_span"] = 4
    elif problem == "changed_scale":
        prompt["text"] = "/5.0"
    elif problem == "wrong_pointer":
        target["text_locations"] = ["wrong"]
    elif problem == "extra_paragraph":
        blocks.append({**prompt, "text": "추가 성적 지침", "location": "extra"})
    else:
        target["paragraph_char_counts"] = [1]
    actual = fields(blocks)
    assert len(actual) == 3
    assert audit_grade_care_inputs(blocks, actual, {"sha256": "hash"})["unresolved"] == 1


@pytest.mark.parametrize("problem", ["filled", "object", "wrong_relation", "overlap"])
def test_second_recipient_not_inferred_from_first(problem):
    blocks = fixture()
    shape = blocks[0]["table_structure"]
    target = next(c for c in shape["cells"] if c["row"] == 12 and c["column"] == 10)
    if problem == "filled":
        target["blank"] = False
    elif problem == "object":
        target["has_control"] = True
    elif problem == "wrong_relation":
        next(b for b in blocks if b["row"] == 12 and b["text"] == "본인과의 관계")["text"] = "추정 관계"
    else:
        shape["cells"].append(deepcopy(target))
    actual = fields(blocks)
    assert len(actual) == 3 and actual[-1]["layout"]["row"] == 11
    assert audit_grade_care_inputs(blocks, actual, {"sha256": "hash"})["unresolved"] == 1


@pytest.mark.parametrize("key,value", [("credits", "-1"), ("gpa", "NaN"), ("gpa", "3.8/4.3"), ("scale", "5.0"), ("scale", "자동 환산")])
def test_grade_rejects_invalid_or_inferred_values(key, value):
    with pytest.raises(ValueError):
        check_input(fields()[0], key, value)


def test_gpa_scale_cross_validation_and_reconfirmation():
    grade = fields()[0]
    answers = {}
    _set_fact(answers, grade, "credits", "18.5", {})
    _set_fact(answers, grade, "gpa", "4.4", {})
    with pytest.raises(ValueError):
        compose(grade, answers[grade["id"]], SimpleNamespace(title="장학금"))
    with pytest.raises(HTTPException) as error:
        _set_fact(answers, grade, "scale", "4.3", {})
    assert error.value.status_code == 422
    _set_fact(answers, grade, "scale", "4.5", {})
    assert "4.5" in compose(grade, answers[grade["id"]], SimpleNamespace(title="장학금"))["text"]
    for fact in answers[grade["id"]]["facts"].values():
        fact["confirmed"] = True
    _set_fact(answers, grade, "gpa", "3.9", {})
    assert all(not fact["confirmed"] for fact in answers[grade["id"]]["facts"].values())
    _set_fact(answers, grade, "scale", "4.3", {})
    assert "4.3" in requirements(grade)[2]["question"]


def test_each_recipient_collects_own_name_relation_and_reported_diagnosis():
    first, second = fields()[2:]
    assert "12행" in field_label(first) and "13행" in field_label(second)
    answers = {}
    for slot in first["input_spec"]["slots"]:
        _set_fact(answers, first, slot["key"], "첫 번째 대상자 " + slot["label"], {})
    assert second["id"] not in answers
    _set_fact(answers, second, "name", "두 번째 대상자", {})
    with pytest.raises(ValueError):
        compose(second, answers[second["id"]], SimpleNamespace(title="장학금"))
    assert "첫 번째" in compose(first, answers[first["id"]], SimpleNamespace(title="장학금"))["text"]


def test_individual_audit_blocks_approval_even_if_summary_cleared():
    actual = fields()
    actual[0]["input_spec"]["slots"][2]["options"] = ["5.0"]
    audit = audit_grade_care_inputs(fixture(), actual, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": actual, "form_coverage": [{"native_grade_care_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("attribute", ["options", "scale_key", "destination"])
def test_duplicate_source_cannot_hide_changed_grade_rules(attribute):
    from app.services.posting_documents import merge_posting_documents
    from tests.test_posting_documents import raw_posting
    original = fields()[0]
    original["layout"]["table_fingerprint"] = "same-table"
    changed = deepcopy(original)
    changed["id"] = "other-source"
    if attribute == "options":
        changed["input_spec"]["slots"][2]["options"] = ["5.0"]
    elif attribute == "scale_key":
        changed["input_spec"]["slots"][1]["scale_key"] = "unrelated-scale"
    else:
        changed["input_cell_sources"].pop()
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [original]}}, {"structured": {"application_fields": [changed]}}]
    assert "conflicting_duplicate_form_field" in merge_posting_documents(raw)["issues"]


def test_api_grade_missing_scale_confirmation_export_and_edit(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    grade = fields()[0]
    row.eligibility = {**row.eligibility, "application_fields": [grade], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    session = action(auth_client, session, "messages", {"message": "18"})
    session = action(auth_client, session, "messages", {"message": "4.4"})
    action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True}, expected=409)
    action(auth_client, session, "messages", {"message": "4.3"}, expected=422)
    session = action(auth_client, session, "messages", {"message": "4.5"})
    session = action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "평점 만점 기준: 4.5" in exported.content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{grade['id']}/gpa", {"message": "4.0"}, method="put")
    assert session["drafts"] == []
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
