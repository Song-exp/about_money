"""Durable, owner-scoped chat snapshots with optimistic version checking."""
from datetime import datetime, timedelta, timezone
import time

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.information_session import InformationSession
from app.schemas.schemas import UserProfile
from app.services import chat


def _cutoff():
    return datetime.now(timezone.utc) - timedelta(days=max(1, settings.information_session_retention_days))


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def restore(db, session_id, user_id, consultation=None):
    if not session_id or user_id is None:
        return
    row = db.get(InformationSession, session_id)
    if row is None:
        return
    if row.user_id != user_id:
        raise HTTPException(404, '대화를 찾을 수 없습니다.')
    if consultation is not None and row.payload.get('consultation', 'scholarship') != consultation:
        raise HTTPException(409, '다른 종류의 상담 대화입니다. 현재 상담에서 새 대화를 시작하세요.')
    if _aware(row.updated_at) < _cutoff():
        raise HTTPException(409, '대화 보관 기간이 지났습니다. 새 대화를 시작하세요.')
    with chat._SESSIONS_LOCK:
        cached = chat._SESSIONS.get(session_id)
        if cached and cached.user_id == user_id and cached.storage_version == row.version and time.monotonic() - cached.last_seen <= chat.SESSION_IDLE_SEC:
            return
        data = row.payload
        session = chat.ChatSession(session_id=session_id, user_id=user_id)
        session.consultation = data.get('consultation', 'scholarship')
        session.profile = UserProfile.model_validate(data.get('profile', {}))
        for name in ('search_mode', 'search_limit', 'benefit_type', 'search_profile_filters', 'search_query',
                     'profile_evidence', 'candidates', 'last_search_candidates', 'last_tool_result',
                     'last_answer_citations', 'history', 'turns'):
            if name in data:
                setattr(session, name, data[name])
        session.ignored_profile_fields = set(data.get('ignored_profile_fields', []))
        session.storage_version = row.version
        session.state = chat.ChatState.SELECT if session.candidates else chat.ChatState.COLLECT
        chat._SESSIONS[session_id] = session


def save(db, session):
    if session.user_id is None:
        return
    payload = {name: getattr(session, name) for name in (
        'search_mode', 'search_limit', 'benefit_type', 'search_profile_filters', 'search_query',
        'profile_evidence', 'candidates', 'last_search_candidates', 'last_tool_result',
        'last_answer_citations', 'history', 'turns')}
    payload.update(consultation=session.consultation, profile=session.profile.model_dump(), ignored_profile_fields=sorted(session.ignored_profile_fields))
    now = datetime.now(timezone.utc)
    if session.storage_version is None:
        db.add(InformationSession(id=session.session_id, user_id=session.user_id, payload=payload, version=1, updated_at=now))
        next_version = 1
    else:
        result = db.execute(update(InformationSession).where(
            InformationSession.id == session.session_id, InformationSession.user_id == session.user_id,
            InformationSession.version == session.storage_version,
        ).values(payload=payload, version=session.storage_version + 1, updated_at=now))
        if result.rowcount != 1:
            db.rollback()
            with chat._SESSIONS_LOCK:
                chat._SESSIONS.pop(session.session_id, None)
            raise HTTPException(409, '다른 요청에서 대화가 변경되었습니다. 새로고침한 뒤 다시 입력해 주세요.')
        next_version = session.storage_version + 1
    db.execute(delete(InformationSession).where(InformationSession.user_id == session.user_id,
                                               InformationSession.updated_at < _cutoff()))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        with chat._SESSIONS_LOCK:
            chat._SESSIONS.pop(session.session_id, None)
        raise HTTPException(409, '대화 저장 중 상태가 변경되었습니다. 새로고침해 주세요.') from None
    session.storage_version = next_version


def resume(db, user_id, session_id=None, consultation='scholarship'):
    if session_id is None:
        row = db.scalar(select(InformationSession).where(InformationSession.user_id == user_id,
            InformationSession.payload['consultation'].as_string() == consultation,
            InformationSession.updated_at >= _cutoff()).order_by(InformationSession.updated_at.desc()).limit(1))
        if row is None:
            return None
        session_id = row.id
    restore(db, session_id, user_id, consultation)
    with chat._SESSIONS_LOCK:
        session = chat._SESSIONS.get(session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(404, '대화를 찾을 수 없습니다.')
    if session.consultation != consultation:
        raise HTTPException(409, '다른 종류의 상담 대화입니다.')
    return session


def snapshot(session):
    return {'consultation': session.consultation, 'session_id': session.session_id, 'messages': session.turns, 'profile': session.profile.model_dump(),
            'version': session.storage_version, 'search': {'mode': session.search_mode,
            'benefit_type': session.benefit_type, 'ignored_fields': sorted(session.ignored_profile_fields),
            'query': session.search_query}}
