import io
import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from docx import Document

from app.models.scholarship import PostingStatus
from app.models.writing_session import WritingSession
from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_content import enrich_content
from app.services.form_structure import attach_groups, descriptive_cells, group_scopes
from app.services.posting_extract import extract_structured
from app.services.posting_documents import merge_posting_documents
from app.services.scholarship_readiness import VALIDATION_VERSION, form_fingerprint, readiness
from app.services.writing_drafts import compose, validate_groups, validate_length
from app.services.writing_requirements import requirements
from tests.test_writing_workflow import setup_writing, collect, action


def native_blocks(rule="※ 총 3장 이내 / 맑은고딕 / 글씨크기 10pt / 줄 간격 160 / 아래 질문에 답변하세요."):
    blocks = [{"text": "[ 자기소개서 ]", "location": "heading", "method": "native", "kind": "paragraph"},
              {"text": rule, "location": "rule", "method": "native", "kind": "paragraph"}]
    for index, label in enumerate(("1. 지원동기", "2. 진로계획")):
        blocks.append({"text": label, "location": f"t/p{index}", "method": "native", "kind": "cell",
                       "table": "t", "cell_location": f"t/c{index}", "row": index * 2, "column": 0})
    blocks.append({"text": "개인정보 동의서", "location": "privacy", "method": "native", "kind": "paragraph"})
    blocks.append({"text": "1. 수집·이용 목적", "location": "privacy/c", "method": "native", "kind": "cell", "table": "privacy", "row": 0, "column": 0})
    return finalize_blocks(blocks)


def extract_group(monkeypatch, omit=False, rule=None):
    blocks = native_blocks() if rule is None else native_blocks(rule)
    candidates = [{"label": b["text"], "field_type": "essay", "label_block_id": f"b{i:04d}",
                   "instruction_block_ids": ["b0001"], "max_length": 3, "length_unit": "pages"}
                  for i, b in enumerate(blocks) if b.get("table") == "t"]
    if omit:
        candidates.pop()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps({"application_fields": candidates}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "original"}, blocks)
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    return result


def test_shared_rule_binds_only_numbered_native_essay_section(monkeypatch):
    result = extract_group(monkeypatch)
    group = result["form_groups"][0]
    assert group["status"] == "source_bound" and len(group["field_ids"]) == 2
    assert group["constraints"] == {"max_length": 3, "length_unit": "pages"}
    assert [r["kind"] for r in group["format_rules"]] == ["font_family", "font_size", "line_spacing"]
    assert not group["issues"] and not result["issues"]
    assert all(f["max_length"] is None and f["length_unit"] is None for f in result["application_fields"])
    assert all("privacy" not in p["location"] for p in group["expected_members"])


@pytest.mark.parametrize("change", ["missing", "numbering", "ocr"])
def test_ambiguous_scope_or_members_cannot_be_approved_after_issue_list_cleared(monkeypatch, change):
    if change == "missing":
        result = extract_group(monkeypatch, omit=True)
        fields, groups = result["application_fields"], result["form_groups"]
        fields = fields[:-1]  # Lost persisted member must still invalidate approval.
    else:
        blocks = native_blocks()
        blocks[3]["text"] = "4. 진로계획" if change == "numbering" else blocks[3]["text"]
        if change == "ocr":
            blocks[3]["method"] = "ocr"
        finalize_blocks(blocks)
        fields = [{"id": str(i), "label": b["text"], "field_type": "essay", "evidence": b["text"],
                   "source": {"sha256": "original", "label_start": b["start"]}} for i, b in enumerate(blocks[2:4])]
        groups = attach_groups(fields, group_scopes(blocks), {"sha256": "original"})
    row = SimpleNamespace(content_hash="hash", status=PostingStatus.OPEN, deadline_at=None,
                          eligibility={"application_fields": fields, "form_groups": groups, "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


def test_native_essay_scope_recovers_model_omission_and_rejects_rewritten_guidance(monkeypatch):
    result = extract_group(monkeypatch, omit=True)
    assert len(result["application_fields"]) == 2 and result["form_groups"][0]["status"] == "source_bound"
    blocks = native_blocks()
    candidates = [{"label": "지원동기", "field_type": "text", "label_block_id": "b0002",
                   "instructions": "원문에 없는 요약 지침", "instruction_block_ids": ["b0001"]}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps({"application_fields": candidates}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "source"}, blocks)
    assert len(result["application_fields"]) == 2 and not result["issues"]
    assert all(f["field_type"] == "essay" and "없는 요약" not in f["instructions"] for f in result["application_fields"])


@pytest.mark.parametrize("text", ["2. 성명", "2. 활동 선택 □"])
def test_native_essay_recovery_does_not_assume_text_or_choice_is_an_essay(monkeypatch, text):
    blocks = native_blocks()
    blocks[3]["text"] = text
    finalize_blocks(blocks)
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "source"}, blocks)
    assert not result["application_fields"] and result["form_groups"][0]["status"] == "needs_review"


