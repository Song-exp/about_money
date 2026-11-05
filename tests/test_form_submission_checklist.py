import io
import json
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import HTTPException

from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services import llm
from app.services.form_external_history import input_slots
from app.services.form_submission_checklist import native_submission_checklists, audit_submission_checklists
from app.services.form_semantics import is_manual
from app.services.posting_documents import merge_posting_documents
from app.services.posting_extract import extract_structured
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.services.writing_chat import start_session
from app.services.writing_history import related, use
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact
from tests.test_form_basic_inputs import fields as basic_fields
from tests.test_writing_workflow import setup_writing, action


def fixture():
    # Immutable original capture predates this implementation, rather than generated predictions.
    path = Path(__file__).resolve().parents[1] / "reports/scholarship-improvement-20261006-native-grid-final-r1/notice-new-555354/capture.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = next(d for d in data["documents"] if d.get("filename", "").endswith(".hwp"))
    return deepcopy(doc["blocks"]), {"sha256": doc["sha256"]}


def fields():
    blocks, source = fixture()
    return [f for g in native_submission_checklists(blocks, source) for f in g["fields"]]


def test_original_example_blank_marks_rules_and_other_blanks_are_distinct():
    actual = fields()
    assert len(actual) == 7
    assert [f["input_cell_source"]["record"] for f in actual] == [143, 171, 199, 222, 288, 311, 345]
    assert [f["input_cell_source"]["blank"] for f in actual] == [False] + [True] * 6
    assert actual[0]["preparation_spec"]["example_source"]["evidence"] == "예시: √"
    assert all(is_manual(f) and f["required"] is None and f["role"] == "unknown" for f in actual)
    assert all(not f["preparation_spec"]["file_verified"] and not f["preparation_spec"]["automatic_marking"] for f in actual)
    assert "2026. 10. 1. 이후" in actual[1]["instructions"]
    assert "2026 7. 1. 이후" in actual[6]["instructions"]
    assert "사진촬영본" in actual[2]["instructions"] and "자필 서명" in actual[0]["instructions"]
    assert "택1" in actual[4]["instructions"] and "1가지 이상" in actual[4]["instructions"]


def test_printed_attachment_header_is_not_an_additional_checkbox(monkeypatch):
    blocks, source = fixture()
    header = next(b for b in blocks if b['text'] == '첨부여부')
    groups = native_submission_checklists(blocks, source)
    assert header['location'] in groups[0]['locations']
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: json.dumps({'application_fields': [
        {'label': '첨부여부', 'field_type': 'checkbox', 'evidence': '첨부여부', 'location': header['location']}]}))
    result = extract_structured('\n'.join(b['text'] for b in blocks), source, blocks)
    assert not any(f['label'] == '첨부여부' for f in result['application_fields'])
    assert len([f for f in result['application_fields'] if f.get('semantic_kind') == 'submission_checklist']) == 7


@pytest.mark.parametrize("problem", ["ocr", "filled", "control", "overlap", "span", "pointer", "title_pointer", "header", "example", "missing_rules", "missing_date", "borrowed_context", "economic_rule"])
def test_unproven_checklist_remains_reviewable(problem):
    blocks, source = fixture()
    shape = next(b["table_structure"] for b in blocks if b.get("table_structure", {}).get("table") == "BodyText/Section0/table:94")
    cell = next(c for c in shape["cells"] if c["record"] == 171)
    block = lambda r: next(b for b in blocks if b["location"].endswith(f"record:{r}"))
    if problem == "ocr":
        block(168)["method"] = "ocr"
    elif problem == "filled":
        cell["blank"] = False
    elif problem == "control":
        cell["has_control"] = True
    elif problem == "overlap":
        shape["cells"].append(deepcopy(cell))
    elif problem == "span":
        cell["column_span"] = 2
    elif problem == "pointer":
        next(c for c in shape["cells"] if c["record"] == 143)["text_locations"] = ["wrong"]
    elif problem == "title_pointer":
        next(c for c in shape["cells"] if c["row"] == 2 and c["column"] == 1)["text_locations"] = ["wrong"]
    elif problem == "header":
        block(108)["text"] = "손상된 표 머리글"
    elif problem == "example":
        block(145)["text"] = "√"
    elif problem == "missing_rules":
        blocks.remove(block(387))
    elif problem == "missing_date":
        blocks.remove(block(186))
    elif problem == "borrowed_context":
        blocks.insert(blocks.index(block(98)), {"text": "다른 문서", "method": "native", "location": "unrelated"})
    else:
        block(276)["row_span"] = 1
    assert not native_submission_checklists(blocks, source)
    assert audit_submission_checklists(blocks, [], source)["unresolved"] == 1


def test_ready_ack_is_conditional_and_change_discards_old_confirmation():
    field = fields()[1]
    answers = {}
    assert [s["key"] for s in input_slots(field)] == ["preparation_status"]
    _set_fact(answers, field, "preparation_status", "준비됨", {})
    assert [s["key"] for s in input_slots(field, answers[field["id"]])] == ["preparation_status", "rules_ack"]
    assert "발급일" in requirements(field, answers[field["id"]])[1]["question"]
    _set_fact(answers, field, "rules_ack", "원문 조건 확인", {}, confirmed=True)
    _set_fact(answers, field, "preparation_status", "준비 중", {})
    assert "rules_ack" not in answers[field["id"]]["facts"]
    assert len(requirements(field, answers[field["id"]])) == 1
    with pytest.raises(HTTPException) as exc:
        _set_fact(answers, field, "rules_ack", "원문 조건 확인", {})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("text", ["√", "O", "제출 완료", "네", "해당 없음"])
