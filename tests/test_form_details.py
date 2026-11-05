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
from app.models.scholarship import Category, SourceType, Scholarship, PostingStatus
from app.models.writing_session import WritingSession
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_consent import native_consent_controls, reconcile_control
from app.services.form_details import enrich_details
from app.services.posting_extract import extract_structured
from app.services.posting_documents import structure_documents
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.services.source_archive import archive_attempt
from app.services.writing_requirements import requirements
from tests.test_writing_flow import approve, recommend, select_card
from tests.test_writing_workflow import action


def consent_blocks():
    result = []
    for index, title in enumerate(("1. 개인정보 수집·이용(필수)", "2. 주민등록번호 처리(필수)", "3. 개인정보 제3자 제공·조회(필수)")):
        table = f"t{index}"
        for row, column, texts in ((0, 0, [title]), (1, 0, ["목적"]), (1, 1, ["원문이 지정한 개인정보 처리 목적입니다."]),
                                   (2, 0, ["수집·이용", "동의 여부"]),
                                   (2, 1, ["위와 같이 정보를 처리하는 것에 동의합니다. (동의함 □ 동의하지 않음 □)"])):
            for p, text in enumerate(texts):
                result.append({"text": text, "location": f"{table}/r{row}/c{column}/p{p}", "cell_location": f"{table}/r{row}/c{column}",
                               "table": table, "row": row, "column": column, "column_span": 2 if row == 0 else 1, "method": "native", "kind": "cell"})
    finalize_blocks(result)
    for index, b in enumerate(result):
        b["block_id"] = f"b{index:04d}"
    return result


def test_omitted_native_consent_controls_are_recovered_without_llm():
    blocks = consent_blocks()
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "original"}, blocks)
    fields = result["application_fields"]
    assert len(fields) == 3
    assert [f["section"]["kind"] for f in fields] == ["collection_use", "identity_processing", "provision_inquiry"]
    assert all(f["source"]["location"].endswith("/r2/c0/p0") for f in fields)
    assert all(f["required"] is True and f["options"] == ["동의함", "동의하지 않음"] for f in fields)
    assert all("처리 목적" in f["consent_context"]["text"] for f in fields)
    assert result["coverage"]["native_consent_controls"] == {"expected": 3, "missing_block_ids": []}
    assert not result["issues"]


def test_model_section_heading_or_value_pointer_is_resolved_without_duplicates(monkeypatch):
    blocks = consent_blocks()
    candidates = [{"label": b["text"], "field_type": "checkbox", "label_block_id": b["block_id"], "options": ["원문에 없는 동의 선택"]}
                  for b in blocks if b["row"] == 0 or b["row"] == 2 and b["column"] == 1]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": candidates}))
    traces = []
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "original"}, blocks, trace_sink=traces.append)
    assert len(result["application_fields"]) == 3 and not result["issues"]
    assert sum(d.get("source_resolution") == "native_consent_input_row" for d in traces[0]["field_decisions"]) == 6
    assert all(f["options"] == ["동의함", "동의하지 않음"] for f in result["application_fields"])
    controls = native_consent_controls(blocks)
    invalid = {"label": controls[0]["label"], "label_block_id": "missing"}
    assert reconcile_control(invalid, controls) == invalid
    ambiguous = {"label": controls[0]["label"]}
    assert reconcile_control(ambiguous, controls) == ambiguous


@pytest.mark.parametrize("change", ["different_row", "different_column", "ocr", "no_choice"])
def test_unproven_options_are_never_native_recovered(change):
    blocks = consent_blocks()[:6]
    choice = blocks[-1]
    if change == "different_row":
        choice["row"] = 3
    elif change == "different_column":
        choice["column"] = 3
    elif change == "ocr":
        choice["method"] = "ocr"
    else:
        choice["text"] = "원문에는 선택 표시가 없습니다."
    assert not native_consent_controls(blocks)


def detail_blocks():
    return finalize_blocks([
        {"text": "1. 본인에 대한 소개와 가족 돌봄 상황을 적어주세요.", "location": "q/0", "cell_location": "q", "method": "native", "kind": "cell", "table": "t", "row": 0, "column": 0},
        {"text": "가족 구성과 현재 경제 상황을 설명해 주세요. 가족의 건강 상태, 치료 여부, 이동 방법과 치료비 부담도 함께 적어주세요. 어떤 돌봄을 얼마나 자주 제공하는지, 학업과 생계에 어떤 영향을 받고 있는지 구체적으로 작성해 주세요.", "location": "q/1", "cell_location": "q", "method": "native", "kind": "cell", "table": "t", "row": 0, "column": 0},
        {"text": "※ 이전 지원 내역은 선발에 영향을 미치지 않으니 편하게 작성해 주세요.", "location": "q/2", "cell_location": "q", "method": "native", "kind": "cell", "table": "t", "row": 0, "column": 0},
        {"text": "다른 문항의 경험과 성과를 설명해 주세요.", "location": "other", "cell_location": "other", "method": "native", "kind": "cell", "table": "t", "row": 2, "column": 0}])


def test_exact_original_requests_include_introduction_and_exclude_notes_and_other_cells():
    blocks = detail_blocks()
    field = {"id": "essay", "label": blocks[0]["text"], "field_type": "essay", "evidence": blocks[0]["text"], "source": {"sha256": "hash"}}
    enrich_details(field, blocks[0], blocks, {"sha256": "hash"})
    rules = field["content_requirements"]
    assert len(rules) == 10 and len(requirements(field)) == 12
    assert any(r["evidence"] == "본인에 대한 소개" for r in rules)
    assert any(r["evidence"] == "학업과 생계에 어떤 영향을 받고 있는지" for r in rules)
    assert not any("선발" in r["evidence"] or "성과" in r["evidence"] for r in rules)
    text = "\n".join(b["text"] for b in blocks)
    for rule in rules:
        for name in ("source", "context_source"):
            source = rule[name]
            assert text[source["text_start"]:source["text_end"]] == source["evidence"]
    keys = [r["key"] for r in rules]
    copy = {"id": "different-field", "label": field["label"], "field_type": "essay", "evidence": field["evidence"], "source": {"sha256": "different"}}
    enrich_details(copy, blocks[0], blocks, {"sha256": "different"})
    assert [r["key"] for r in copy["content_requirements"]] == keys


