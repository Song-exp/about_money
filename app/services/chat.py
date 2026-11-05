"""Shared information-chat session and condition helpers."""

from __future__ import annotations

import logging

import re

import threading

import time

import uuid

from dataclasses import dataclass, field

from enum import Enum

from sqlalchemy.orm import Session

from app.schemas.schemas import UserProfile

logger = logging.getLogger(__name__)

class ChatState(str, Enum):
    COLLECT = "collect"            # 조건 수집 중
    SELECT = "select"              # 후보 중 선택 대기

@dataclass
class ChatSession:
    session_id: str
    consultation: str = 'scholarship'
    user_id: uuid.UUID | None = None
    state: ChatState = ChatState.COLLECT
    profile: UserProfile = field(default_factory=UserProfile)
    candidates: list[int] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    search_mode: str = "personal"
    search_limit: int = 5
    benefit_type: str | None = None
    ignored_profile_fields: set[str] = field(default_factory=set)
    search_profile_filters: dict = field(default_factory=dict)
    search_query: str = ""
    profile_evidence: dict = field(default_factory=dict)
    last_search_candidates: list[dict] = field(default_factory=list)
    last_tool_result: dict = field(default_factory=dict)
    last_answer_citations: list[int] = field(default_factory=list)
    intent_status: str = "fallback"
    turn_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    turns: list[dict] = field(default_factory=list)
    storage_version: int | None = None
    last_seen: float = field(default_factory=time.monotonic)

_SESSIONS: dict[str, ChatSession] = {}

_SESSIONS_LOCK = threading.Lock()

SESSION_IDLE_SEC = 6 * 3600

MAX_SESSIONS = 2000

def _evict_sessions(now: float) -> None:
    """오래 안 쓴 세션을 버린다. 상한을 넘으면 가장 오래된 것부터 버린다."""
    for sid, sess in list(_SESSIONS.items()):
        if now - sess.last_seen > SESSION_IDLE_SEC:
            del _SESSIONS[sid]
    overflow = len(_SESSIONS) - MAX_SESSIONS + 1
    if overflow > 0:
        oldest = sorted(_SESSIONS.items(), key=lambda kv: kv[1].last_seen)[:overflow]
        for sid, _ in oldest:
            del _SESSIONS[sid]

def get_or_create_session(session_id: str | None, user_id: uuid.UUID | None) -> ChatSession:
    now = time.monotonic()
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id) if session_id else None
        if sess is not None and now - sess.last_seen <= SESSION_IDLE_SEC:
            from fastapi import HTTPException
            if sess.user_id is not None and sess.user_id != user_id:
                raise HTTPException(404, "대화를 찾을 수 없습니다.")
            if user_id and sess.user_id is None:
                sess.user_id = user_id
            sess.last_seen = now
            return sess
        _evict_sessions(now)
        sid = session_id or uuid.uuid4().hex
        sess = ChatSession(session_id=sid, user_id=user_id, last_seen=now)
        _SESSIONS[sid] = sess
        return sess

_BRACKET_RE = re.compile(r"(?<!\d)(10|[0-9])\s*(?:분위|구간)")

_GPA_AFTER_RE = re.compile(r"(?<![\d.])(\d(?:\.\d+)?)(?![\d.])\s*(?:점|학점)")

_GPA_BEFORE_RE = re.compile(r"학점\s*(?:은|이|:)?\s*(\d(?:\.\d+)?)(?![\d.])")

_GRADE_RE = re.compile(r"(?<!\d)([1-4])\s*학년")

