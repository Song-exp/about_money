import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services import llm
from app.services.scholarship_readiness import form_fingerprint
from app.services.writing_chat import start_session
from reports.atomic_input_reference import record_of
from reports.choice_repeat_reference import audit_choice_repeat
from reports.measure_choice_repeat import measure_collection, VALUES
from tests.test_writing_workflow import setup_writing, action

REPORTS = Path(__file__).resolve().parents[1] / "reports"


def fixture():
    gold = json.loads((REPORTS / "scholarship-improvement-20261010-choice-repeat-verified/gold.json").read_text(encoding="utf-8"))
    fields = json.loads((REPORTS / "scholarship-improvement-20261010-grid-audit-verified/r1-prediction.json").read_text(encoding="utf-8"))["application_fields"]
    return gold, fields


def field_at(fields, record):
    return next(f for f in fields if record_of(f.get("source")) == record)


def test_original_contract_and_all_selection_and_repeat_counts(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    gold, fields = fixture()
    before = deepcopy((gold, fields))
    audit = audit_choice_repeat(gold, fields)
    assert audit["unresolved_groups"] == 0 and not audit["complete_atomic_gold"]
    result = measure_collection(gold, fields)
    assert len(result["choice_cases"]) == 15 and len(result["repeat_cases"]) == 5
    assert result["recipient_isolation_checks"] == 1
    assert (gold, fields) == before


@pytest.mark.parametrize("problem", ["missing_group", "duplicate_group", "invented_option", "wrong_choice_scope",
    "wrong_mode", "missing_multi_rule", "wrong_value_unit", "missing_inline_value", "value_on_unselected_history",
    "wrong_history_destination", "wrong_choice_source", "wrong_person_row", "wrong_person_table", "missing_column",
    "duplicate_column", "wrong_currency_type", "fixed_currency_unit", "missing_date_column", "wrong_period_source",
    "missing_repeat_rule", "missing_template", "swapped_template", "wrong_cell_record", "wrong_cell_row", "wrong_cell_hash",
    "wrong_cell_table", "missing_layout_review", "automatic_count", "missing_scope_confirmation", "manual_repeat_bypass"])
def test_choice_and_repeat_schema_corruption_is_detected(problem):
    gold, fields = fixture()
    housing, history, person, repeat = (field_at(fields, r) for r in (501, 415, 727, 830))
    choices = housing["choice_spec"]["choices"]
    repetition = repeat["input_spec"]["repeat"]
    if problem == "missing_group":
        fields.remove(housing)
    elif problem == "duplicate_group":
        fields.append(deepcopy(housing))
    elif problem == "invented_option":
        housing["options"].append("없는 선택")
    elif problem == "wrong_choice_scope":
        choices[1]["values"] = deepcopy(choices[0]["values"])
    elif problem == "wrong_mode":
        housing["selection_mode"] = "single"
    elif problem == "missing_multi_rule":
        person["choice_spec"]["multiple_sources"] = []
    elif problem == "wrong_value_unit":
        choices[2]["values"][0]["unit"] = "원/월"
    elif problem == "missing_inline_value":
        choices[3]["values"] = []
    elif problem == "value_on_unselected_history":
        history["choice_spec"]["choices"][1]["values"] = deepcopy(history["choice_spec"]["choices"][0]["values"])
    elif problem == "wrong_history_destination":
        history["choice_spec"]["choices"][0]["values"][0]["destination"] = "Section0/record:444"
    elif problem == "wrong_choice_source":
        choices[0]["source"]["sha256"] = "another-document"
    elif problem == "wrong_person_row":
        person["layout"]["row"] = 12
    elif problem == "wrong_person_table":
        person["layout"]["table"] = "different-table"
    elif problem == "missing_column":
        repetition["columns"].pop()
    elif problem == "duplicate_column":
        repetition["columns"].append(deepcopy(repetition["columns"][0]))
    elif problem == "wrong_currency_type":
        repetition["columns"][3]["kind"] = "text"
    elif problem == "fixed_currency_unit":
        repetition["columns"][4]["unit"] = "원"
    elif problem == "missing_date_column":
        repetition["columns"].pop(6)
    elif problem == "wrong_period_source":
        repetition["columns"][5]["source"]["location"] = "Section0/record:907"
    elif problem == "missing_repeat_rule":
        repeat["instruction_sources"] = []
    elif problem == "missing_template":
        repetition["templates"].pop()
    elif problem == "swapped_template":
        repetition["templates"][0], repetition["templates"][1] = repetition["templates"][1], repetition["templates"][0]
    elif problem in ("wrong_cell_record", "wrong_cell_row", "wrong_cell_hash", "wrong_cell_table"):
        key, value = {"wrong_cell_record": ("record", 9999), "wrong_cell_row": ("row", 18),
                      "wrong_cell_hash": ("payload_sha256", "wrong"), "wrong_cell_table": ("table", "wrong")}[problem]
        repetition["templates"][0]["cells"][0][key] = value
    elif problem == "missing_layout_review":
        repetition["original_layout_review_required"] = False
    elif problem == "automatic_count":
        repetition["count_key"] = "automatic_rows"
    elif problem == "missing_scope_confirmation":
        repeat["input_spec"]["slots"].pop(0)
    else:
        repeat["manual_spec"] = {"mode": "original_only"}
    assert audit_choice_repeat(gold, fields)["unresolved_groups"] > 0


def api_session(client, db, record):
    row, initial = setup_writing(client, db)
    owner = db.get(WritingSession, uuid.UUID(initial["id"])).user_id
    _, fields = fixture()
    row.eligibility = {**row.eligibility, "application_fields": [field_at(fields, record)], "extraction_issues": [],
                       "form_groups": [], "form_exclusions": [], "form_coverage": []}
    row.eligibility = {**row.eligibility, "form_review": {"fingerprint": form_fingerprint(row)}}
    db.commit()
    session = start_session(db, owner, row, UserProfile())
    if session["next_action"]["kind"] == "qualification":
        session = action(client, session, "messages", {"message": "네, 공고 조건을 확인했고 해당합니다."})
    return session


def reviewed(client, session, fid, manual=False):
    session = action(client, session, f"fields/{fid}/confirm", {"confirmed": True})
    session = action(client, session, "draft")
    return action(client, session, "review", {"confirmed": True, "manual_checks_confirmed": manual})


def test_real_history_field_change_discards_prior_details_and_blocks_export(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    monkeypatch.setattr(settings, "chat_rate_limit_per_min", 1000)
    session = api_session(auth_client, db, 415)
    field = session["fields"][0]
    for value in ("있음", "1", "2024", "이전수혜고유응답"):
        session = action(auth_client, session, "messages", {"message": value})
    session = reviewed(auth_client, session, field["id"])
    url = f"/writing-sessions/{session['id']}/export?format=txt"
    assert "이전수혜고유응답" in auth_client.get(url).content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{field['id']}/value", {"message": "없음"}, method="put")
    assert set(session["answers"][field["id"]]["facts"]) == {"value"} and not session["drafts"]
    assert auth_client.get(url).status_code == 409
    action(auth_client, session, f"fields/{field['id']}/history_name", {"message": "이전수혜고유응답"}, method="put", expected=422)
    session = reviewed(auth_client, session, field["id"])
    assert "이전수혜고유응답" not in auth_client.get(url).content.decode("utf-8-sig")
    session = action(auth_client, session, f"fields/{field['id']}/value", {"message": "있음"}, method="put")
    assert session["next_action"]["slot_key"] == "history_count"
    assert set(session["answers"][field["id"]]["facts"]) == {"value"}


def test_real_repeat_count_change_discards_rows_and_reload_requires_new_answers(auth_client, db, monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    monkeypatch.setattr(settings, "chat_rate_limit_per_min", 1000)
    session = api_session(auth_client, db, 830)
    fid = session["fields"][0]["id"]
    for value in ("제외 조건 확인", "2"):
        session = action(auth_client, session, "messages", {"message": value})
    for index in range(2):
        for key, value in VALUES.items():
            session = action(auth_client, session, "messages", {"message": f"이전행{index}고유응답" if key == "name" else value})
    session = reviewed(auth_client, session, fid, manual=True)
    url = f"/writing-sessions/{session['id']}/export?format=txt"
    assert "이전행1고유응답" in auth_client.get(url).content.decode("utf-8-sig")
    old = session
    session = action(auth_client, session, f"fields/{fid}/entry_count", {"message": "1"}, method="put")
    assert set(session["answers"][fid]["facts"]) == {"scope_confirmed", "entry_count"}
    assert not session["drafts"] and session["next_action"]["slot_key"] == "entry_0_year"
    assert auth_client.get(url).status_code == 409
    action(auth_client, old, f"fields/{fid}/entry_count", {"message": "2"}, method="put", expected=409)
    action(auth_client, session, f"fields/{fid}/entry_1_name", {"message": "재사용"}, method="put", expected=422)
    loaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert loaded["answers"] == session["answers"] and loaded["next_action"]["slot_key"] == "entry_0_year"
    for key, value in VALUES.items():
        session = action(auth_client, session, "messages", {"message": "새행고유응답" if key == "name" else value})
    session = reviewed(auth_client, session, fid, manual=True)
    exported = auth_client.get(url).content.decode("utf-8-sig")
    assert "새행고유응답" in exported and "이전행" not in exported
