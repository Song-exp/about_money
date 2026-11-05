"""본인이 재사용을 허용한 관련 이력을 선택·확인하여 문항 사실에 반영한다."""
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from app.models.application import ApplicationDocument, UserApplication
from app.services.writing_requirements import normalize_answers, requirements
from app.services.writing_workflow import _active, _field, _set_fact, _invalidate, _commit, _clean


def related(db, session, field_id):
    field = _field(session, field_id)
    if field.get("history_reuse_policy"):
        raise HTTPException(422, "원문이 이전 신청 내용의 그대로 붙여넣기를 금지합니다. 이번 신청의 내용을 대화로 새로 확인하세요.")
    if field.get("semantic_kind") == "submission_checklist":
        raise HTTPException(422, "서류 준비 상태는 이번 신청에서 새로 확인하세요.")
    if field["field_type"] == "checkbox":
        raise HTTPException(422, "선택·동의 항목은 이번 신청에서 본인이 직접 선택하세요.")
    rows = db.execute(select(ApplicationDocument, UserApplication).join(UserApplication).where(
        UserApplication.user_id == session.user_id, UserApplication.is_reusable.is_(True),
        UserApplication.id != session.application_id if session.application_id else True,
    ).order_by(ApplicationDocument.id.desc()).limit(100)).all()
    def score(row):
        prompt = row[0].prompt_question or ""
        return 2 if field["label"] in prompt else sum(word in row[0].content_text for word in ("학업", "장학", "활동", "계획", "경험") if word in field["label"])
    rows.sort(key=score, reverse=True)
    return [{"document_id": d.id, "application_id": str(a.id), "scholarship_name": a.scholarship_name,
             "prompt_question": d.prompt_question, "content_text": d.content_text} for d, a in rows[:5]]


def use(db, session, field_id, slot_key, document_id, quote, confirmed, expected_version):
    _active(db, session)
    field = _field(session, field_id)
    if field.get("history_reuse_policy"):
        raise HTTPException(422, "원문이 이전 신청 내용의 그대로 붙여넣기를 금지합니다. 이번 신청의 내용을 대화로 새로 확인하세요.")
    if field.get("semantic_kind") == "submission_checklist":
        raise HTTPException(422, "서류 준비 상태는 이번 신청에서 새로 확인하세요.")
    if field["field_type"] == "checkbox":
        raise HTTPException(422, "선택·동의 항목은 이번 신청에서 본인이 직접 선택하세요.")
    if not confirmed or slot_key not in [r["key"] for r in requirements(field)]:
        raise HTTPException(422, "이번 문항에 사용할 항목과 실제 본인 내용인지 확인하세요.")
    row = db.execute(select(ApplicationDocument, UserApplication).join(UserApplication).where(
        ApplicationDocument.id == document_id, UserApplication.user_id == session.user_id,
        UserApplication.is_reusable.is_(True),
        UserApplication.id != session.application_id if session.application_id else True)).first()
    if row is None:
        raise HTTPException(404, "재사용할 수 있는 본인 이력을 찾을 수 없습니다.")
    quote = _clean(quote)
    if not quote or quote not in row[0].content_text:
        raise HTTPException(422, "원문에 있는 정확한 내용을 선택하세요.")
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    _set_fact(answers, field, slot_key, quote, {"kind": "history", "document_id": document_id, "application_id": str(row[1].id)}, confirmed=True)
    _invalidate(workflow)
    return _commit(db, session, expected_version, answers, workflow,
                   [*session.messages, {"role": "bot", "text": f"'{field['label']}'에 확인한 과거 내용을 반영했습니다."}])