_REGIONS = [
    "서울", "경기", "인천", "부산", "대구", "대전", "광주", "울산", "세종",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

_MAJOR_HINTS = {
    "공학": "공학", "이공": "이공계", "인문": "인문", "사회": "사회과학",
    "예술": "예술", "체육": "체육", "의학": "의학", "간호": "간호",
    "경영": "경영", "컴퓨터": "컴퓨터", "소프트웨어": "컴퓨터",
}

def _parse_profile(text: str, profile: UserProfile) -> list[str]:
    """자유 발화에서 조건을 추출해 프로필을 갱신. 갱신된 필드명 목록 반환."""
    updated: list[str] = []

    bracket_matches = list(_BRACKET_RE.finditer(text))
    m = bracket_matches[-1] if bracket_matches else None
    if m:
        v = int(m.group(1))
        if profile.income_bracket != v:
            profile.income_bracket = v
            updated.append("소득분위")

    gpa_matches = sorted([*_GPA_BEFORE_RE.finditer(text), *_GPA_AFTER_RE.finditer(text)], key=lambda m: m.start())
    g = gpa_matches[-1] if gpa_matches else None
    if g:
        try:
            v = float(g.group(1))
            if 0 <= v <= 4.5 and profile.gpa != v:
                profile.gpa = v
                updated.append("학점")
        except ValueError:
            pass

    grade_matches = list(_GRADE_RE.finditer(text))
    gr = grade_matches[-1] if grade_matches else None
    if gr and profile.grade_level != gr.group(1):
        profile.grade_level = gr.group(1)
        updated.append("학년")

    region_matches = [m for m in re.finditer("|".join(_REGIONS), text)
                      if not re.match(r".{0,5}(?:아니|안\s*살|거주하지|출신)", text[m.end():m.end()+12])]
    for m in reversed(region_matches):
        if profile.region != m[0]:
            profile.region = m[0]
            updated.append("지역")
            break

    for kw, major in _MAJOR_HINTS.items():
        if kw in text and profile.major != major:
            profile.major = major
            updated.append("전공")
            break

    explicit_major = re.search(r"전공(?:은|이|:)?\s*([가-힣A-Za-z]+(?:학과|학부|계열))", text)
    if explicit_major:
        profile.major = explicit_major[1]
    for key, label in (("income_bracket", "소득|분위|구간"), ("gpa", "학점"), ("region", "지역|거주")):
        if re.search(f"(?:{label}).{{0,10}}(?:모르|미확인)", text):
            setattr(profile, key, None)

    return updated

@dataclass
class BotReply:
    session_id: str
    state: str
    message: str
    candidates: list[dict] = field(default_factory=list)
    draft: dict | None = None
    profile: dict = field(default_factory=dict)
    selection: dict | None = None
    answer_source: str = "system"
    fallback_reason: str | None = None
    search: dict = field(default_factory=dict)
    tool_result: dict = field(default_factory=dict)
    citations: list[int] = field(default_factory=list)

_PERSISTED_PROFILE_FIELDS = ("income_bracket", "gpa", "grade_level", "region", "major")

def _load_profile_from_account(db: Session, sess: ChatSession) -> None:
    """계정에 저장된 조건을 대화 프로필의 출발점으로 삼는다.

    대화 세션은 프로세스 메모리에 있어 서버가 재시작하면 사라진다. 계정에
    붙여 두면 사용자가 소득분위·학점을 매번 다시 말하지 않아도 된다.
    """
    if sess.user_id is None:
        return
    from app.models.user import User

    user = db.get(User, sess.user_id)
    if user is None:
        return
    for field in _PERSISTED_PROFILE_FIELDS:
        if getattr(sess.profile, field, None) is None:
            value = getattr(user, field, None)
            if value is not None:
                setattr(sess.profile, field, value)

def _save_profile_to_account(db: Session, sess: ChatSession) -> None:
    """대화에서 알아낸 조건을 계정에 남긴다(비로그인이면 아무것도 안 한다)."""
    if sess.user_id is None:
        return
    from app.models.user import User

    user = db.get(User, sess.user_id)
    if user is None:
        return
    changed = False
    for field in _PERSISTED_PROFILE_FIELDS:
        value = getattr(sess.profile, field, None)
        if getattr(user, field, None) != value:
            setattr(user, field, value)
            changed = True
    if changed:
        db.commit()

def handle_message(*args, **kwargs):
    from app.services.information_chat import handle_message as handle
    return handle(*args, **kwargs)

def note_upload(*args, **kwargs):
    from app.services.information_chat import note_upload as note
    return note(*args, **kwargs)
