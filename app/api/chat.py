"""챗봇 대화 API + 채팅 중 파일 업로드 + 임베딩 인덱싱 트리거."""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import current_user, optional_user, rate_limit_chat, require_admin
from app.models.user import User
from app.models.application import (
    ApplicationDocument,
    ApplicationSource,
    DocType,
    UserApplication,
)
from app.services import information_chat as chat_service
from app.services import indexing
from app.services import information_store, chat as shared_chat
from app.utils.file_parser import UnsupportedFileType, extract_text
from app.utils.upload import read_limited

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    # 매 호출이 유료 LLM을 태우므로 분당 상한을 건다.
    dependencies=[Depends(rate_limit_chat)],
)
policy_router = APIRouter(prefix='/policy-chat', tags=['youth-policy-chat'], dependencies=[Depends(rate_limit_chat)])


@router.get('/session')
def resume_information_session(session_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = information_store.resume(db, user.id, session_id)
    return information_store.snapshot(session) if session else None


@router.post('/session')
def new_information_session(user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = shared_chat.get_or_create_session(None, user.id)
    shared_chat._load_profile_from_account(db, session)
    information_store.save(db, session)
    return information_store.snapshot(session)


@policy_router.get('/session')
def resume_policy_session(session_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = information_store.resume(db, user.id, session_id, consultation='youth_policy')
    return information_store.snapshot(session) if session else None


@policy_router.post('/session')
def new_policy_session(user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = shared_chat.get_or_create_session(None, user.id)
    session.consultation = 'youth_policy'
    shared_chat._load_profile_from_account(db, session)
    chat_service._handle_policy_message(db, session, '')
    return information_store.snapshot(session)


class ChatMessageRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=10000)


class CandidateOut(BaseModel):
    index: int
    id: int
    title: str
    deadline: str | None = None
    reasons: list[str] = []
    source_url: str = ""
    benefit: dict = {}
    eligibility: dict = {}
    readiness: dict = {}
    eligibility_status: str = "unknown"
    checks_needed: list[str] = []
    consultation: str = 'scholarship'
    application_period: dict = {}
    source_requirements: list[dict] = []


class ChatMessageResponse(BaseModel):
    session_id: str
    state: str
    message: str
    candidates: list[CandidateOut] = []
    draft: dict | None = None
    profile: dict = {}
    selection: dict | None = None
    answer_source: str = "system"
    fallback_reason: str | None = None
    search: dict = {}
    tool_result: dict = {}
    citations: list[int] = []


@router.post("/message", response_model=ChatMessageResponse)
def send_message(
    req: ChatMessageRequest,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> ChatMessageResponse:
    """대화 한 턴 처리. 백엔드가 세션 상태와 LLM 대화를 관리한다.

    로그인은 필수가 아니다. 장학금 탐색은 진입장벽을 낮게 두고, 개인 데이터를
    쓰거나 남기는 지점(첨부 업로드·초안 저장)에서만 로그인을 요구한다.
    """
    reply = chat_service.handle_message(
        db, req.session_id, user.id if user else None, req.message
    )
    return _reply_out(reply)


def _reply_out(reply):
    return ChatMessageResponse(
        session_id=reply.session_id,
        state=reply.state,
        message=reply.message,
        candidates=[CandidateOut(**c) for c in reply.candidates],
        draft=reply.draft,
        profile=reply.profile,
        selection=getattr(reply, "selection", None),
        answer_source=reply.answer_source,
        fallback_reason=reply.fallback_reason,
        search=reply.search,
        tool_result=reply.tool_result,
        citations=reply.citations,
    )


@policy_router.post('/message', response_model=ChatMessageResponse)
def send_policy_message(req: ChatMessageRequest, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    return _reply_out(chat_service.handle_message(db, req.session_id, user.id if user else None, req.message,
                                                consultation='youth_policy'))


@router.post("/upload", response_model=ChatMessageResponse)
async def upload_in_chat(
    session_id: str | None = Form(default=None),
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ChatMessageResponse:
    """대화 중 과거 신청서(PDF/DOCX/HWP/TXT)를 첨부한다.

    텍스트를 추출해 사용자 이력으로 저장하고, 임베딩 인덱싱까지 수행한다.
    이후 초안 생성 시 이 내용이 근거로 쓰인다(플라이휠).
    """
    information_store.restore(db, session_id, user.id, consultation='scholarship')
    if session_id:
        cached = shared_chat.get_or_create_session(session_id, user.id)
        if cached.consultation != 'scholarship':
            raise HTTPException(409, '과거 신청서 첨부는 장학금 상담에서 이용해 주세요.')
    os.makedirs(settings.upload_dir, exist_ok=True)
    original = os.path.basename(file.filename or "upload")
    safe_name = f"{uuid.uuid4().hex}_{original}"
    dest = os.path.join(settings.upload_dir, safe_name)

    content = await read_limited(file)
    with open(dest, "wb") as f:
        f.write(content)

    try:
        text = extract_text(dest)
    except UnsupportedFileType as e:
        os.remove(dest)  # 거절한 파일을 디스크에 남기지 않는다
        raise HTTPException(status_code=415, detail=str(e))
    finally:
        # 추출 후에는 원본을 다시 읽지 않는다. 주민번호 등이 남아 있는 파일을
        # 계속 들고 있지 않기 위해 지운다.
        if os.path.exists(dest):
            os.remove(dest)

    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail="파일에서 텍스트를 추출하지 못했습니다. 이미지로만 된 PDF일 수 있어요.",
        )

    # 업로드는 로그인 필수이므로 소유자가 항상 정해져 있다.
    sess = chat_service.get_or_create_session(session_id, user.id)
    sess.user_id = user.id

    app_row = UserApplication(
        user_id=sess.user_id,
        scholarship_name=f"첨부: {original}",
        source=ApplicationSource.UPLOADED,
        source_file_path=None,  # 원본은 추출 직후 삭제한다
        is_reusable=True,
    )
    app_row.documents.append(
        ApplicationDocument(
            doc_type=DocType.OTHER,
            prompt_question=None,
            content_text=text,
            char_count=len(text),
        )
    )
    db.add(app_row)
    db.commit()

    # 업로드 즉시 인덱싱해 초안 생성에 바로 활용되게 한다
    try:
        indexing.index_documents(db)
    except Exception:  # noqa: BLE001 - 인덱싱 실패가 업로드를 막지 않게
        pass

    reply = chat_service.note_upload(db, sess.session_id, sess.user_id, original, len(text))
    # 정보 대화에서는 이력을 보관하고 선택을 안내한다. 초안은 별도 작성 대화에서 처리한다.
    return ChatMessageResponse(
        session_id=reply.session_id,
        state=reply.state,
        message=reply.message,
        candidates=reply.candidates or [],
        draft=reply.draft,
        profile=reply.profile,
    )


# --- 인덱싱 트리거(운영/배치) ---
index_router = APIRouter(
    prefix="/admin/index",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@index_router.post("/run")
def run_indexing(db: Session = Depends(get_db)) -> dict:
    """pending 문서 + 미인덱싱 공고를 임베딩·적재."""
    return indexing.reindex_all(db)
