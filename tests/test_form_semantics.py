import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from docx import Document
from sqlalchemy import select

from app.crawlers.base import RawPosting
from app.crawlers.registry import _enrich_from_body, upsert_posting
from app.models.scholarship import Scholarship, Category, SourceType, PostingStatus
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.document_parser import parse_document
from app.services.form_constraints import parse_constraints
from app.services.form_semantics import enrich_semantics
from app.services.posting_extract import extract_structured
from app.services.posting_documents import structure_documents
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.services.source_archive import archive_attempt
from tests.test_writing_flow import approve, recommend, select_card
from tests.test_writing_workflow import action


@pytest.mark.parametrize("proof,expected", [
    ("지원동기 (필수) 공백 제외 500~1,000자", {"required": True, "includes_spaces": False, "min_length": 500, "max_length": 1000, "length_unit": "characters"}),
    ("학업계획 (선택 항목) 500글자 이상 1000글자 이내 공백 포함", {"required": False, "includes_spaces": True, "min_length": 500, "max_length": 1000, "length_unit": "characters"}),
    ("활동계획 2페이지 이내", {"max_length": 2, "length_unit": "pages"}),
    ("내용 300 bytes 이하", {"max_length": 300, "length_unit": "bytes"}),
    ("내용 50단어 이상", {"min_length": 50, "length_unit": "words"}),
    ("내용 1.5페이지 이내", {}),
    ("원문 선택지 중 하나를 입력", {}),
    ("필수아님", {}),
])
def test_only_explicit_constraints_are_recovered(proof, expected):
    values, issues = parse_constraints(proof)
    assert values == expected and not issues


@pytest.mark.parametrize("proof", ["500자 이내 또는 1000자 이내", "1000자 이상 500자 이내", "500자 이내 500바이트 이내"])
def test_conflicting_lengths_are_withheld(proof):
    values, issues = parse_constraints(proof)
    assert issues and not any(k in values for k in ("max_length", "min_length", "length_unit"))


def test_omitted_constraints_are_recovered_and_wrong_model_values_rejected(monkeypatch):
    block = {"text": "지원동기 (필수) 공백 제외 500~1000자", "location": "p", "method": "native"}
    item = {"label": "지원동기", "field_type": "essay", "label_block_id": "b0000"}
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [item]}))
    result = extract_structured(block["text"], blocks=[block])
    field = result["application_fields"][0]
    assert field["required"] is True and field["max_length"] == 1000 and field["min_length"] == 500
    assert field["includes_spaces"] is False
    item["max_length"] = 500
    assert extract_structured(block["text"], blocks=[block])["application_fields"] == []


def test_constraint_conflicts_block_review_even_when_other_issues_are_cleared(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    result = extract_structured("지원동기 500자 이내 또는 1000자 이내", {"sha256": "hash"})
    field = result["application_fields"][0]
    assert field["max_length"] is None and field["constraint_issues"]
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "validation_version": VALIDATION_VERSION, "application_fields": [field]})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert readiness(row)["writing_ready"] is False


def test_photo_type_and_local_caption_preserve_original_proof(monkeypatch):
    blocks = [{"text": "(사진)", "location": "t/c1/p1", "table": "t", "cell_location": "t/c1", "method": "native"},
              {"text": "※ 컬러사진 첨부 흑백사진 불가", "location": "t/c1/p2", "table": "t", "cell_location": "t/c1", "method": "native"},
              {"text": "다른 파일 첨부", "location": "t/c2", "table": "t", "cell_location": "t/c2", "method": "native"}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": "(사진)", "field_type": "text", "label_block_id": "b0000"}]}))
    body = "\n".join(b["text"] for b in blocks)
    field = extract_structured(body, {"sha256": "photo"}, blocks)["application_fields"][0]
    assert field["field_type"] == "image" and field["instructions"] == blocks[1]["text"]
    source = field["instruction_sources"][0]
    assert body[source["text_start"]:source["text_end"]] == source["evidence"]


@pytest.mark.parametrize("method,table,label", [("ocr", "t", "照片"), ("native", None, "사진"), ("native", "t", "성명")])
def test_unverified_manual_type_cannot_hide_a_text_question(monkeypatch, method, table, label):
    blocks = [{"text": label, "location": "p", "method": method, "table": table}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [
        {"label": label, "field_type": "image", "label_block_id": "b0000"}]}))
    assert extract_structured(label, blocks=blocks)["application_fields"] == []


