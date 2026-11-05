import importlib
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select
from app.models.user import User
from app.models.writing_session import WritingSession
from tests.conftest import make_scholarship
from tests.test_scholarship_validation import stored_form


def recommend(client, text="소득 3분위, 학점 3.8 서울 거주"):
    response = client.post("/chat/message", json={"message": text})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, scholarship, attempt):
    url = f"/admin/crawl/scholarships/{scholarship.id}/review"
    headers = {"X-Admin-Token": "test-admin-token"}
    context = client.get(url, headers=headers).json()
    assert client.post(url, headers=headers, json={"attempt_id": attempt.id, "fingerprint": context["fingerprint"], "reviewed_all_documents": True}).status_code == 200


def select_card(client, info, scholarship_id):
    return client.post("/writing-sessions", json={"information_session_id": info["session_id"], "scholarship_id": scholarship_id})


def test_card_click_creates_separate_versioned_chat_and_carries_profile(auth_client, db):
    row, attempt = stored_form(db)
    approve(auth_client, row, attempt)
    info = recommend(auth_client)
    assert info["draft"] is None and info["candidates"][0]["source_url"] == row.source_url
    writing = select_card(auth_client, info, row.id).json()
    assert writing["id"] != info["session_id"] and writing["state"] == "collecting"
    assert writing["profile"]["gpa"] == 3.8 and writing["profile"]["income_bracket"] == 3
    assert len(writing["messages"]) == 1 and writing["next_action"]["kind"] == "qualification"
    assert writing["revision_id"] and writing["fields"][0]["source"]["sha256"]
    assert select_card(auth_client, info, row.id).json()["id"] == writing["id"]


def test_student_profile_is_not_carried_to_other_or_unknown_roles(auth_client, db):
    row, attempt = stored_form(db)
    template = row.eligibility["application_fields"][0]
    fields = [{**template, "id": role, "label": "학점", "field_type": "text", "role": role}
              for role in ("student", "guardian", "applicant", "unknown")]
    row.eligibility = {**row.eligibility, "application_fields": fields}
    db.commit()
    approve(auth_client, row, attempt)
    writing = select_card(auth_client, recommend(auth_client), row.id).json()
    assert set(writing["answers"]) == {"student"}
    assert writing["answers"]["student"]["text"] == "3.8"


def test_answer_persists_and_duplicate_request_does_not_duplicate_turn(auth_client, db):
    row, attempt = stored_form(db)
    approve(auth_client, row, attempt)
    writing = select_card(auth_client, recommend(auth_client), row.id).json()
    url = f"/writing-sessions/{writing['id']}"
    writing = auth_client.post(f"{url}/messages", json={"message": "네, 공고 조건을 모두 확인했고 해당합니다.", "expected_version": 0}).json()
    request = {"message": "지역 아동센터에서 1년간 학습 멘토링을 했습니다.", "expected_version": writing["version"]}
    response = auth_client.post(f"{url}/messages", json=request)
    assert response.status_code == 200 and response.json()["state"] == "collecting"
    assert auth_client.post(f"{url}/messages", json=request).status_code == 409
    restored = auth_client.get(url).json()
    assert len(restored["messages"]) == 5 and restored["version"] == 2
    assert restored["answers"][writing["fields"][0]["id"]]["text"] == request["message"]


def test_unreviewed_form_opens_pending_page_without_making_draft(auth_client, db):
    row, _ = stored_form(db)
    info = recommend(auth_client)
    assert info["candidates"][0]["readiness"]["writing_ready"] is False
    result = select_card(auth_client, info, row.id)
    assert result.status_code == 200 and result.json()["state"] == "blocked"
    assert auth_client.post(f"/writing-sessions/{result.json()['id']}/messages", json={"message": "경험", "expected_version": 0}).status_code == 409