def test_detail_parser_stops_at_new_prompt_and_keeps_conditional_clauses():
    blocks = detail_blocks()
    blocks[1]["text"] = "지원이 있었는지, 있었다면 어떤 도움을 받았는지, 필요한 지원이 무엇인지 작성해 주세요."
    blocks[2]["text"] = "2. 다음 질문의 내용과 목표를 설명해 주세요."
    finalize_blocks(blocks)
    field = {"label": "기존 지원 경험", "field_type": "essay", "evidence": "기존 지원 경험", "source": {}}
    enrich_details(field, blocks[0], blocks, {})
    rules = field["content_requirements"]
    assert any(r["evidence"] == "있었다면 어떤 도움을 받았는지" for r in rules)
    assert not any("목표" in r["evidence"] for r in rules)


def setup_details(client, db, monkeypatch):
    text = "가족 구성과 경제 상황을 설명해 주세요."
    document = Document()
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.text = "신청 이유"
    cell.add_paragraph(text)
    for heading in ("개인정보 수집·이용(필수)", "주민등록번호 처리(필수)", "개인정보 제3자 제공·조회(필수)"):
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1)).text = heading
        table.cell(1, 0).text = "목적"
        table.cell(1, 1).text = "원문에서 정한 실제 처리 목적입니다."
        table.cell(2, 0).text = "수집·이용"
        table.cell(2, 0).add_paragraph("동의 여부")
        table.cell(2, 1).text = "위와 같이 정보를 처리합니다. (동의함 □ 동의하지 않음 □)"
    stream = io.BytesIO(); document.save(stream)
    from app.services.document_parser import parse_document
    parsed = parse_document(stream.getvalue(), "신청서.docx")
    index = next(i for i, b in enumerate(parsed["blocks"]) if b["text"] == "신청 이유")
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": [{"label": "신청 이유", "field_type": "essay", "label_block_id": f"b{index:04d}"}]}))
    raw = RawPosting(title="세부 요청 양식", source_url="https://x.test/details", source_platform="test", source_type=SourceType.PUBLIC,
                     category=Category.SCHOLARSHIP, deadline_at=datetime.now(timezone.utc) + timedelta(days=10), body_text=parsed["extracted_text"])
    raw.source_documents = [{**parsed, "url": raw.source_url, "filename": "신청서.docx", "content": stream.getvalue()}]
    structure_documents(raw); _enrich_from_body(raw)
    attempt = archive_attempt(db, raw); upsert_posting(db, raw); db.commit()
    row = db.scalar(select(Scholarship))
    assert not row.eligibility.get("extraction_issues"), row.eligibility.get("extraction_issues")
    approve(client, row, attempt); db.refresh(row)
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    return select_card(client, recommend(client), row.id).json()


def test_collect_all_original_details_explicit_absence_and_direct_consent_before_draft(auth_client, db, monkeypatch):
    session = setup_details(auth_client, db, monkeypatch)
    session = action(auth_client, session, "messages", {"message": "네, 자격이 모두 해당합니다."})
    first = session["next_action"]["slot_key"]
    session = action(auth_client, session, "messages", {"message": "확인했습니다."})
    assert session["next_action"]["slot_key"] == first
    session = action(auth_client, session, "messages", {"message": "현재 가족은 저와 부모님으로 구성되어 있습니다."})
    essay = session["next_action"]["field_id"]
    assert session["next_action"]["kind"] == "field"
    action(auth_client, session, f"fields/{essay}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "해당 없음"})
    assert session["next_action"]["kind"] == "confirm"
    session = action(auth_client, session, f"fields/{essay}/confirm", {"confirmed": True})
    for expected_section in ("collection_use", "identity_processing", "provision_inquiry"):
        field = session["current_field"]
        assert field["section"]["kind"] == expected_section
        assert "처리 목적" in session["next_action"]["question"]
        assert field["id"] not in session["answers"]
        action(auth_client, session, "messages", {"message": "네"}, expected=422)
        session = action(auth_client, session, "messages", {"message": "동의하지 않음"})
        session = action(auth_client, session, f"fields/{field['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert len(session["drafts"]) == 1 and "해당 없음" in session["drafts"][0]["text"]
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    from app.core.security import _reset_rate_limit
    _reset_rate_limit()
    result = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert result.status_code == 200 and "동의하지 않음" in result.content.decode("utf-8-sig")
    assert "고유식별정보 처리" in result.content.decode("utf-8-sig")
    key = session["requirements"][essay][0]["key"]
    session = action(auth_client, session, f"fields/{essay}/{key}", {"message": "현재 저와 어머니 두 명으로 구성되어 있습니다."}, method="put")
    assert not session["drafts"] and not session["draft_reviewed"]
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409


def test_repeating_table_headers_require_review_even_with_matching_fingerprint():
    row = SimpleNamespace(content_hash="hash", status=PostingStatus.OPEN, deadline_at=None,
                          eligibility={"application_fields": [{"id": "f", "label": "신청서", "field_type": "text", "evidence": "신청서", "source": {"sha256": "hash"}}],
                                       "validation_version": VALIDATION_VERSION, "form_exclusions": [{"reason": "repeated_table_header_requires_review"}]})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]
