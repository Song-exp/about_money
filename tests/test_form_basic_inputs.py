import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_basic_inputs import native_basic_inputs, audit_basic_inputs
from app.services.posting_extract import extract_structured
from app.services.writing_workflow import _set_fact
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
    def table(name, rows, columns, cells):
        shape = {"method": "native", "table": name, "rows": rows, "columns": columns, "issues": [], "cells": []}
        first = None
        for row, col, span, text in cells:
            location = f"{name}/r{row}/c{col}"
            shape["cells"].append({"table": name, "row": row, "column": col, "column_span": span, "row_span": 1,
                "cell_location": location, "blank": not text, "has_control": False, "paragraph_char_counts": [len(text) + 1],
                "text_locations": [location + "/text"] if text else [], "record": len(shape["cells"])})
            if text:
                b = {"table": name, "row": row, "column": col, "column_span": span, "row_span": 1,
                    "method": "native", "location": location + "/text", "cell_location": location, "text": text}
                blocks.append(b)
                first = first or b
        first["table_structure"] = shape
    table("cover", 2, 4, [(0, 0, 1, "학교"), (0, 1, 1, ""), (0, 2, 1, "학과"), (0, 3, 1, ""),
        (1, 0, 1, "학년"), (1, 1, 1, "학년 /     학기"), (1, 2, 1, "성명"), (1, 3, 1, "")])
    table("contact", 3, 19, [(0, 0, 3, "성  명"), (0, 3, 7, ""), (0, 10, 5, "주민등록번호"), (0, 15, 4, "-"),
        (1, 0, 3, "연락처"), (1, 3, 7, ""), (1, 10, 5, "이메일"), (1, 15, 4, ""),
        (2, 0, 3, "주  소"), (2, 3, 7, ""), (2, 10, 2, "주거 형태"), (2, 12, 7, "□ 자가(      만원)")])
    return finalize_blocks(blocks)


def fields():
    return [f for g in native_basic_inputs(fixture(), {"sha256": "hash"}) for f in g["fields"]]


def test_omitted_model_fields_restore_destinations_and_merge_grade_prompt(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    actual = [f for f in result["application_fields"] if f.get("input_spec", {}).get("version") == "native-basic-inputs-v1"]
    assert len(actual) == 8 and result["coverage"]["native_basic_input_audit"]["unresolved"] == 0
    grade = next(f for f in actual if f["label"] == "학년")
    assert [s["key"] for s in grade["input_spec"]["slots"]] == ["grade", "semester"]
    assert not any(f["label"] == "학년 /     학기" for f in result["application_fields"])
    assert all(f["role"] == "unknown" and f["required"] is None for f in actual)
    assert sum(f["input_cell_source"]["blank"] for f in actual) == 7


@pytest.mark.parametrize("problem", ["ocr", "filled", "object", "overlap", "wrong_span", "unknown_prompt", "extra_paragraph", "wrong_text_pointer"])
def test_unproven_basic_destination_requires_review(problem):
    blocks = fixture()
    shape = blocks[0]["table_structure"]
    if problem == "ocr":
        blocks[0]["method"] = "ocr"
    elif problem == "filled":
        shape["cells"][1]["blank"] = False
    elif problem == "object":
        shape["cells"][1]["has_control"] = True
    elif problem == "overlap":
        shape["cells"][-1] = deepcopy(shape["cells"][1])
    elif problem == "wrong_span":
        shape["cells"][1]["column_span"] = 2
    elif problem == "unknown_prompt":
        next(b for b in blocks if b["text"].startswith("학년 /"))["text"] = "임의 학적 지침"
    elif problem == "extra_paragraph":
        blocks.append({**blocks[0], "text": "추가 지침", "location": "extra", "table_structure": None})
    else:
        shape["cells"][5]["text_locations"] = ["wrong"]
    selected = [f for g in native_basic_inputs(blocks, {"sha256": "hash"}) for f in g["fields"]]
    assert len(selected) == 4 and audit_basic_inputs(blocks, selected, {"sha256": "hash"})["unresolved"] == 1


def test_same_labels_in_different_tables_keep_separate_answers():
    names = [f for f in fields() if "성" in f["label"]]
    assert len(names) == 2 and names[0]["id"] != names[1]["id"]
    answers = {}
    _set_fact(answers, names[0], "name", "검증용 표지 이름", {})
    assert names[1]["id"] not in answers
    _set_fact(answers, names[1], "name", "검증용 본문 이름", {})
    assert "본문" in compose(names[1], answers[names[1]["id"]], SimpleNamespace(title="장학금"))["text"]


def test_other_rows_issues_do_not_discard_separately_proven_contact_cells():
    blocks = fixture()
    next(b["table_structure"] for b in blocks if b.get("table_structure") and b["table"] == "contact")["issues"].append("other_rows_require_review")
    actual = [f for g in native_basic_inputs(blocks, {"sha256": "hash"}) for f in g["fields"]]
    assert len(actual) == 8 and audit_basic_inputs(blocks, actual, {"sha256": "hash"})["unresolved"] == 0


@pytest.mark.parametrize("problem", ["filled", "overlap", "label_span"])
def test_unproven_contact_cell_does_not_bind_basic_answers(problem):
    blocks = fixture()
    owner = next(b for b in blocks if b.get("table_structure") and b["table"] == "contact")
    shape = owner["table_structure"]
    if problem == "filled":
        shape["cells"][7]["blank"] = False
    elif problem == "overlap":
        shape["cells"].append(deepcopy(shape["cells"][7]))
    else:
        next(b for b in blocks if b["text"] == "이메일")["column_span"] = 6
    actual = [f for g in native_basic_inputs(blocks, {"sha256": "hash"}) for f in g["fields"]]
    assert len(actual) == 4 and audit_basic_inputs(blocks, actual, {"sha256": "hash"})["unresolved"] == 1


def test_grade_requires_both_components_and_revision_unconfirms_group():
    grade = next(f for f in fields() if f["label"] == "학년")
    assert len(requirements(grade)) == 2
    answers = {}
    _set_fact(answers, grade, "grade", "3학년", {})
    with pytest.raises(ValueError):
        compose(grade, answers[grade["id"]], SimpleNamespace(title="장학금"))
    _set_fact(answers, grade, "semester", "2학기", {})
    assert "학년: 3학년\n학기: 2학기" == compose(grade, answers[grade["id"]], SimpleNamespace(title="장학금"))["text"]
    for fact in answers[grade["id"]]["facts"].values():
        fact["confirmed"] = True
    _set_fact(answers, grade, "semester", "1학기", {})
    assert all(not f["confirmed"] for f in answers[grade["id"]]["facts"].values())


def test_missing_destination_cannot_be_approved_by_clearing_summary():
    actual = fields()
    actual[0]["input_cell_source"]["cell_location"] = "wrong"
    audit = audit_basic_inputs(fixture(), actual, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": actual, "form_coverage": [{"native_basic_input_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


def test_api_grade_collection_confirmation_and_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    grade = next(f for f in fields() if f["label"] == "학년")
    row.eligibility = {**row.eligibility, "application_fields": [grade], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    session = action(auth_client, session, "messages", {"message": "3학년"})
    action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "2학기"})
    session = action(auth_client, session, f"fields/{grade['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "학기: 2학기" in exported.content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{grade['id']}/semester", {"message": "1학기"}, method="put")
    assert session["drafts"] == []
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
