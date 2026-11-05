"""Independent source contracts and real-form fields through the writing API."""
import io
import json
import uuid
from types import SimpleNamespace
from copy import deepcopy
from pathlib import Path

import pytest
from docx import Document

from app.core.config import settings
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services import llm
from app.models.scholarship import PostingStatus
from app.services.scholarship_readiness import form_fingerprint, readiness, VALIDATION_VERSION
from app.services.writing_chat import start_session
from reports.atomic_input_reference import record_of
from reports.measure_submission_consent import measure_collection
from reports.submission_consent_reference import audit_submission_consent, build_submission_consent_gold
from tests.test_writing_workflow import setup_writing, action

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def fixture():
    gold = json.loads((REPORTS / "scholarship-improvement-20261010-submission-consent-verified/gold.json").read_text(encoding="utf-8"))
    prediction = json.loads((REPORTS / "scholarship-improvement-20261010-grid-audit-verified/r1-prediction.json").read_text(encoding="utf-8"))
    return gold, prediction["application_fields"], prediction["coverage"]


def at(fields, record, person=None):
    return next(f for f in fields if record_of(f.get("source")) == record and
                (person is None or record_of((f.get("subject") or {}).get("source")) == person))


def test_source_reference_and_real_collection_do_not_mutate_inputs():
    gold, fields, coverage = fixture()
    before = deepcopy((gold, fields, coverage))
    audit = audit_submission_consent(gold, fields, coverage)
    assert audit["unresolved_contracts"] == 0 and len(audit["checks"]) == 27
    assert audit["printed_reference_still_requires_review"] and not audit["whole_form_complete"]
    result = measure_collection(gold, fields)
    assert len(result["checklist_cases"]) == 16 and result["original_only_and_signature_inputs_rejected"] == 7
    assert (gold, fields, coverage) == before


def test_reference_can_be_rebuilt_from_original_inventory_without_predictions():
    inventory = json.loads((REPORTS / "scholarship-improvement-20261010-source-records-verified/source-record-gold.json").read_text(encoding="utf-8"))
    gold, _, _ = fixture()
    assert build_submission_consent_gold(inventory) == gold
    inventory["source_sha256"] = "other-document"
    with pytest.raises(ValueError):
        build_submission_consent_gold(inventory)


