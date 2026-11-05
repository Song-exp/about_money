import importlib
import io
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from docx import Document
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select

from app.models.application import ApplicationDocument, ApplicationSource, UserApplication
from app.models.writing_session import WritingSession
from app.services.writing_drafts import validate_length
from tests.test_scholarship_validation import stored_form
from tests.test_writing_flow import approve, recommend, select_card


def setup_writing(client, db):
    row, attempt = stored_form(db)
    approve(client, row, attempt)
    db.refresh(row)
    session = select_card(client, recommend(client), row.id).json()
    return row, session


def action(client, session, path, body=None, method="post", expected=200):
    response = getattr(client, method)(f"/writing-sessions/{session['id']}/{path}",
                                      json={"expected_version": session["version"], **(body or {})})
    assert response.status_code == expected, response.text
    return response.json()


def collect(client, session):
    session = action(client, session, "messages", {"message": "네, 공고 자격을 확인했고 모두 해당합니다."})
    assert session["next_action"]["slot_key"] == "reason"
    session = action(client, session, "messages", {"message": "학업에 필요한 교재 비용을 마련하기 위해 신청합니다."})
    assert session["next_action"]["slot_key"] == "impact"
    session = action(client, session, "messages", {"message": "교재를 구입해 이번 학기 전공 수업에 집중할 수 있습니다."})
    assert session["state"] == "awaiting_confirmation"
    return action(client, session, f"fields/{session['fields'][0]['id']}/confirm", {"confirmed": True})


def test_collection_confirmation_review_save_and_exports(auth_client, db):
    _, session = setup_writing(auth_client, db)
    action(auth_client, session, "draft", expected=409)
    session = collect(auth_client, session)
    assert session["state"] == "ready"
    session = action(auth_client, session, "draft")
    assert session["state"] == "review" and "교재 비용" in session["drafts"][0]["text"]
    assert not session["draft_reviewed"]
    action(auth_client, session, "save", expected=409)
    base = f"/writing-sessions/{session['id']}"
    assert auth_client.get(f"{base}/export").status_code == 409
    session = action(auth_client, session, "review", {"confirmed": True})
    txt = auth_client.get(f"{base}/export?format=txt")
    docx = auth_client.get(f"{base}/export?format=docx")
    assert txt.status_code == docx.status_code == 200
    assert session["drafts"][0]["text"] in txt.content.decode("utf-8-sig")
    assert "지원동기" in "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    session = action(auth_client, session, "save")
    assert session["state"] == "saved"
    application = db.get(UserApplication, uuid.UUID(session["application_id"]))
    assert not application.is_reusable and application.documents[0].content_text == session["drafts"][0]["text"]
    again = action(auth_client, session, "save")
    assert again["version"] == session["version"]
    assert len(db.scalars(select(UserApplication)).all()) == 1
    session = action(auth_client, session, "save", {"allow_reuse": True})
    db.refresh(application)
    action(auth_client, session, "history", {"field_id": session["fields"][0]["id"], "slot_key": "reason",
           "document_id": application.documents[0].id, "quote": application.documents[0].content_text, "confirmed": True}, expected=404)


def test_uncertain_or_short_answers_do_not_fill_facts(auth_client, db):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, session, "messages", {"message": "모르겠어요"})
    assert session["next_action"]["kind"] == "qualification"
    session = action(auth_client, session, "messages", {"message": "네, 해당합니다."})
    for text in ("모르겠어요", "좋아요"):
        session = action(auth_client, session, "messages", {"message": text})
        assert session["next_action"]["slot_key"] == "reason" and session["answers"] == {}
    action(auth_client, session, f"fields/{session['fields'][0]['id']}/confirm", {"confirmed": True}, expected=409)


