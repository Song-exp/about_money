from copy import deepcopy
from types import SimpleNamespace
import json
import uuid

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_external_history import native_external_history, audit_external_history
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
    blocks, cells = [], []
    def add(text, row, col, span, paragraph=0, rowspan=1):
        blocks.append({"text": text, "table": "external", "row": row, "column": col, "column_span": span,
            "row_span": rowspan, "method": "native", "location": f"external/r{row}/c{col}/p{paragraph}",
            "cell_location": f"external/r{row}/c{col}"})
    add("외부 장학금 수혜 경험", 0, 0, 2, rowspan=5)
    add("※ 등록금으로 납부된 국가장학금, 교내장학금 제외", 0, 0, 2, 1, 5)
    add("※ 칸을 추가하여 작성 가능", 0, 0, 2, 2, 5)
    headers = [(2, 2, "수혜연도"), (4, 3, "지급처"), (7, 1, "장학명"), (8, 3, "학기당 수혜금액"),
        (11, 3, "총 (예정)수혜금액"), (14, 3, "수혜 기간"), (17, 2, "장학금 성격")]
    for col, span, title in headers:
        add(title, 0, col, span)
        if col == 8:
            add("※ 일시 장학의 경우 ‘일시’로 기입", 0, col, span, 1)
        if col == 17:
            add("(생활비, 월세지원 등)", 0, col, span, 1)
    # Native row templates: five proven blanks, printed year and date prompt.
    for row, year in enumerate((2026, 2025, 2024, 2023), 1):
        add(f"{year}년", row, 2, 2)
        add("수혜시작일 ~ 수혜종료일", row, 14, 3)
        for col, span, _ in headers:
            cells.append({"table": "external", "row": row, "column": col, "column_span": span, "row_span": 1,
                "cell_location": f"external/r{row}/c{col}", "record": row * 100 + col,
                "blank": col not in (2, 14), "has_control": col == 14,
                "text_locations": [f"external/r{row}/c{col}/p0"] if col in (2, 14) else [],
                "paragraph_char_counts": [6 if col == 2 else 46 if col == 14 else 1]})
    blocks[0]["table_structure"] = {"cells": cells, "rows": 5, "columns": 19}
    return finalize_blocks(blocks)


def field():
    return native_external_history(fixture(), {"sha256": "hash"})[0]["fields"][0]


def initialize(count="1"):
    f, answers = field(), {}
    _set_fact(answers, f, "scope_confirmed", "제외 조건 확인", {})
    _set_fact(answers, f, "entry_count", count, {})
    return f, answers


VALUES = {"year": "2024", "provider": "검증용 재단", "name": "검증용 장학금", "semester_amount": "일시",
    "total_amount": "100 만원", "start_date": "2024-02-29", "end_date": "2024-03-01", "nature": "생활비"}


def test_model_omission_restores_all_columns_and_preserves_physical_templates(monkeypatch):
    blocks = fixture()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert len(result["application_fields"]) == 1
    assert result["coverage"]["native_external_history_audit"]["unresolved"] == 0
    repeat = result["application_fields"][0]["input_spec"]["repeat"]
    assert len(repeat["columns"]) == 8 and len(repeat["templates"]) == 4
    assert sum(c["blank"] for t in repeat["templates"] for c in t["cells"]) == 20
    assert repeat["original_layout_review_required"] is True


@pytest.mark.parametrize("problem", ["ocr", "unknown_exclusion", "missing_addition", "extra_header", "filled_cell", "overlap", "wrong_year", "unknown_date_prompt"])
def test_unproven_repeat_source_requires_review(problem):
    blocks = fixture()
    if problem == "ocr":
        blocks[-1]["method"] = "ocr"
    elif problem == "unknown_exclusion":
        blocks[1]["text"] = "모든 국가장학금 제외"
    elif problem == "missing_addition":
        blocks[2]["text"] = "행 추가 불가"
    elif problem == "extra_header":
        blocks.append({**blocks[3], "location": "extra", "text": "추가 규칙"})
    elif problem == "filled_cell":
        blocks[0]["table_structure"]["cells"][1]["blank"] = False
    elif problem == "overlap":
        blocks[0]["table_structure"]["cells"].append(deepcopy(blocks[0]["table_structure"]["cells"][1]))
    elif problem == "wrong_year":
        blocks[-2]["text"] = "2022년"
    else:
        blocks[-1]["text"] = "날짜 미확인"
    selected = [g["fields"][0] for g in native_external_history(blocks, {})]
    assert not selected and audit_external_history(blocks, selected, {})["unresolved"] == 1