def test_real_reference_still_blocks_approval_after_bounded_contracts_pass():
    gold, fields, coverage = fixture()
    assert not audit_submission_consent(gold, fields, coverage)["unresolved_contracts"]
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="original-source", eligibility={
        "application_fields": fields, "extraction_issues": [], "form_coverage": [coverage],
        "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]


@pytest.mark.parametrize("problem", ["missing_item", "duplicate_item", "file_verified", "automatic_mark",
    "example_completed", "wrong_mark_cell", "missing_issue_date", "hidden_rule", "economic_resolution",
    "real_destination_for_service_value", "missing_ready_confirmation", "wrong_document", "wrong_header",
    "missing_refusal", "crossed_consent_context", "missing_consent_context", "wrong_section", "wrong_option_source",
    "wrong_person", "wrong_person_cell", "invented_person_refusal", "signature_as_text", "condition_leak",
    "condition_removed", "original_date_as_chat", "automatic_date_placement", "mixed_dates", "identity_cell_changed",
    "reference_as_input", "reference_resolved", "reference_source_changed", "reference_candidate_missing",
    "wrong_visible_person", "wrong_visible_consent", "wrong_visible_document"])
def test_source_contract_corruption_is_detected(problem):
    gold, fields, coverage = fixture()
    checklist, consent, person, extra, date = (at(fields, *args) for args in ((168,), (1262,), (1412, 1444), (1402, 1466), (1493,)))
    if problem == "missing_item":
        fields.remove(checklist)
    elif problem == "duplicate_item":
        fields.append(deepcopy(checklist))
    elif problem == "file_verified":
        checklist["preparation_spec"]["file_verified"] = True
    elif problem == "automatic_mark":
        checklist["preparation_spec"]["automatic_marking"] = True
    elif problem == "example_completed":
        at(fields, 136)["preparation_spec"]["example_only"] = False
    elif problem == "wrong_mark_cell":
        checklist["input_cell_source"]["record"] = 199
    elif problem == "missing_issue_date":
        checklist["instruction_sources"] = [p for p in checklist["instruction_sources"] if record_of(p) != 186]
    elif problem == "hidden_rule":
        checklist["instructions"] = "원문 조건 생략"
    elif problem == "economic_resolution":
        at(fields, 281)["preparation_spec"]["economic_rule_review_required"] = False
    elif problem == "real_destination_for_service_value":
        checklist["input_spec"]["slots"][0]["destination"] = checklist["layout"]["cell_location"]
    elif problem == "missing_ready_confirmation":
        checklist["input_spec"]["ready_slots"] = []
    elif problem == "wrong_document":
        checklist["preparation_spec"]["document_sources"] = []
    elif problem == "wrong_header":
        checklist["preparation_spec"]["header_sources"][0]["sha256"] = "other-document"
    elif problem == "missing_refusal":
        consent["options"] = ["동의함"]
    elif problem == "crossed_consent_context":
        consent["consent_context"] = deepcopy(at(fields, 1207)["consent_context"])
    elif problem == "missing_consent_context":
        consent["consent_context"]["sources"].pop()
    elif problem == "wrong_section":
        consent["section"]["kind"] = "collection_use"
    elif problem == "wrong_option_source":
        consent["option_sources"][0]["location"] = "Section0/record:1216"
    elif problem == "wrong_person":
        person["subject"] = deepcopy(extra["subject"])
    elif problem == "wrong_person_cell":
        person["input_cell_source"]["payload_sha256"] = "wrong"
    elif problem == "invented_person_refusal":
        person["options"].append("동의하지 않음")
    elif problem == "signature_as_text":
        at(fields, 1417, 1444)["field_type"] = "text"
    elif problem == "condition_leak":
        person["applicability"] = deepcopy(extra["applicability"])
    elif problem == "condition_removed":
        extra.pop("applicability")
    elif problem == "original_date_as_chat":
        date.pop("manual_spec")
    elif problem == "automatic_date_placement":
        date["original_destination"]["visual_placement_verified"] = True
    elif problem == "mixed_dates":
        date["original_destination"]["source"] = deepcopy(at(fields, 1035)["source"])
    elif problem == "identity_cell_changed":
        at(fields, 464)["input_cell_source"]["record"] = 1425
    elif problem == "reference_as_input":
        copied = deepcopy(person)
        copied["layout"]["row"] = 1
        fields.append(copied)
    elif problem == "reference_resolved":
        coverage["native_reference_audit"]["controls"][0].update(automatic_inheritance=True, target_field_ids=[consent["id"]])
    elif problem == "reference_source_changed":
        coverage["native_reference_audit"]["controls"][0]["source"]["sha256"] = "other-document"
    elif problem == "wrong_visible_person":
        person["subject"]["label"] = "신청자"
    elif problem == "wrong_visible_consent":
        consent["label"] = "일괄 동의"
    elif problem == "wrong_visible_document":
        checklist["label"] = "다른 증빙서류"
    else:
        coverage["native_reference_audit"]["controls"][0]["candidates"].pop()
    assert audit_submission_consent(gold, fields, coverage)["unresolved_contracts"] > 0


def api_session(client, db, selected):
    # Isolate real source fields in the test DB. This does not approve the real notice.
    row, initial = setup_writing(client, db)
    owner = db.get(WritingSession, uuid.UUID(initial["id"])).user_id
    row.eligibility = {**row.eligibility, "application_fields": selected, "extraction_issues": [],
                       "form_groups": [], "form_exclusions": [], "form_coverage": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, owner, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건을 확인했고 해당합니다."})
    return session


def finish_collection(client, session, values):
    for _ in range(80):
        step = session["next_action"]
        if step is None:
            return session
        if step["kind"] == "confirm":
            session = action(client, session, f"fields/{step['field_id']}/confirm", {"confirmed": True})
        elif step["kind"] == "applicability":
            session = action(client, session, f"conditions/{step['condition_id']}", {"applies": True})
        else:
            assert step["kind"] == "field"
            session = action(client, session, "messages", {"message": values[(step["field_id"], step["slot_key"])]})
    raise AssertionError("collection did not finish")


def exported_texts(client, session):
    url = f"/writing-sessions/{session['id']}/export"
    txt, docx = (client.get(url + f"?format={fmt}") for fmt in ("txt", "docx"))
    assert txt.status_code == docx.status_code == 200
    return [txt.content.decode("utf-8-sig"), "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)]


@pytest.fixture(autouse=True)
def no_model_and_allow_accelerated_conversation(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    monkeypatch.setattr(settings, "chat_rate_limit_per_min", 1000)


def test_real_preparation_and_original_fields_export_without_claiming_completion(auth_client, db):
    _, fields, _ = fixture()
    # Applicant footer signature retains its unresolved consent-reference gate;
    # it belongs in the blocked real form, rather than this approved test subset.
    selected = [at(fields, r) for r in (168, 455, 464, 1035, 1042, 1493)] + [at(fields, 1417, 1444)]
    session = api_session(auth_client, db, selected)
    checklist, name = selected[:2]
    for f in selected[2:]:
        action(auth_client, session, f"fields/{f['id']}/value", {"message": "임의 입력"}, method="put", expected=422)
    values = {(checklist["id"], "preparation_status"): "준비됨", (checklist["id"], "rules_ack"): "원문 조건 확인",
              (name["id"], "name"): "본인확인이름"}
    session = finish_collection(auth_client, session, values)
    assert not any(f["id"] in session["answers"] for f in selected[2:])
    session = action(auth_client, session, "draft")
    action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": False}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    for text in exported_texts(auth_client, session):
        assert "사용자 보고 준비 상태: 준비됨" in text and "실제 파일 검증·첨부·제출 완료를 뜻하지 않습니다" in text
        assert "2026. 10. 1. 이후" in text and "날짜를 자동으로 채우지 않습니다" in text
        assert "직접 서명" in text and "주민등록번호는 채팅에 입력하지 말고" in text
    old = session
    session = action(auth_client, session, f"fields/{checklist['id']}/preparation_status", {"message": "준비 중"}, method="put")
    assert not session["drafts"] and "rules_ack" not in session["answers"][checklist["id"]]["facts"]
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    action(auth_client, old, f"fields/{checklist['id']}/rules_ack", {"message": "원문 조건 확인"}, method="put", expected=409)
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded["answers"] == session["answers"]
    session = finish_collection(auth_client, session, values)
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    for text in exported_texts(auth_client, session):
        assert "사용자 보고 준비 상태: 준비 중; 원문 조건 확인: 미확인" in text


def test_real_consent_sections_refusal_people_and_condition_remain_separate(auth_client, db):
    gold, fields, _ = fixture()
    sections = [at(fields, r) for r in (1207, 1262, 1370)]
    people = [f for f in fields if f.get("layout", {}).get("table") == "BodyText/Section0/table:1393" and f["layout"]["row"] in (2, 3)]
    name = at(fields, 455)
    session = api_session(auth_client, db, [*sections, *people, name])
    values = {(f["id"], "value"): value for f, value in zip(sections, ("동의함", "동의하지 않음", "동의함"))}
    values[(name["id"], "name")] = "신청자확인이름"
    for f in people:
        if f["field_type"] != "signature":
            values[(f["id"], "value")] = "표시하지 않음" if f["field_type"] == "checkbox" else f"대상자{f['layout']['row']}고유응답"
    session = finish_collection(auth_client, session, values)
    assert session["answers"][sections[1]["id"]]["text"] == "동의하지 않음"
    consent = at(people, 1412, 1444)
    assert session["answers"][consent["id"]]["facts"]["value"]["selection_state"] == "unchecked"
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    for text in exported_texts(auth_client, session):
        assert "사용자가 선택한 내용: 동의하지 않음" in text and "사용자가 선택한 내용: 표시하지 않음" in text
        assert all(item["heading"]["text"] in text for item in gold["consents"])
        assert "대상자2고유응답" in text and "대상자3고유응답" in text
    retained = deepcopy(session["answers"])
    session = action(auth_client, session, f"fields/{sections[0]['id']}/value", {"message": "동의하지 않음"}, method="put")
    assert not session["drafts"]
    assert all(session["answers"][fid] == answer for fid, answer in retained.items() if fid != sections[0]["id"])
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 409
    session = finish_collection(auth_client, session, values)
    extra = at(people, 1402, 1466)
    cid = extra["applicability"]["id"]
    session = action(auth_client, session, f"conditions/{cid}", {"applies": False})
    assert not any(f["id"] in session["answers"] for f in people if f["layout"]["row"] == 3)
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    for text in exported_texts(auth_client, session):
        assert "대상자2고유응답" in text and "대상자3고유응답" not in text
        assert "추가 돌봄대상자(해당 시): 원본에서 직접 서명" not in text