def test_llm_cannot_add_facts_and_malformed_output_falls_back(auth_client, db, monkeypatch):
    from app.services import llm
    _, session = setup_writing(auth_client, db)
    session = collect(auth_client, session)
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: '{"order":["invented"],"text":"전국 대회 1등"}')
    session = action(auth_client, session, "draft")
    assert "전국 대회" not in session["drafts"][0]["text"]
    assert session["drafts"][0]["method"] == "confirmed_evidence_assembly"
    def generate_supported(system, prompt, **kwargs):
        import json
        data = json.loads(prompt)
        if data['task'] == 'essay_grounding_check':
            return json.dumps({'supported': True, 'unsupported_sentences': [], 'missing_fact_keys': []})
        return json.dumps({'paragraphs': [{
            'text': '교재를 구입해 이번 학기 전공 수업에 집중하고자 합니다. 학업에 필요한 교재 비용을 마련하기 위해 장학금을 신청합니다.',
            'fact_keys': ['reason', 'impact'],
            'quotes': [{'fact_key': key, 'quote': value} for key, value in data['confirmed_facts'].items()]
        }]}, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat_complete", generate_supported)
    session = action(auth_client, session, "draft")
    assert session["drafts"][0]["text"].startswith("교재를 구입해")
    assert session["drafts"][0]["method"] == "llm_grounded_prose"


def test_edit_invalidate_review_and_old_versions_fail(auth_client, db):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, collect(auth_client, session), "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    old = session
    fid = session["fields"][0]["id"]
    session = action(auth_client, session, f"draft/{fid}", {"message": "수정한 본인 지원동기입니다."}, method="put")
    assert not session["draft_reviewed"]
    action(auth_client, old, "review", {"confirmed": True}, expected=409)
    action(auth_client, session, "save", expected=409)
    session = action(auth_client, session, f"fields/{fid}/reason", {"message": "실제 교재 구입 비용 지원이 필요합니다."}, method="put")
    assert session["drafts"] == [] and session["state"] == "awaiting_confirmation"


def test_length_constraints_block_review_and_manual_checks(auth_client, db):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, collect(auth_client, session), "draft")
    fid = session["fields"][0]["id"]
    session = action(auth_client, session, f"draft/{fid}", {"message": "가" * 501}, method="put")
    assert session["state"] == "needs_revision"
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    assert validate_length({"field_type": "essay", "length_unit": "characters", "min_length": 3}, "가 나")["errors"] == ["below_min_length"]
    assert validate_length({"field_type": "essay", "length_unit": "bytes", "max_length": 4, "instructions": "UTF-8"}, "가나")["errors"] == ["above_max_length"]
    assert validate_length({"field_type": "essay", "length_unit": "pages"}, "내용")["warnings"]


def history_row(db, owner, reusable=True):
    application = UserApplication(user_id=owner, scholarship_name="이전 장학금", source=ApplicationSource.UPLOADED, is_reusable=reusable)
    application.documents.append(ApplicationDocument(prompt_question="지원동기", content_text="학업 교재 비용이 필요해 장학금을 신청합니다."))
    db.add(application); db.commit()
    return application


def test_history_requires_owner_consent_exact_quote_and_confirmation(auth_client, db):
    _, session = setup_writing(auth_client, db)
    owner = db.get(WritingSession, uuid.UUID(session["id"])).user_id
    allowed = history_row(db, owner)
    history_row(db, owner, False)
    history_row(db, uuid.uuid4())
    fid = session["fields"][0]["id"]
    listed = auth_client.get(f"/writing-sessions/{session['id']}/history", params={"field_id": fid}).json()
    assert [h["document_id"] for h in listed] == [allowed.documents[0].id]
    payload = {"field_id": fid, "slot_key": "reason", "document_id": allowed.documents[0].id, "quote": allowed.documents[0].content_text, "confirmed": False}
    action(auth_client, session, "history", payload, expected=422)
    action(auth_client, session, "history", {**payload, "quote": "원문에 없는 사실", "confirmed": True}, expected=422)
    session = action(auth_client, session, "history", {**payload, "confirmed": True})
    assert session["answers"][fid]["facts"]["reason"]["confirmed"]
    session = action(auth_client, session, "messages", {"message": "네, 자격을 모두 확인했습니다."})
    session = action(auth_client, session, "messages", {"message": "교재를 구입해 전공 공부에 집중할 수 있습니다."})
    session = action(auth_client, session, f"fields/{fid}/confirm", {"confirmed": True})
    session = action(auth_client, session, "draft")
    session = action(auth_client, session, "review", {"confirmed": True})
    allowed.is_reusable = False; db.commit()
    action(auth_client, session, "draft", expected=409)
    action(auth_client, session, "save", expected=409)
    assert auth_client.get(f"/writing-sessions/{session['id']}/export").status_code == 409


