from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_final_destinations import audit_final_destinations
from app.services.writing_chat import start_session
from app.services.writing_requirements import requirements, field_label
from app.services.scholarship_readiness import form_fingerprint
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from tests.test_form_final_destinations import fixture as base_fixture, fields
from tests.test_writing_workflow import setup_writing, action


def fixture():
    blocks = base_fixture()
    blocks.append({"location": "consent-heading", "kind": "paragraph", "method": "native", "text": "개인[신용]정보 수집·이용·제공 동의서"})
    table, cell = "consent-outer", "consent-outer/r0/c0"
    texts = [("intro", "동의서 내용을 확인해 주세요."), ("attestation", "신청자 본인 및 돌봄대상자는 본 동의서의 내용을 이해하였으며, 내용을 확인하였습니다."),
             ("footer-date", "2026년          월           일"), ("recipient", "우양재단 貴中")]
    shape = {"method": "native", "table": table, "rows": 1, "columns": 1, "cells": [{"table": table, "row": 0, "column": 0,
             "row_span": 1, "column_span": 1, "cell_location": cell, "has_control": True, "blank": False, "text_locations": [loc for loc, _ in texts]}]}
    for index, (loc, text) in enumerate(texts):
        block = {"location": loc, "cell_location": cell, "kind": "cell", "method": "native", "table": table,
                 "row": 0, "column": 0, "row_span": 1, "column_span": 1, "text": text}
        if index == 0:
            block["table_structure"] = shape
        blocks.append(block)
    return finalize_blocks(blocks)


def test_consent_date_is_separate_from_declaration_and_not_a_blank_cell():
    actual = fields(fixture())
    footer = next(f for f in actual if f["source"]["location"] == "footer-date")
    standalone = next(f for f in actual if f["source"]["location"] == "date")
    assert footer["id"] != standalone["id"] and not requirements(footer)
    assert "개인정보동의서" in field_label(footer) and footer["required"] is None
    assert footer["original_destination"]["kind"] == "paragraph_in_cell_template" and not footer.get("input_cell_source")
    assert not footer["original_destination"]["visual_placement_verified"]
    assert audit_final_destinations(fixture(), actual, {"sha256": "hash"})["unresolved"] == 0


@pytest.mark.parametrize("problem", ["ocr", "heading", "recipient", "attestation", "pointer", "duplicate", "order"])
def test_footer_date_context_does_not_authorize_unknown_relationship(problem):
    blocks = fixture()
    date = next(b for b in blocks if b["location"] == "footer-date")
    if problem == "ocr":
        date["method"] = "ocr"
    elif problem == "heading":
        next(b for b in blocks if b["location"] == "consent-heading")["text"] = "다른 문서"
    elif problem == "recipient":
        next(b for b in blocks if b["location"] == "recipient")["text"] = "다른 수신인"
    elif problem == "attestation":
        next(b for b in blocks if b["location"] == "attestation")["text"] = "원문 관계 불명"
    elif problem == "pointer":
        next(b for b in blocks if b["location"] == "intro")["table_structure"]["cells"][0]["text_locations"].remove("footer-date")
    elif problem == "duplicate":
        blocks.append({**date, "location": "duplicate"})
    else:
        date["start"] = 0
    actual = fields(blocks)
    assert not any(f["source"]["location"] == "footer-date" for f in actual)
    assert audit_final_destinations(blocks, actual, {"sha256": "hash"})["unresolved"] > 0


def test_omitted_footer_date_remains_in_individual_audit():
    actual = [f for f in fields(fixture()) if f["source"]["location"] != "footer-date"]
    audit = audit_final_destinations(fixture(), actual, {"sha256": "hash"})
    assert audit["unresolved"] == 1 and any(c["source"]["location"] == "footer-date" and c["issues"] for c in audit["controls"])


def test_api_does_not_share_or_generate_dates_and_requires_original_review(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    row, initial = setup_writing(auth_client, db)
    old_session = db.get(WritingSession, uuid.UUID(initial["id"]))
    dates = [f for f in fields(fixture()) if f["source"]["location"] in ("date", "footer-date")]
    value = deepcopy(initial["fields"][0])
    value.update(field_type="text", label="검증용 이름", content_requirements=[], required=None)
    row.eligibility = {**row.eligibility, "application_fields": [value, *dates], "form_groups": [], "form_exclusions": [], "form_coverage": [], "extraction_issues": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, old_session.user_id, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    assert len(session["manual_items"]) == 2
    for date in dates:
        action(auth_client, session, f"fields/{date['id']}/value", {"message": "검증용 날짜"}, method="put", expected=422)
    session = action(auth_client, session, "messages", {"message": "검증용 신청 이름"})
    session = action(auth_client, session, f"fields/{value['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert not any(date["id"] in session["answers"] for date in dates) and len(session["drafts"]) == 1
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    text = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").content.decode("utf-8-sig")
    assert "개인정보동의서" in text and text.count("날짜를 자동으로 채우지 않습니다") >= 2