def test_group_only_change_invalidates_form_fingerprint(monkeypatch):
    result = extract_group(monkeypatch)
    row = SimpleNamespace(content_hash="hash", eligibility={"application_fields": result["application_fields"], "form_groups": result["form_groups"]})
    before = form_fingerprint(row)
    row.eligibility["form_groups"][0]["constraints"]["max_length"] = 2
    assert form_fingerprint(row) != before


def test_merged_duplicate_fields_remap_every_group_member(monkeypatch):
    result = extract_group(monkeypatch)
    duplicate = deepcopy(result)
    for field in duplicate["application_fields"]:
        field["id"] += "copy"
    duplicate["form_groups"][0]["id"] += "copy"
    duplicate["form_groups"][0]["field_ids"] = [f["id"] for f in duplicate["application_fields"]]
    raw = SimpleNamespace(source_documents=[{"structured": result}, {"structured": duplicate}])
    merged = merge_posting_documents(raw)
    assert len(merged["application_fields"]) == 2 and len(merged["form_groups"]) == 2
    assert all(g["field_ids"] == [f["id"] for f in merged["application_fields"]] for g in merged["form_groups"])


def test_description_cells_keep_real_name_choices_and_checklist_check_column():
    blocks = []
    for table, rows in (("list", [["서류명", "첨부여부", "비고", "발급일 기준"], ["재학증명서", "□ 제출", "성명 기재", "2026년 발급"]]),
                        ("privacy", [["개인정보 수집·이용(필수)"], ["수집·이용 목적", "신청 접수 및 장학금 지급을 위한 목적입니다."], ["동의 여부", "동의함 □ 동의하지 않음 □"]]),
                        ("person", [["성명", ""]])):
        for row, values in enumerate(rows):
            for col, text in enumerate(values):
                if text:
                    blocks.append({"text": text, "table": table, "row": row, "column": col, "method": "native", "location": f"{table}/{row}/{col}"})
    excluded = descriptive_cells(finalize_blocks(blocks), {"sha256": "original"})
    assert "privacy/1/0" in excluded and "privacy/1/1" in excluded and "list/1/0" in excluded
    assert "person/0/0" not in excluded and "privacy/2/1" not in excluded and "list/1/1" not in excluded
    blocks[-1]["text"] = "수집·이용 목적"
    assert "person/0/0" not in descriptive_cells(blocks)  # Same title with empty value is an input.


def test_excluded_model_description_is_auditable_without_missing_label_failure(monkeypatch):
    blocks = finalize_blocks([{"text": "▣ 개인정보 : 성명, 생년월일, 주소, 연락처", "location": "inventory", "table": "t", "row": 0, "column": 0, "method": "native"}])
    traces = []
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps({"application_fields": [{"label": "성명", "label_block_id": "b0000", "field_type": "text"}]}))
    result = extract_structured(blocks[0]["text"], {"sha256": "hash"}, blocks, trace_sink=traces.append)
    assert not result["application_fields"] and not result["issues"]
    assert traces[0]["field_decisions"][0]["result"] == "excluded"
    assert result["form_exclusions"][0]["source"]["evidence"] == blocks[0]["text"]


