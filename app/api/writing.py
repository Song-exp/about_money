import uuid
from fastapi import APIRouter, Depends, HTTPException, Response, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.security import current_user, rate_limit_chat
from app.models.user import User
from app.services import writing_chat, information_chat, writing_workflow, writing_history, writing_export

router = APIRouter(prefix="/writing-sessions", tags=["writing"], dependencies=[Depends(rate_limit_chat)])


class SelectRequest(BaseModel):
    information_session_id: str
    scholarship_id: int


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    expected_version: int = Field(ge=0)


class VersionRequest(BaseModel):
    expected_version: int = Field(ge=0)


class ApplicabilityRequest(VersionRequest):
    applies: bool = Field(strict=True)


class EditRequest(MessageRequest):
    pass


class ReviewRequest(VersionRequest):
    confirmed: bool
    manual_checks_confirmed: bool = False


class SaveRequest(VersionRequest):
    allow_reuse: bool = False


class HistoryRequest(VersionRequest):
    field_id: str
    slot_key: str
    document_id: int
    quote: str = Field(min_length=1, max_length=10000)
    confirmed: bool


def mutable(db, user, session_id, version):
    session = writing_chat.get_owned(db, session_id, user.id)
    if session.version != version:
        raise HTTPException(409, "대화가 변경되었습니다. 최신 대화를 다시 불러오세요.")
    return session


@router.post("")
def select_scholarship(req: SelectRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    information, scholarship = information_chat.validate_selection(db, req.information_session_id, user.id, req.scholarship_id)
    return writing_chat.start_session(db, user.id, scholarship, information.profile)


@router.get("/{session_id}")
def get_session(session_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_chat.view(db, writing_chat.get_owned(db, session_id, user.id))


@router.post("/{session_id}/messages")
def send_message(session_id: uuid.UUID, req: MessageRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_chat.answer(db, mutable(db, user, session_id, req.expected_version), req.message, req.expected_version)


@router.post("/{session_id}/fields/{field_id}/confirm")
def confirm_field(session_id: uuid.UUID, field_id: str, req: ReviewRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not req.confirmed:
        raise HTTPException(422, "본인의 사실인지 확인해야 합니다.")
    return writing_workflow.confirm_field(db, mutable(db, user, session_id, req.expected_version), field_id, req.expected_version)


@router.put("/{session_id}/fields/{field_id}/{slot_key}")
def edit_field(session_id: uuid.UUID, field_id: str, slot_key: str, req: EditRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.edit_fact(db, mutable(db, user, session_id, req.expected_version), field_id, slot_key, req.message, req.expected_version)


@router.post("/{session_id}/draft")
def generate_draft(session_id: uuid.UUID, req: VersionRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.generate(db, mutable(db, user, session_id, req.expected_version), req.expected_version)


@router.post("/{session_id}/fields/{field_id}/skip")
def skip_field(session_id: uuid.UUID, field_id: str, req: VersionRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.skip_field(db, mutable(db, user, session_id, req.expected_version), field_id, req.expected_version)


@router.post("/{session_id}/conditions/{condition_id}")
def set_applicability(session_id: uuid.UUID, condition_id: str, req: ApplicabilityRequest,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.set_applicability(db, mutable(db, user, session_id, req.expected_version),
                                              condition_id, req.applies, req.expected_version)


@router.put("/{session_id}/draft/{field_id}")
def edit_draft(session_id: uuid.UUID, field_id: str, req: EditRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.edit_draft(db, mutable(db, user, session_id, req.expected_version), field_id, req.message, req.expected_version)


@router.post("/{session_id}/review")
def review_draft(session_id: uuid.UUID, req: ReviewRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not req.confirmed:
        raise HTTPException(422, "전체 초안 내용을 확인해야 합니다.")
    return writing_workflow.review_draft(db, mutable(db, user, session_id, req.expected_version), req.expected_version, req.manual_checks_confirmed)


@router.post("/{session_id}/save")
def save_draft(session_id: uuid.UUID, req: SaveRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_workflow.save(db, writing_chat.get_owned(db, session_id, user.id), req.expected_version, req.allow_reuse)


@router.get("/{session_id}/history")
def related_history(session_id: uuid.UUID, field_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_history.related(db, writing_chat.get_owned(db, session_id, user.id), field_id)


@router.post("/{session_id}/history")
def use_history(session_id: uuid.UUID, req: HistoryRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return writing_history.use(db, mutable(db, user, session_id, req.expected_version), req.field_id, req.slot_key,
                               req.document_id, req.quote, req.confirmed, req.expected_version)


@router.get("/{session_id}/export")
def export_draft(session_id: uuid.UUID, format: str = Query(default="docx", pattern="^(txt|docx)$"), user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = writing_chat.get_owned(db, session_id, user.id)
    content, media_type = writing_export.export(db, session, format)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="scholarship-{session.scholarship_id}-draft.{format}"', "X-Content-Type-Options": "nosniff"})
