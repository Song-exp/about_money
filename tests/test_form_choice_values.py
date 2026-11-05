import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_choice_values import native_value_controls, selections
from app.services.form_control_audit import audit_controls
from app.services.posting_extract import extract_structured
from app.services.posting_documents import merge_posting_documents
from app.services.writing_requirements import requirements, next_action, writing_fingerprint
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_drafts import compose
from app.services.scholarship_readiness import form_fingerprint
from app.services.writing_chat import start_session
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from tests.test_writing_workflow import setup_writing, action


def source_blocks():
    blocks = []
    for row, label, texts in [(0, "주거 형태", ["□ 자가(      만원) □ 전세(      만원)", "□ 월세(      만원/월) □ 기타(          )"]),
                              (1, "해당사항", ["□ 장기요양 인정자(등급:    )", "□ 중증장애인", "□ 정신건강 관련 질환으로 치료 중"]),
                              (2, "해당사항", ["□ 장기요양 인정자(등급:    )", "□ 중증장애인", "□ 정신건강 관련 질환으로 치료 중"])]:
        blocks.append({"text": label, "location": f"t/r{row}/header", "table": "t", "row": row, "column": 0,
                       "column_span": 1, "cell_location": f"t/r{row}/c0", "method": "native"})
        if row:
            blocks.append({**blocks[-1], "text": "※ 중복 체크 가능", "location": f"t/r{row}/rule"})
        for i, text in enumerate(texts):
            blocks.append({"text": text, "location": f"t/r{row}/c1/p{i}", "table": "t", "row": row, "column": 1,
                           "cell_location": f"t/r{row}/c1", "method": "native"})
    return finalize_blocks(blocks)


def fields():
    return [c["field"] for c in native_value_controls(source_blocks(), {"sha256": "hash"})]


def test_native_source_controls_survive_model_omission_and_do_not_duplicate(monkeypatch):
    blocks = source_blocks()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({"application_fields": []}))
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert len(result["application_fields"]) == 3
    assert result["coverage"]["native_control_audit"]["unresolved"] == 0
    assert len({f["id"] for f in result["application_fields"]}) == 3
    assert all(f["required"] is None for f in result["application_fields"])
    assert fields()[0]["selection_mode"] == "marks"
    assert fields()[1]["selection_mode"] == "multiple"


@pytest.mark.parametrize("change", ["ocr", "unknown_caption", "missing_rule", "wrong_header", "wrong_span"])
def test_uncertain_controls_are_not_recovered(change):
    blocks = source_blocks()
    if change == "ocr":
        blocks[6]["method"] = "ocr"
    elif change == "unknown_caption":
        blocks[6]["text"] = "□ 다른 선택"
    elif change == "missing_rule":
        blocks[4]["text"] = "안내"
    elif change == "wrong_header":
        blocks[3]["text"] = "설명"
    else:
        blocks[3]["column_span"] = 2
    assert len(native_value_controls(blocks, {"sha256": "hash"})) == 2


@pytest.mark.parametrize("text", ['[]', '["중증장애인","중증장애인"]', '["없는 항목"]', '[1]', '중증장애인, 다른 선택'])
def test_multi_choice_only_accepts_original_distinct_captions(text):
    with pytest.raises(ValueError):
        selections(fields()[1], text)


def test_housing_values_are_selected_only_and_change_discards_prior_facts():
    housing = fields()[0]
    answers = {}
    _set_fact(answers, housing, "value", housing["options"][2], {"kind": "conversation"})
    slots = requirements(housing, answers[housing["id"]])
    assert [r["key"] for r in slots] == ["value", "choice_2_value"]
    for text in ("50만원", "-1", "네"):
        with pytest.raises(HTTPException):
            _check_choice(housing, text, "choice_2_value", answers[housing["id"]])
    _set_fact(answers, housing, "choice_2_value", "50", {"kind": "conversation"})
    for f in answers[housing["id"]]["facts"].values():
        f["confirmed"] = True
    draft = compose(housing, answers[housing["id"]], SimpleNamespace(title="장학금"))
    assert draft["text"] == "월세(50 만원/월)" and not draft["validation"]["errors"]
    _set_fact(answers, housing, "value", housing["options"][3], {"kind": "conversation_revision"})
    assert set(answers[housing["id"]]["facts"]) == {"value"}
    assert not answers[housing["id"]]["facts"]["value"]["confirmed"]
    with pytest.raises(HTTPException):
        _check_choice(housing, "50", "choice_2_value", answers[housing["id"]])