def test_applicability_does_not_escape_cell_or_person_role():
    texts = ["※ 신청인이 본인이 아닌 경우 추가 동의 서명", "성명(신청인) : (서명 또는 인)",
             "(생년월일: )", "성명(학생) : (서명 또는 인)"]
    blocks = finalize_blocks([{"text": text, "location": f"t/c/p{i}", "cell_location": "t/c", "table": "t", "method": "native"}
                              for i, text in enumerate(texts)])
    fields = []
    for b in blocks[1:]:
        field = {"label": "생년월일" if "생년월일" in b["text"] else "성명", "field_type": "text", "evidence": b["text"]}
        enrich_semantics(field, b, blocks, {"sha256": "proof"})
        fields.append(field)
    assert fields[0]["applicability"]["id"] == fields[1]["applicability"]["id"]
    assert "applicability" not in fields[2]
    blocks[2]["cell_location"] = "other"
    field = {"label": "생년월일", "field_type": "text", "evidence": blocks[2]["text"]}
    enrich_semantics(field, blocks[2], blocks, {})
    assert "applicability" not in field


@pytest.mark.parametrize("choice,expected", [(" □학업장려 장학금", ["학업장려 장학금"]),
                                           ("(동의함 □ 동의하지 않음 □)", ["동의함", "동의하지 않음"])])
def test_native_checkbox_options_strip_markers_and_override_model_variants(choice, expected):
    blocks = finalize_blocks([
        {"text": "장학금구분", "location": "t/c1", "table": "t", "row": 0, "column": 0, "column_span": 2, "method": "native"},
        {"text": choice, "location": "t/c2", "table": "t", "row": 0, "column": 2, "method": "native"},
        {"text": "□다른 문항", "location": "t/c3", "table": "t", "row": 1, "column": 2, "method": "native"},
    ])
    for model_options in ([], [choice]):
        field = {"label": "장학금구분", "field_type": "checkbox", "options": model_options}
        enrich_semantics(field, blocks[0], blocks, {"sha256": "proof"})
        assert field["options"] == expected
        assert field["option_sources"][0]["evidence"] == choice
        assert field["type_source"]["sha256"] == "proof"


def setup_semantic_writing(client, db, monkeypatch):
    document = Document()
    document.add_paragraph("지원동기 (500자 이내)")
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.text = "※ 신청인이 지원대상자 본인이 아닌 경우 추가 동의 서명"
    cell.add_paragraph("성명(신청인) : (서명 또는 인)")
    cell.add_paragraph("(생년월일: )")
    photo = document.add_table(rows=1, cols=1).cell(0, 0)
    photo.text = "(사진)"
    photo.add_paragraph("※ 컬러사진 첨부 흑백사진 불가")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "1. 수집·이용에 관한 사항"
    table.cell(1, 0).text = "수집·이용 동의 여부"
    table.cell(1, 1).text = "(동의함 □ 동의하지 않음 □)"
    stream = io.BytesIO(); document.save(stream)
    parsed = parse_document(stream.getvalue(), "신청서.docx")
    fields = []
    for i, b in enumerate(parsed["blocks"]):
        if b["text"].startswith("지원동기"):
            label, kind = "지원동기", "essay"
        elif b["text"].startswith("성명(신청인)"):
            label, kind = "성명(신청인)", "signature"
        elif b["text"] == "(사진)":
            label, kind = "(사진)", "text"
        elif b["text"] == "수집·이용 동의 여부":
            label, kind = b["text"], "text"
        else:
            continue
        fields.append({"label": label, "field_type": kind, "label_block_id": f"b{i:04d}"})
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": fields}))
    raw = RawPosting(title="조건부 신청 양식", source_url="https://x.test/conditional", source_platform="test",
                     source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
                     deadline_at=datetime.now(timezone.utc) + timedelta(days=10), body_text=parsed["extracted_text"])
    raw.source_documents = [{**parsed, "filename": "신청서.docx", "url": raw.source_url, "content": stream.getvalue()}]
    structure_documents(raw); _enrich_from_body(raw)
    attempt = archive_attempt(db, raw); upsert_posting(db, raw); db.commit()
    row = db.scalar(select(Scholarship))
    assert not row.eligibility.get("extraction_issues"), row.eligibility.get("extraction_issues")
    approve(client, row, attempt); db.refresh(row)
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    return select_card(client, recommend(client), row.id).json()