def test_original_marks_and_unverified_completion_cannot_be_preparation_status(text):
    with pytest.raises(HTTPException) as exc:
        _set_fact({}, fields()[1], "preparation_status", text, {})
    assert exc.value.status_code == 422


def test_economic_alternatives_are_not_automatically_chosen_or_copied():
    first, second = fields()[4:6]
    answers = {}
    _set_fact(answers, first, "preparation_status", "준비됨", {})
    assert second["id"] not in answers
    _set_fact(answers, second, "preparation_status", "해당 없음", {})
    assert answers[first["id"]]["facts"]["preparation_status"]["text"] == "준비됨"
    assert len(requirements(second, answers[second["id"]])) == 1
    assert "택1" in requirements(first, answers[first["id"]])[1]["question"]


def test_recovery_does_not_remove_other_original_inputs(monkeypatch):
    blocks, source = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), source, blocks)
    actual = [f for f in result["application_fields"] if f.get("semantic_kind") == "submission_checklist"]
    assert len(actual) == 7 and result["coverage"]["native_submission_checklist_audit"]["unresolved"] == 0
    assert any(f["source"]["location"].endswith("record:529") for f in result["application_fields"])
    # Source write prompts stay visible, pending separate paragraph destination review.
    assert "(작성하기)" in actual[3]["instructions"] and "(작성하기)" in actual[5]["instructions"]


def test_individual_audit_blocks_cleared_summary():
    blocks, source = fixture()
    actual = fields()
    actual[0]["preparation_spec"]["automatic_marking"] = True
    audit = audit_submission_checklists(blocks, actual, source)
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": actual, "form_coverage": [{"native_submission_checklist_audit": audit}],
        "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("change", ["ready_slots", "preparation_only", "example", "automatic_marking", "service_only"])
def test_merge_keeps_preparation_policy_conflicts(change):
    first = fields()[0]
    second = deepcopy(first)
    if change == "ready_slots":
        second["input_spec"]["ready_slots"] = []
    elif change == "preparation_only":
        second["input_spec"]["preparation_only"] = False
    elif change == "service_only":
        second["input_spec"]["slots"][0]["service_only"] = False
    elif change == "example":
        second["preparation_spec"]["example_only"] = False
    else:
        second["preparation_spec"]["automatic_marking"] = True
    raw = SimpleNamespace(source_documents=[{"structured": {"application_fields": [f]}} for f in (first, second)])
    merged = merge_posting_documents(raw)
    assert "conflicting_duplicate_form_field" in merged["issues"]


def test_history_cannot_reuse_past_preparation(db, monkeypatch):
    session = SimpleNamespace(fields=[fields()[1]], workflow={})
    with pytest.raises(HTTPException) as exc:
        related(db, session, session.fields[0]["id"])
    assert exc.value.status_code == 422
    monkeypatch.setattr("app.services.writing_history._active", lambda *args: None)
    with pytest.raises(HTTPException) as exc:
        use(db, session, session.fields[0]["id"], "preparation_status", 1, "준비됨", True, 1)
    assert exc.value.status_code == 422


def test_api_ready_state_ack_export_and_revision(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    row, initial = setup_writing(auth_client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    field, basic = fields()[1], basic_fields()[0]
    row.eligibility = {**row.eligibility, "application_fields": [field, basic], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    assert field["id"] not in session["answers"]
    session = action(auth_client, session, "messages", {"message": "준비됨"})
    action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True}, expected=409)
    action(auth_client, session, f"fields/{field['id']}/rules_ack", {"message": "네"}, method="put", expected=422)
    session = action(auth_client, session, "messages", {"message": "원문 조건 확인"})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "messages", {"message": "검증용 대학교"})
    session = action(auth_client, session, f"fields/{basic['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert [d["field_id"] for d in session["drafts"]] == [basic["id"]]
    assert session["manual_items"][0]["selection"] == "준비됨\n\n원문 조건 확인"
    action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": False}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    url = f"/writing-sessions/{session['id']}/export"
    txt, docx = auth_client.get(url + "?format=txt"), auth_client.get(url + "?format=docx")
    assert txt.status_code == docx.status_code == 200
    for content in (txt.content.decode("utf-8-sig"), "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)):
        assert "사용자 보고 준비 상태: 준비됨" in content and "2026. 10. 1. 이후" in content
        assert "제출 완료를 뜻하지 않습니다" in content
    session = action(auth_client, session, f"fields/{field['id']}/preparation_status", {"message": "준비 중"}, method="put")
    assert "rules_ack" not in session["answers"][field["id"]]["facts"]
    assert not session["drafts"] and not session["draft_reviewed"]
    assert auth_client.get(url + "?format=txt").status_code == 409