def test_two_people_and_multi_choices_collect_grade_only_if_selected(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    a, b = fields()[1:]
    answers = {}
    _set_fact(answers, a, "value", json.dumps([a["options"][0], a["options"][1]]), {"kind": "conversation"})
    _set_fact(answers, b, "value", b["options"][1], {"kind": "conversation"})
    assert len(requirements(a, answers[a["id"]])) == 2
    assert len(requirements(b, answers[b["id"]])) == 1
    assert a["id"] != b["id"]
    _set_fact(answers, a, "choice_0_value", "인지지원등급", {"kind": "conversation"})
    assert compose(a, answers[a["id"]], SimpleNamespace(title="장학금"))["text"] == "장기요양 인정자(등급: 인지지원등급)\n중증장애인"
    assert compose(b, answers[b["id"]], SimpleNamespace(title="장학금"))["text"] == "중증장애인"


def api_session(client, db, only_housing=False):
    row, initial = setup_writing(client, db)
    original = db.get(WritingSession, uuid.UUID(initial["id"]))
    new_fields = fields()
    blocks = source_blocks()
    if only_housing:
        new_fields = new_fields[:1]
        blocks = [b for b in blocks if b["row"] == 0]
    row.eligibility = {**row.eligibility, "application_fields": new_fields, "extraction_issues": [],
                       "form_groups": [], "form_exclusions": [],
                       "form_coverage": [{"native_control_audit": audit_controls(blocks, new_fields, {"sha256": "hash"})}]}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    return start_session(db, original.user_id, row, UserProfile())


def test_api_choice_values_confirmation_export_and_change_invalidation(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db)
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    housing, a, b = session["fields"]
    session = action(auth_client, session, "messages", {"message": housing["options"][2]})
    assert session["next_action"]["slot_key"] == "choice_2_value"
    action(auth_client, session, f"fields/{housing['id']}/confirm", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "messages", {"message": "50"})
    session = action(auth_client, session, f"fields/{housing['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "messages", {"message": json.dumps([a["options"][0], a["options"][1]])})
    assert session["next_action"]["slot_key"] == "choice_0_value"
    session = action(auth_client, session, "messages", {"message": "3등급"})
    session = action(auth_client, session, f"fields/{a['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "messages", {"message": b["options"][1]})
    session = action(auth_client, session, f"fields/{b['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    assert len(session["drafts"]) == 3
    action(auth_client, session, f"draft/{housing['id']}", {"message": "자가(999 만원)"}, method="put", expected=422)
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt")
    assert exported.status_code == 200 and "월세(50 만원/월)" in exported.content.decode("utf-8-sig")
    prior = session
    session = action(auth_client, session, f"fields/{housing['id']}/value", {"message": housing["options"][3]}, method="put")
    assert session["drafts"] == [] and not session["draft_reviewed"]
    assert set(session["answers"][housing["id"]]["facts"]) == {"value"}
    action(auth_client, prior, f"fields/{housing['id']}/value", {"message": housing["options"][2]}, method="put", expected=409)
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    action(auth_client, session, f"fields/{housing['id']}/choice_2_value", {"message": "80"}, method="put", expected=422)


def test_api_rebuilt_export_contains_new_value_and_no_retired_value(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    session = api_session(auth_client, db, only_housing=True)
    if session["next_action"]["kind"] == "qualification":
        session = action(auth_client, session, "messages", {"message": "네, 공고 조건에 해당합니다."})
    housing = session["fields"][0]
    session = action(auth_client, session, "messages", {"message": housing["options"][2]})
    session = action(auth_client, session, "messages", {"message": "50"})
    session = action(auth_client, session, f"fields/{housing['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    session = action(auth_client, session, f"fields/{housing['id']}/value", {"message": housing["options"][3]}, method="put")
    session = action(auth_client, session, "messages", {"message": "기숙사"})
    session = action(auth_client, session, f"fields/{housing['id']}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    exported = auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").content.decode("utf-8-sig")
    assert "기타(기숙사)" in exported and "월세(50" not in exported


def test_audit_rejects_forged_value_slots_even_with_real_option_sources():
    blocks = source_blocks()
    new_fields = fields()
    new_fields[0]["choice_spec"]["choices"][0]["values"][0]["unit"] = "원"
    assert audit_controls(blocks, new_fields, {"sha256": "hash"})["unresolved"] == 1


def test_duplicate_attachments_merge_same_values_but_conflicting_units_stay_review():
    originals = fields()
    copies = deepcopy(originals)
    for item in copies:
        item["id"] += "copy"
        item["source"]["url"] = "https://other.test/form"
        for choice in item["choice_spec"]["choices"]:
            choice["source"]["url"] = "https://other.test/form"
            for value in choice["values"]:
                value["source"]["url"] = "https://other.test/form"
    def merge():
        raw = SimpleNamespace(source_documents=[{"structured": {"application_fields": originals}},
                                               {"structured": {"application_fields": copies}}])
        return merge_posting_documents(raw)
    result = merge()
    assert len(result["application_fields"]) == 3 and not result["issues"]
    copies[0]["choice_spec"]["choices"][0]["values"][0]["unit"] = "원"
    result = merge()
    assert "conflicting_duplicate_form_field" in result["issues"]