def test_notice_condition_changes_block_existing_session(auth_client, db):
    row, session = setup_writing(auth_client, db)
    row.eligibility = {**row.eligibility, "target_desc": "새 추가 조건"}; db.commit()
    assert auth_client.get(f"/writing-sessions/{session['id']}").json()["state"] == "stale"
    action(auth_client, session, "messages", {"message": "네 해당합니다."}, expected=409)
    fresh = select_card(auth_client, recommend(auth_client), row.id).json()
    assert fresh["id"] != session["id"] and fresh["state"] == "collecting"


def test_optional_only_skip(auth_client, db):
    _, session = setup_writing(auth_client, db)
    fid = session["fields"][0]["id"]
    action(auth_client, session, f"fields/{fid}/skip", expected=422)
    stored = db.get(WritingSession, uuid.UUID(session["id"]))
    stored.fields = [{**f, "required": False} for f in stored.fields]; db.commit()
    session = action(auth_client, session, f"fields/{fid}/skip")
    session = action(auth_client, session, "messages", {"message": "네, 공고 자격을 확인했습니다."})
    session = action(auth_client, session, "draft")
    assert session["drafts"][0]["text"] == "" and session["drafts"][0]["validation"]["errors"] == []


def test_workflow_migration_roundtrip():
    initial = importlib.import_module("migrations.versions.d64f9b832105_add_writing_sessions")
    migration = importlib.import_module("migrations.versions.e75a0c943216_add_writing_workflow")
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for name in ("users", "scholarships", "scholarship_revisions", "user_applications"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    with engine.begin() as connection:
        metadata.create_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            initial.upgrade(); migration.upgrade()
            assert {"workflow", "application_id"} <= {c["name"] for c in inspect(connection).get_columns("writing_sessions")}
            migration.downgrade()
            assert "workflow" not in {c["name"] for c in inspect(connection).get_columns("writing_sessions")}
            initial.downgrade()
    engine.dispose()


def test_manual_page_review_required_before_export(auth_client, db):
    _, session = setup_writing(auth_client, db)
    stored = db.get(WritingSession, uuid.UUID(session["id"]))
    stored.fields = [{**f, "length_unit": "pages", "max_length": 1} for f in stored.fields]
    db.commit()
    session = action(auth_client, collect(auth_client, session), "draft")
    action(auth_client, session, "review", {"confirmed": True}, expected=409)
    session = action(auth_client, session, "review", {"confirmed": True, "manual_checks_confirmed": True})
    assert auth_client.get(f"/writing-sessions/{session['id']}/export?format=txt").status_code == 200


def test_all_writing_actions_protect_session_owner(auth_client, db):
    _, session = setup_writing(auth_client, db)
    fid = session["fields"][0]["id"]
    auth_client.post("/auth/logout")
    assert auth_client.post("/auth/signup", json={"privacy_consent": True, "email": "second@khu.ac.kr", "password": "test-password-2"}).status_code == 201
    for path, body, method in (("draft", {}, "post"), ("review", {"confirmed": True}, "post"), ("save", {}, "post"),
                               (f"draft/{fid}", {"message": "타인 수정 시도"}, "put"),
                               (f"fields/{fid}/confirm", {"confirmed": True}, "post"),
                               (f"fields/{fid}/reason", {"message": "타인 정보 수정"}, "put")):
        action(auth_client, session, path, body, method=method, expected=404)
    assert auth_client.get(f"/writing-sessions/{session['id']}/export").status_code == 404
    assert auth_client.get(f"/writing-sessions/{session['id']}/history", params={"field_id": fid}).status_code == 404


def test_qualification_disagreement_blocks_generation(auth_client, db):
    _, session = setup_writing(auth_client, db)
    session = action(auth_client, session, "messages", {"message": "아니요, 추가 조건에 해당하지 않습니다."})
    assert session["state"] == "ineligible"
    action(auth_client, session, "draft", expected=409)
    session = action(auth_client, session, "messages", {"message": "네, 잘못 입력했습니다. 조건에 해당합니다."})
    assert session["next_action"]["kind"] == "field"


def test_qualification_decimal_income_does_not_crash(auth_client, db):
    row, session = setup_writing(auth_client, db)
    row.eligibility = {**row.eligibility, "income_bracket": [3]}; db.commit()
    session = select_card(auth_client, recommend(auth_client, "소득분위 모르겠어요"), row.id).json()
    session = action(auth_client, session, "messages", {"message": "2.5"})
    assert session["next_action"]["key"] == "income_bracket"
    session = action(auth_client, session, "messages", {"message": "3"})
    assert session["profile"]["income_bracket"] == 3
