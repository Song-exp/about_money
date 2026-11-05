"""선택한 장학금의 독립 작성 대화 시작·원본 버전 고정·문항별 정보 보관."""
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.models.writing_session import WritingSession
from app.services.scholarship_history import record_revision
from app.services.scholarship_readiness import readiness


def initial_message(scholarship, session):
    from app.services.writing_requirements import next_action, normalize_answers
    if not readiness(scholarship)["writing_ready"]:
        return "선택한 장학금의 신청 양식 확인이 아직 완료되지 않았습니다. 공고 원문을 확인해 주세요."
    action = next_action(session, scholarship, normalize_answers(session), session.workflow)
    return action["question"] if action else "필요한 정보를 확인했습니다. 문항별 초안을 생성할 수 있습니다."


def start_session(db, user_id, scholarship, profile):
    from app.services.consultation_scope import in_scope
    if not in_scope(scholarship, 'scholarship'):
        raise HTTPException(409, '청년정책은 정책 상담에서 지원 대상과 신청 방법을 확인해 주세요.')
    from app.services.writing_requirements import notice_fingerprint, writing_fingerprint
    fingerprint = writing_fingerprint(scholarship)
    query = select(WritingSession).where(WritingSession.user_id == user_id,
                                        WritingSession.scholarship_id == scholarship.id,
                                        WritingSession.form_fingerprint == fingerprint)
    session = db.scalar(query)
    if session is None:
        revision = record_revision(db, scholarship)
        session = WritingSession(user_id=user_id, scholarship_id=scholarship.id, revision_id=revision.id,
                                 form_fingerprint=fingerprint, profile=profile.model_dump(),
                                 fields=deepcopy((scholarship.eligibility or {}).get("application_fields", [])),
                                 answers={}, workflow={"notice_fingerprint": notice_fingerprint(scholarship),
                                                       "form_groups": deepcopy((scholarship.eligibility or {}).get("form_groups", []))},
                                 messages=[], state="collecting" if readiness(scholarship)["writing_ready"] else "blocked")
        profile_keys = {"소득분위": "income_bracket", "소득구간": "income_bracket", "학점": "gpa", "학년": "grade_level", "거주지역": "region", "전공": "major"}
        for field in session.fields:
            if field.get("role", "student") != "student":
                continue
            key = profile_keys.get("".join(field["label"].split()))
            value = session.profile.get(key) if key else None
            if field["field_type"] == "text" and value is not None:
                session.answers[field["id"]] = {"label": field["label"], "text": str(value), "status": "carried_profile"}
        session.messages = [{"role": "bot", "text": initial_message(scholarship, session)}]
        db.add(session)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            session = db.scalar(query)
            if session is None:
                raise
    return view(db, session)


def get_owned(db, session_id, user_id):
    session = db.scalar(select(WritingSession).where(WritingSession.id == session_id, WritingSession.user_id == user_id))
    if session is None:
        raise HTTPException(404, "작성 대화를 찾을 수 없습니다.")
    return session


def view(db, session):
    from app.services.writing_workflow import view as workflow_view
    return workflow_view(db, session)


def answer(db, session, text, expected_version):
    from app.services.writing_workflow import message
    return message(db, session, text, expected_version)