def fill_essay(client, session):
    session = action(client, session, "messages", {"message": "네, 자격을 확인했습니다."})
    session = action(client, session, "messages", {"message": "학업 교재 비용을 마련하기 위해 장학금을 신청합니다."})
    session = action(client, session, "messages", {"message": "교재를 구입해 전공 수업에 집중할 수 있습니다."})
    return action(client, session, f"fields/{session['next_action']['field_id']}/confirm", {"confirmed": True})


def test_conditions_choices_and_manual_items_reach_export_without_auto_consent(auth_client, db, monkeypatch):
    session = fill_essay(auth_client, setup_semantic_writing(auth_client, db, monkeypatch))
    assert session["next_action"]["kind"] == "applicability"
    condition = session["next_action"]["condition_id"]
    birth = next(f for f in session["fields"] if f["label"] == "생년월일")
    action(auth_client, session, f"fields/{birth['id']}/value", {"message": "2001-01-01"}, method="put", expected=409)
    session = action(auth_client, session, "messages", {"message": "모르겠어요"})
    assert session["next_action"]["kind"] == "applicability"
    session = action(auth_client, session, f"conditions/{condition}", {"applies": False})
    assert session["next_action"]["kind"] == "field"
    consent = session["next_action"]["field_id"]
    assert consent not in session["answers"]
    action(auth_client, session, "messages", {"message": "네"}, expected=422)
    assert auth_client.get(f"/writing-sessions/{session['id']}/history", params={"field_id": consent}).status_code == 422
    session = action(auth_client, session, "messages", {"message": "동의하지 않음"})
    session = action(auth_client, session, f"fields/{consent}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert len(session["drafts"]) == 1 and all(d["field_id"] != consent for d in session["drafts"])
    assert len(session["manual_items"]) == 2
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").content.decode("utf-8-sig")
    assert "동의하지 않음" in exported and "흑백사진 불가" in exported
    assert "생년월일" not in exported and "성명(신청인)" not in exported
    previous = session
    session = action(auth_client, session, f"conditions/{condition}", {"applies": True})
    assert session["drafts"] == [] and not session["draft_reviewed"]
    action(auth_client, previous, f"conditions/{condition}", {"applies": False}, expected=409)
    assert auth_client.get(f"/writing-sessions/{session['id']}/export").status_code == 409
    assert session["next_action"]["field_id"] == birth["id"]


def test_condition_request_requires_boolean_owner_and_original_id(auth_client, db, monkeypatch):
    session = setup_semantic_writing(auth_client, db, monkeypatch)
    condition = session["conditions"][0]["id"]
    action(auth_client, session, "conditions/invented", {"applies": True}, expected=422)
    action(auth_client, session, f"conditions/{condition}", {"applies": "false"}, expected=422)
    auth_client.post("/auth/logout")
    assert auth_client.post("/auth/signup", json={"privacy_consent": True, "email": "other-semantic@khu.ac.kr", "password": "test-password-2"}).status_code == 201
    action(auth_client, session, f"conditions/{condition}", {"applies": True}, expected=404)


def test_condition_correction_preserves_answers_but_excludes_inactive_drafts(auth_client, db, monkeypatch):
    session = fill_essay(auth_client, setup_semantic_writing(auth_client, db, monkeypatch))
    condition = session["next_action"]["condition_id"]
    session = action(auth_client, session, "messages", {"message": "예"})
    birth = session["next_action"]["field_id"]
    session = action(auth_client, session, "messages", {"message": "2001-01-01"})
    session = action(auth_client, session, f"fields/{birth}/confirm", {"confirmed": True})
    consent = session["next_action"]["field_id"]
    session = action(auth_client, session, "messages", {"message": "동의함"})
    session = action(auth_client, session, f"fields/{consent}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert any(d["field_id"] == birth for d in session["drafts"])
    session = action(auth_client, session, f"conditions/{condition}", {"applies": False})
    assert session["answers"][birth]["text"] == "2001-01-01"
    assert session["applicability_states"][birth] == "not_applicable"
    session = action(auth_client, session, "draft")
    assert not any(d["field_id"] == birth for d in session["drafts"])
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    base = f"/writing-sessions/{session['id']}"
    assert "2001-01-01" not in auth_client.get(base + "/export?format=txt").content.decode("utf-8-sig")
    session = action(auth_client, session, f"conditions/{condition}", {"applies": True})
    assert session["next_action"]["kind"] == "confirm" and session["next_action"]["field_id"] == birth