def test_consent_question_without_original_control_stays_in_review(monkeypatch):
    prompt = "동의를 거부할 수 있으나 선발이 제한됩니다. 동의하십니까?"
    blocks = finalize_blocks([{"text": "개인정보 수집·이용 동의서", "location": "t/header", "method": "native", "table": "t", "row": 0, "column": 0},
                              {"text": prompt, "location": "t/prompt", "method": "native", "table": "t", "row": 1, "column": 0}])
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: json.dumps({"application_fields": [{"label": prompt, "field_type": "essay", "label_block_id": "b0001"}]}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert not result["application_fields"] and "consent_control_requires_original_review" in result["issues"]
    blocks[1]["text"] += " 동의함 □ 동의하지 않음 □"
    assert "t/prompt" not in descriptive_cells(finalize_blocks(blocks))


def content_field():
    proof = "작성 분 량 은 학 업 계획 및 자 기 계발 500 자 이상, 목 표 달 성 을 위한 추 진 계획 3 가 지 이상 제시"
    field = {"id": "plan", "label": "목표 달성을 위한 추진계획", "field_type": "essay", "evidence": "목표 달성을 위한 추진계획",
             "source": {"sha256": "hash"}, "instruction_sources": [{"evidence": proof, "sha256": "hash", "location": "image/25", "text_start": 0, "text_end": len(proof)}]}
    enrich_content(field)
    return field


def test_original_item_requirement_does_not_leak_to_neighbour_and_checks_edits(monkeypatch):
    field = content_field()
    neighbour = {**field, "label": "학업계획 및 자기계발"}
    neighbour.pop("content_requirements")
    enrich_content(neighbour)
    assert not neighbour.get("content_requirements")
    assert len(requirements(field)) == 3
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    facts = {f"original_item_{i}": {"text": text, "confirmed": True} for i, text in enumerate(("전공 수업 복습", "연구실 프로젝트 참여", "자격 시험 준비"), 1)}
    draft = compose(field, {"facts": facts}, SimpleNamespace(title="장학금"))
    assert draft["text"] == "1. 전공 수업 복습\n\n2. 연구실 프로젝트 참여\n\n3. 자격 시험 준비"
    assert not draft["validation"]["errors"]
    assert "missing_original_content_items" in validate_length(field, "1. 계획 하나\n2. 계획 둘")["errors"]
    assert "duplicate_original_content_items" in validate_length(field, "1. 계획 하나\n2. 계획하나\n3. 계획 둘")["errors"]


def group_for(fields, unit="pages", maximum=3):
    return {"id": "g", "label": "자기소개서", "field_ids": [f["id"] for f in fields], "expected_members": [{"evidence": f["label"]} for f in fields],
            "source": {"sha256": "hash"}, "status": "source_bound", "issues": [], "evidence": "총 3장 이내 / 글씨크기 10pt",
            "constraints": {"length_unit": unit, "max_length": maximum}, "format_rules": [{"kind": "font_size", "evidence": "10pt"}]}


def test_group_count_checks_combined_text_and_missing_member():
    fields = [{"id": "a", "field_type": "essay"}, {"id": "b", "field_type": "essay"}]
    for f in fields:
        f["label"] = f["id"]
    group = group_for(fields, "characters", 10)
    drafts = [{"field_id": "a", "text": "12345"}, {"field_id": "b", "text": "67890"}]
    assert "above_max_length" in validate_groups([group], fields, drafts, {})[0]["errors"]
    assert "shared_group_drafts_missing" in validate_groups([group], fields, drafts[:1], {})[0]["errors"]


def test_nonapplicable_members_do_not_count_or_trigger_manual_checks():
    field = {"id": "a", "label": "a", "field_type": "essay", "applicability": {"id": "c", "mode": "user_confirmation"}}
    group = group_for([field])
    workflow = {"applicability_answers": {"c": {"applies": False, "confirmed": True}}}
    result = validate_groups([group], [field], [{"field_id": "a", "text": "old content"}], workflow)[0]
    assert not result["field_ids"] and not result["errors"] and not result["warnings"]


def test_history_copy_prohibition_is_local_and_blocks_history_endpoints(auth_client, db):
    row, session = setup_writing(auth_client, db)
    field = row.eligibility["application_fields"][0]
    field = {**field, "history_reuse_policy": {"mode": "direct_copy_prohibited", "sources": [{"evidence": "이전 신청 내용을 그대로 붙여 넣으면 탈락"}]}}
    row.eligibility = {**row.eligibility, "application_fields": [field]}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    from app.services.writing_chat import start_session
    from app.schemas.schemas import UserProfile
    previous = db.get(WritingSession, uuid.UUID(session["id"]))
    session = start_session(db, previous.user_id, row, UserProfile.model_validate(previous.profile))
    base = f"/writing-sessions/{session['id']}"
    assert auth_client.get(base + "/history", params={"field_id": field["id"]}).status_code == 422
    action(auth_client, session, "history", {"field_id": field["id"], "slot_key": "reason", "document_id": 1, "quote": "과거내용", "confirmed": True}, expected=422)


def test_shared_pages_and_format_require_confirmation_export_and_recheck_after_edit(auth_client, db):
    row, session = setup_writing(auth_client, db)
    group = group_for(row.eligibility["application_fields"])
    row.eligibility = {**row.eligibility, "form_groups": [group]}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    from app.services.writing_chat import start_session
    from app.schemas.schemas import UserProfile
    previous = db.get(WritingSession, uuid.UUID(session["id"]))
    session = start_session(db, previous.user_id, row, UserProfile.model_validate(previous.profile))
    session = collect(auth_client, session)
    session = action(auth_client, session, "draft")
    assert session["drafts"][0]["validation"]["warnings"] == []
    assert set(session["group_validations"][0]["warnings"]) == {"original_layout_page_check_required", "original_group_format_check_required"}
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert group["evidence"] in exported.content.decode("utf-8-sig")
    docx = auth_client.get(f"/writing-sessions/{session['id']}/export?format=docx")
    assert group["evidence"] in "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    session = action(auth_client, session, f"draft/{session['fields'][0]['id']}", {"message": "수정한 실제 본인의 신청 이유입니다."}, method="put")
    assert not session["draft_reviewed"]
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    stored = db.get(WritingSession, uuid.UUID(session["id"]))
    from app.services.writing_drafts import draft_digest
    stored.workflow = {**stored.workflow, "reviewed_digest": draft_digest(stored.workflow["drafts"]), "manual_checks_confirmed": False}
    db.commit()
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409


def test_content_duplicate_blocks_confirmation_and_generation(auth_client, db):
    row, session = setup_writing(auth_client, db)
    field = {**row.eligibility["application_fields"][0], **content_field()}
    row.eligibility = {**row.eligibility, "application_fields": [field]}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    from app.services.writing_chat import start_session
    from app.schemas.schemas import UserProfile
    previous = db.get(WritingSession, uuid.UUID(session["id"]))
    session = start_session(db, previous.user_id, row, UserProfile.model_validate(previous.profile))
    session = action(auth_client, session, "messages", {"message": "네, 모두 해당합니다."})
    for text in ("전공 수업을 매일 복습하겠습니다.", "전공 수업을 매일 복습하겠습니다.", "연구 프로젝트에 매주 참여하겠습니다."):
        session = action(auth_client, session, "messages", {"message": text})
    assert session["next_action"]["slot_key"] == "original_item_2"
    action(auth_client, session, "fields/plan/confirm", {"confirmed": True}, expected=409)
    action(auth_client, session, "draft", expected=409)
    session = action(auth_client, session, "messages", {"message": "자격 시험을 준비하여 이번 학기 응시하겠습니다."})
    session = action(auth_client, session, "fields/plan/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "draft/plan", {"message": "1. 전공 복습\n2. 연구 참여"}, method="put")
    assert session["state"] == "needs_revision"
    action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True}, expected=409)