def test_stale_original_blocks_old_chat_and_new_selection_preserves_old_answers(auth_client, db):
    row, attempt = stored_form(db)
    approve(auth_client, row, attempt)
    info = recommend(auth_client)
    first = select_card(auth_client, info, row.id).json()
    row.content_hash = "new-original-version"
    db.commit()
    assert auth_client.get(f"/writing-sessions/{first['id']}").json()["state"] == "stale"
    second = select_card(auth_client, info, row.id).json()
    assert second["id"] != first["id"] and second["state"] == "blocked"
    assert len(db.scalars(select(WritingSession)).all()) == 2


def test_card_selection_rechecks_conditions_and_current_candidate_list(auth_client, db):
    row, _ = stored_form(db)
    info = recommend(auth_client)
    assert select_card(auth_client, info, 99999).status_code == 409
    row.eligibility = {**row.eligibility, "income_bracket": [7, 8]}
    db.commit()
    assert select_card(auth_client, info, row.id).status_code == 409


def test_other_account_cannot_read_or_answer_writing_session(auth_client, client, db):
    row, _ = stored_form(db)
    info = recommend(auth_client)
    writing = select_card(auth_client, info, row.id).json()
    auth_client.post("/auth/logout")
    client.post("/auth/signup", json={"privacy_consent": True, "email": "other@khu.ac.kr", "password": "test-password-2"})
    assert client.get(f"/writing-sessions/{writing['id']}").status_code == 404
    assert client.post(f"/writing-sessions/{writing['id']}/messages", json={"message": "정보", "expected_version": 0}).status_code == 404
    assert select_card(client, info, row.id).status_code == 409
    assert client.post("/chat/message", json={"session_id": info["session_id"], "message": "계속"}).status_code == 404


def test_unknown_profile_is_not_reported_as_satisfied_and_grade_mismatch_excluded(auth_client, db):
    db.add(make_scholarship(title="학년 조건", eligibility={"grade_level": ["3"], "gpa_min": 3.5, "validation_version": "evidence-v1"}))
    db.commit()
    unknown = recommend(auth_client, "장학금 알려줘")
    assert unknown["candidates"][0]["eligibility_status"] == "unknown"
    assert "학년 확인 필요" in unknown["candidates"][0]["checks_needed"]
    assert recommend(auth_client, "2학년 학점 3.8")["candidates"] == []
    satisfied = recommend(auth_client, "3학년 학점 3.8")
    assert satisfied["candidates"][0]["eligibility_status"] == "satisfied"


def test_condition_correction_and_explicit_unknown_are_saved(auth_client, db):
    info = recommend(auth_client, "서울 아니고 부산 거주, 1분위 아니고 3분위, 2학년")
    assert info["profile"]["region"] == "부산" and info["profile"]["income_bracket"] == 3
    unknown = auth_client.post("/chat/message", json={"session_id": info["session_id"], "message": "소득분위는 모르겠어"}).json()
    assert unknown["profile"]["income_bracket"] is None
    db.expire_all()
    assert db.scalar(select(User)).income_bracket is None


def test_selection_requires_login(client, db):
    row, _ = stored_form(db)
    info = recommend(client)
    assert select_card(client, info, row.id).status_code == 401


def test_llm_cannot_invent_scholarships_or_override_known_condition(client, db, monkeypatch):
    from app.services import llm
    db.add(make_scholarship(title="실제 장학금", benefit={"amount_desc": "장학금 100만원"}))
    db.commit()
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: "허구 장학금에 9999만원 지원")
    result = recommend(client, "학점 3.8 장학금 금액 비교해줘")
    assert "허구" not in result["message"] and "9999" not in result["message"]
    assert "실제 장학금" in result["message"] and "100만원" in result["message"]
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: '{"ask_field":"gpa"}')
    result = recommend(client, "학점 3.8")
    assert "학점을 알려" not in result["message"]


def test_writing_migration_roundtrip():
    migration = importlib.import_module("migrations.versions.d64f9b832105_add_writing_sessions")
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for name in ("users", "scholarships", "scholarship_revisions"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    with engine.begin() as connection:
        metadata.create_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "writing_sessions" in inspect(connection).get_table_names()
            migration.downgrade()
            assert "writing_sessions" not in inspect(connection).get_table_names()
    engine.dispose()
