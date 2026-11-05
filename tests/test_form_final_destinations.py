import json
import uuid
import io
from copy import deepcopy

import pytest
from fastapi import HTTPException
from docx import Document
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_final_destinations import bind_final_destinations, audit_final_destinations, ESSAY_TITLES
from app.services.form_profile_inputs import _field
from app.services.form_semantics import is_manual
from app.services.posting_extract import extract_structured
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact
from app.services.writing_chat import start_session
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from types import SimpleNamespace
from tests.test_form_basic_inputs import fixture as basic_fixture
from tests.test_writing_workflow import setup_writing, action


def fixture():
    blocks = basic_fixture()
    for loc, text in (("date", "2026년      월      일"), ("sign", "신청자:                 (인/서명)   *서명 또는 도장 필수"),
                      ("heading", "[ 자기소개서 ]"), ("rule", "※ 총 3장 이내로 작성 / 맑은고딕 / 글씨크기 10pt / 줄 간격 160 / 아래 질문에 벗어나지 않게 답변해 주세요.")):
        blocks.append({"location": loc, "text": text, "kind": "paragraph", "method": "native"})
    titles = sorted(ESSAY_TITLES)
    for number in range(2):
        table = f"essays{number}"
        shape = {"table": table, "method": "native", "rows": 4, "columns": 1, "issues": [], "cells": []}
        first = None
        for row in range(4):
            loc = f"{table}/r{row}/c0"
            text = titles[number * 2 + row // 2] if row % 2 == 0 else ""
            shape["cells"].append({"table": table, "row": row, "column": 0, "row_span": 1, "column_span": 1,
                                   "cell_location": loc, "record": row, "blank": not text, "has_control": False,
                                   "paragraph_char_counts": [len(text) + 1], "text_locations": [loc + "/text"] if text else []})
            if text:
                b = {"location": loc + "/text", "cell_location": loc, "text": text, "kind": "cell", "method": "native",
                     "table": table, "row": row, "column": 0, "row_span": 1, "column_span": 1}
                blocks.append(b)
                first = first or b
        first["table_structure"] = shape
    return finalize_blocks(blocks)


def fields(blocks=None):
    blocks = blocks or fixture()
    essays = []
    for block in blocks:
        if block["text"] in ESSAY_TITLES:
            field = _field(block, {"sha256": "hash"}, block["location"])
            field.update(field_type="essay", content_requirements=[{"kind": "fixture_detail", "key": "preserved"}])
            essays.append(field)
    return bind_final_destinations(blocks, essays, {"sha256": "hash"})


def test_original_only_items_and_essay_destinations_preserve_contents():
    actual = fields()
    essays = [f for f in actual if f["field_type"] == "essay"]
    manual = [f for f in actual if is_manual(f)]
    assert len(essays) == 4 and len(manual) == 3
    assert all(f["input_cell_source"]["blank"] and f["content_requirements"][0]["key"] == "preserved" for f in essays)
    assert all(not requirements(f) and f["manual_spec"]["mode"] == "original_only" for f in manual)
    assert audit_final_destinations(fixture(), actual, {"sha256": "hash"})["unresolved"] == 0
    identity = next(f for f in actual if f["label"] == "주민등록번호")
    assert identity["layout"]["column"] == 15 and identity["required"] is None
    assert all(f["original_destination"]["visual_placement_verified"] is False for f in manual if f.get("original_destination"))


def test_omitted_model_essays_and_manual_titles_are_recovered(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert result["coverage"]["native_final_destination_audit"]["unresolved"] == 0
    assert len([f for f in result["application_fields"] if f["field_type"] == "essay"]) == 4
    assert len(result["form_groups"]) == 1 and len(result["form_groups"][0]["field_ids"]) == 4


@pytest.mark.parametrize("problem", ["filled", "object", "overlap", "wrong_span", "ocr", "wrong_pointer", "wrong_paragraph", "table_issue"])
def test_unproven_essay_destinations_require_review(problem):
    blocks = fixture()
    owner = next(b for b in blocks if b.get("table") == "essays0")
    shape = owner["table_structure"]
    cell = shape["cells"][1]
    if problem == "filled":
        cell["blank"] = False
    elif problem == "object":
        cell["has_control"] = True
    elif problem == "overlap":
        shape["cells"].append(deepcopy(cell))
    elif problem == "wrong_span":
        cell["column_span"] = 2
    elif problem == "ocr":
        owner["method"] = "ocr"
    elif problem == "wrong_pointer":
        shape["cells"][0]["text_locations"] = ["wrong"]
    elif problem == "wrong_paragraph":
        cell["paragraph_char_counts"] = [2]
    else:
        shape["issues"] = ["invalid"]
    actual = fields(blocks)
    assert audit_final_destinations(blocks, actual, {"sha256": "hash"})["unresolved"] > 0


@pytest.mark.parametrize("problem", ["filled_prompt", "object", "ocr", "extra_pointer", "blank_marker", "title_span"])
def test_identity_cell_requires_literal_original_prompt(problem):
    blocks = fixture()
    owner = next(b for b in blocks if b.get("table") == "contact")
    target = next(c for c in owner["table_structure"]["cells"] if (c["row"], c["column"]) == (0, 15))
    prompt = next(b for b in blocks if b.get("table") == "contact" and b.get("row") == 0 and b.get("column") == 15)
    if problem == "filled_prompt":
        prompt["text"] = "사용자가 이미 작성한 값"
    elif problem == "object":
        target["has_control"] = True
    elif problem == "ocr":
        prompt["method"] = "ocr"
    elif problem == "extra_pointer":
        target["text_locations"].append("extra")
    elif problem == "blank_marker":
        target["blank"] = True
    else:
        next(b for b in blocks if b["text"] == "주민등록번호")["column_span"] = 4
    assert audit_final_destinations(blocks, fields(blocks), {"sha256": "hash"})["unresolved"] == 1


@pytest.mark.parametrize("problem", ["ocr_date", "filled_date", "intervening_paragraph", "ocr_signature"])
def test_signature_and_date_relation_not_inferred(problem):
    blocks = fixture()
    date = next(b for b in blocks if b["location"] == "date")
    signer = next(b for b in blocks if b["location"] == "sign")
    if problem == "ocr_date":
        date["method"] = "ocr"
    elif problem == "filled_date":
        date["text"] = "2026년 10월 10일"
    elif problem == "ocr_signature":
        signer["method"] = "ocr"
    else:
        blocks.insert(blocks.index(signer), {**date, "location": "intervening", "text": "다른 항목"})
    assert not any(f.get("original_destination") for f in fields(blocks))
    assert audit_final_destinations(blocks, fields(blocks), {"sha256": "hash"})["unresolved"] > 0


def test_manual_identity_cannot_be_written_through_internal_fact_path():
    identity = next(f for f in fields() if f["label"] == "주민등록번호")
    answers = {}
    with pytest.raises(HTTPException) as exc:
        _set_fact(answers, identity, "value", "검증용 비민감 문자열", {})
    assert exc.value.status_code == 422 and answers == {}


def test_cleared_audit_summary_cannot_approve_missing_destination():
    actual = fields()
    actual[0].pop("input_cell_source")
    audit = audit_final_destinations(fixture(), actual, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": actual, "form_coverage": [{"native_final_destination_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("attribute", ["policy", "destination", "action"])
def test_duplicate_source_cannot_hide_original_only_policy(attribute):
    from app.services.posting_documents import merge_posting_documents
    from tests.test_posting_documents import raw_posting
    original = next(f for f in fields() if f["label"] == "주민등록번호")
    original["layout"]["table_fingerprint"] = "same-table"
    changed = deepcopy(original)
    changed["id"] = "other-source"
    if attribute == "policy":
        changed.pop("manual_spec")
    elif attribute == "destination":
        changed["input_cell_source"]["column"] = 16
    else:
        changed["manual_spec"]["action"] = "다른 작성 지침"
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [original]}}, {"structured": {"application_fields": [changed]}}]
    assert "conflicting_duplicate_form_field" in merge_posting_documents(raw)["issues"]


