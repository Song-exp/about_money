import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_family_inputs import native_family_inputs, audit_family_inputs
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
    def add(text, row, col, span=1, paragraph=0):
        blocks.append({"text": text, "table": "family", "row": row, "column": col, "column_span": span,
                       "row_span": 1, "method": "native", "location": f"family/r{row}/c{col}/p{paragraph}",
                       "cell_location": f"family/r{row}/c{col}"})
    add("동거 가족", 0, 0)
    add("조부(   ), 조모(   ), 부(   ), 모(   )", 0, 1)
    add("※ 해당하는 가족 구성원에 ‘O’ 표시", 0, 1, paragraph=1)
    add("형제자매(   )남 (   )녀 중 (   )째", 0, 2)
    add("기타 동거가족", 1, 0)
    add("※ 있을 시 작성", 1, 1)
    add("가족월평균수입", 2, 0)
    add("(본인 포함 동거가족)", 2, 0, paragraph=1)
    add("(세후)            만원", 2, 1)
    add("수입출처", 2, 2)
    add("정부지원　   　원 /근로수입        원 /후원금        원", 2, 3)
    return finalize_blocks(blocks)


def fields():
    return [g["fields"][0] for g in native_family_inputs(fixture(), {"sha256": "hash"})]


def test_source_recovery_and_family_mark_without_invented_empty_option(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert len(result["application_fields"]) == 5
    assert result["coverage"]["native_family_audit"]["unresolved"] == 0
    marks = next(f for f in fields() if f.get("choice_spec"))
    assert marks["options"] == ["조부", "조모", "부", "모"]
    answers = {}
    _set_fact(answers, marks, "value", '["조부", "모"]', {"kind": "conversation"})
    assert compose(marks, answers[marks["id"]], SimpleNamespace(title="장학금"))["text"] == "조부(O)\n모(O)"
    _set_fact(answers, marks, "value", "[]", {"kind": "conversation_revision"})
    assert answers[marks["id"]]["facts"]["value"]["text"] == "표시하지 않음"
    assert compose(marks, answers[marks["id"]], SimpleNamespace(title="장학금"))["text"] == "표시하지 않음"
    assert "표시하지 않음" not in marks["options"]


@pytest.mark.parametrize("problem", ["ocr", "wrong_mark_rule", "unknown_family", "unknown_unit", "missing_condition", "wrong_span"])
def test_unproven_family_grammar_stays_review(problem):
    blocks = fixture()
    if problem == "ocr":
        blocks[1]["method"] = "ocr"
    elif problem == "wrong_mark_rule":
        blocks[2]["text"] = "임의의 표시"
    elif problem == "unknown_family":
        blocks[1]["text"] = "배우자(   )"
    elif problem == "unknown_unit":
        blocks[-1]["text"] = "정부지원 만원 /근로수입 원 /후원금 원"
    elif problem == "missing_condition":
        blocks[5]["text"] = "설명"
    else:
        blocks[4]["column_span"] = 2
    groups = native_family_inputs(blocks, {"sha256": "hash"})
    assert len(groups) < 5
    assert audit_family_inputs(blocks, [g["fields"][0] for g in groups], {"sha256": "hash"})["unresolved"] > 0


def test_numeric_values_keep_source_units_and_no_inferred_sibling_total():
    siblings = fields()[1]
    income = fields()[-1]
    answers = {}
    for key, value in (("male_count", "0"), ("female_count", "0"), ("birth_order", "1")):
        _set_fact(answers, siblings, key, value, {"kind": "conversation"})
    for key, value in (("government_income", "0"), ("work_income", "1200000"), ("donation_income", "30000")):
        _set_fact(answers, income, key, value, {"kind": "conversation"})
    draft = compose(income, answers[income["id"]], SimpleNamespace(title="장학금"))
    assert draft["text"] == "정부지원: 0 원\n근로수입: 1200000 원\n후원금: 30000 원"
    for key, value, target in (("male_count", "-1", siblings), ("birth_order", "0", siblings),
                               ("government_income", "10만원", income), ("work_income", "네", income)):
        with pytest.raises(HTTPException):
            _check_choice(target, value, key)


def api_session(client, db, label):
    row, initial = setup_writing(client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    selected = [f for f in fields() if f["label"] == label]
    row.eligibility = {**row.eligibility, "application_fields": selected, "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    return session


def test_api_income_requires_all_three_values_before_confirmation_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db, "수입출처")
    field = session["fields"][0]
    session = action(auth_client, session, "messages", {"message": "0"})
    action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True}, expected=409)
    for value in ("1200000", "30000"):
        session = action(auth_client, session, "messages", {"message": value})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "근로수입: 1200000 원" in exported.content.decode("utf-8-sig")


def test_api_other_family_condition_change_drops_old_answer_and_blocks_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db, "기타 동거가족")
    field = session["fields"][0]
    condition = field["applicability"]["id"]
    session = action(auth_client, session, f"conditions/{condition}", {"applies": True})
    session = action(auth_client, session, "messages", {"message": "검증용 친척과 동거합니다."})
    session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    session = action(auth_client, session, f"conditions/{condition}", {"applies": False})
    assert field["id"] not in session["answers"] and session["drafts"] == []
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    session = action(auth_client, session, f"conditions/{condition}", {"applies": True})
    assert session["next_action"]["slot_key"] == "other_family"
    assert field["id"] not in session["answers"]


def test_family_audit_cannot_be_bypassed_with_cleared_summary():
    blocks = fixture()
    selected = fields()
    next(f for f in selected if f["label"] == "수입출처")["input_spec"]["slots"].pop()
    audit = audit_family_inputs(blocks, selected, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": selected, "form_coverage": [{"native_family_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]