def test_additional_rows_zero_rows_and_count_revision_remove_old_values():
    f, answers = initialize("5")
    assert len(requirements(f, answers[f["id"]])) == 42
    with pytest.raises(ValueError):
        compose(f, answers[f["id"]], SimpleNamespace(title="장학금"))
    for row in range(5):
        for key, value in VALUES.items():
            _set_fact(answers, f, f"entry_{row}_{key}", value, {})
    draft = compose(f, answers[f["id"]], SimpleNamespace(title="장학금"))
    assert "original_repeating_layout_check_required" in draft["validation"]["warnings"]
    text = draft["text"]
    assert "5번째 내역" in text and "2024" in text and "2026" not in text
    assert "일시" in text and "100 만원" in text and "정보 수집용" not in text
    _set_fact(answers, f, "entry_count", "0", {})
    assert set(answers[f["id"]]["facts"]) == {"scope_confirmed", "entry_count"}
    assert "내역 없음(사용자 응답)" in compose(f, answers[f["id"]], SimpleNamespace(title="장학금"))["text"]
    _set_fact(answers, f, "entry_count", "1", {})
    assert "entry_0_year" not in answers[f["id"]]["facts"]


@pytest.mark.parametrize("key,value", [("scope_confirmed", "네"), ("entry_count", "101"), ("entry_count", "-1"),
    ("entry_0_year", "2024년"), ("entry_0_total_amount", "100"), ("entry_0_total_amount", "일시"),
    ("entry_0_start_date", "2023-02-29"), ("entry_0_end_date", "2024-02-28")])
def test_invalid_row_values_and_dates_rejected(key, value):
    f, answers = initialize()
    _set_fact(answers, f, "entry_0_start_date", "2024-02-29", {})
    with pytest.raises(HTTPException):
        _check_choice(f, value, key, answers[f["id"]])


def test_row_edit_invalidates_confirmation_and_checks_date_order_on_render():
    f, answers = initialize()
    for key, value in VALUES.items():
        _set_fact(answers, f, f"entry_0_{key}", value, {})
    for fact in answers[f["id"]]["facts"].values():
        fact["confirmed"] = True
    _set_fact(answers, f, "entry_0_start_date", "2024-03-02", {})
    assert all(not fact["confirmed"] for fact in answers[f["id"]]["facts"].values())
    with pytest.raises(ValueError):
        compose(f, answers[f["id"]], SimpleNamespace(title="장학금"))
    with pytest.raises(HTTPException):
        _check_choice(f, "값", "entry_1_name", answers[f["id"]])


def api_session(client, db):
    row, initial = setup_writing(client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    row.eligibility = {**row.eligibility, "application_fields": [field()], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, original.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    return session


def test_api_repeat_collection_and_txt_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db)
    f = session["fields"][0]
    for value in ("제외 조건 확인", "1", *VALUES.values()):
        session = action(auth_client, session, "messages", {"message": value})
    session = action(auth_client, session, f"fields/{f['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "2024-02-29" in exported.content.decode("utf-8-sig")


def test_api_count_change_invalidates_export_and_reasks_year(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db)
    f = session["fields"][0]
    for value in ("제외 조건 확인", "0"):
        session = action(auth_client, session, "messages", {"message": value})
    session = action(auth_client, session, f"fields/{f['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    session = action(auth_client, session, f"fields/{f['id']}/entry_count", {"message": "1"}, method="put")
    assert session["drafts"] == [] and session["next_action"]["slot_key"] == "entry_0_year"
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409


def test_audit_cannot_hide_lost_repeat_columns():
    selected = field()
    selected["input_spec"]["repeat"]["columns"].pop()
    audit = audit_external_history(fixture(), [selected], {"sha256": "hash"})
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": [selected], "form_coverage": [{"native_external_history_audit": audit}], "extraction_issues": [], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


def test_duplicate_sources_cannot_hide_lost_repeat_column():
    from app.services.posting_documents import merge_posting_documents
    from tests.test_posting_documents import raw_posting
    original = field()
    original["layout"]["table_fingerprint"] = "same-table"
    changed = deepcopy(original)
    changed["id"] = "other-source"
    changed["input_spec"]["repeat"]["columns"].pop()
    raw = raw_posting()
    raw.source_documents = [{"structured": {"application_fields": [original]}}, {"structured": {"application_fields": [changed]}}]
    assert "conflicting_duplicate_form_field" in merge_posting_documents(raw)["issues"]