def test_api_direct_original_items_not_collected_and_exported_as_instructions(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    row, initial = setup_writing(auth_client, db)
    original_session = db.get(WritingSession, uuid.UUID(initial["id"]))
    manual = [f for f in fields() if is_manual(f)]
    value = deepcopy(initial["fields"][0])
    value.update(field_type="text", label="검증용 이름", input_spec=None, content_requirements=[], required=None)
    row.eligibility = {**row.eligibility, "application_fields": [value, *manual], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original_session.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    assert len(session["manual_items"]) == 3 and all(item["action"] for item in session["manual_items"])
    for item in manual:
        action(auth_client, session, f"fields/{item['id']}/value", {"message": "검증용 비민감 문자열"}, method="put", expected=422)
    session = action(auth_client, session, "messages", {"message": "검증용 신청 이름"})
    session = action(auth_client, session, f"fields/{value['id']}/confirm", {"confirmed": True})
    assert session["next_action"] is None and not any(f["id"] in session["answers"] for f in manual)
    session = action(auth_client, session, "draft")
    assert len(session["drafts"]) == 1
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200
    text = exported.content.decode("utf-8-sig")
    assert "주민등록번호는 채팅에 입력하지 말고" in text and "날짜를 자동으로" in text and "도장을 찍으세요" in text
    docx = auth_client.get(f"/writing-sessions/{session['id']}/export?format=docx")
    assert docx.status_code == 200 and "주민등록번호는 채팅에 입력하지 말고" in "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    session = action(auth_client, session, f"fields/{value['id']}/value", {"message": "수정한 신청 이름"}, method="put")
    assert session["drafts"] == [] and auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
