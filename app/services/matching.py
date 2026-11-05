"""매칭 엔진.

2단계:
  1) 하드 필터 - 필수 자격 불일치 공고를 완전히 제외 (신뢰의 핵심)
  2) 소프트 랭킹 - 관심분야/마감임박/혜택금액 가중 합산으로 정렬

임베딩 유사도(관심분야)는 선택적으로 결합한다. 벡터 검색이 불가한
환경에서도 규칙 기반 점수만으로 동작하도록 설계했다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.scholarship import Category, PostingStatus, Scholarship
from app.schemas.schemas import MatchResult, ScholarshipOut, UserProfile
from app.services.scholarship_readiness import readiness

# SQLite는 타임존을 보존하지 않아 naive datetime을 돌려준다.
# DB에 저장할 때 KST 기준이었으므로 naive면 KST로 간주한다.
_KST = timezone(timedelta(hours=9))


def _as_aware(dt: datetime | None) -> datetime | None:
    """naive datetime을 KST aware로 보정한다(aware면 그대로)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt


def _passes_hard_filter(s: Scholarship, profile: UserProfile) -> tuple[bool, list[str]]:
    """필수 자격 검사. 통과 여부 + 통과 사유(설명용)."""
    reasons: list[str] = []
    elig = s.eligibility or {}
    from app.services.eligibility_rules import evaluate_rules
    reviewed = evaluate_rules(s, profile)
    if reviewed is not None:
        return reviewed['status'] != 'not_eligible', reviewed['reasons']

    # 소득분위: 공고가 허용하는 분위 목록에 사용자가 포함되어야 함
    brackets = elig.get("income_bracket")
    if brackets and profile.income_bracket is not None:
        if profile.income_bracket not in brackets:
            return False, []
        reasons.append(f"소득 {profile.income_bracket}분위 충족")

    # 최소 학점
    gpa_min = elig.get("gpa_min")
    if gpa_min is not None and profile.gpa is not None:
        if profile.gpa < gpa_min:
            return False, []
        reasons.append(f"학점 {profile.gpa} ≥ 기준 {gpa_min}")

    # 지역 (전국이면 통과)
    regions = elig.get("region")
    if regions and profile.region:
        if profile.region not in regions and "전국" not in regions:
            return False, []
        reasons.append(f"지역 {profile.region} 일치")

    grades = elig.get("grade_level")
    if grades and profile.grade_level:
        if profile.grade_level not in [str(g) for g in grades]:
            return False, []
        reasons.append(f"{profile.grade_level}학년 조건 충족")

    return True, reasons


def evaluate_eligibility(s: Scholarship, profile: UserProfile) -> dict:
    from app.services.eligibility_rules import evaluate_rules
    reviewed = evaluate_rules(s, profile)
    if reviewed is not None:
        return reviewed
    ok, reasons = _passes_hard_filter(s, profile)
    if not ok:
        return {"status": "not_eligible", "reasons": [], "checks_needed": []}
    eligibility = s.eligibility or {}
    checks = []
    requirements = (eligibility.get('policy_validation') or eligibility.get('document_validation') or {}).get('requirements', [])
    if any(item.get('category') in ('eligibility', 'exclusion') for item in requirements):
        checks.append('공고별 추가·제외 조건 확인 필요')
    for key, label in (("income_bracket", "소득분위"), ("gpa_min", "학점"), ("region", "거주 지역"), ("grade_level", "학년")):
        attribute = "gpa" if key == "gpa_min" else key
        if eligibility.get(key) and getattr(profile, attribute) is None:
            checks.append(f"{label} 확인 필요")
    if eligibility.get("target_desc") or eligibility.get("scope") or eligibility.get("major"):
        checks.append("공고의 추가 자격 조건 확인 필요")
    if not any(eligibility.get(k) for k in ("income_bracket", "gpa_min", "region", "grade_level")):
        checks.append("공고 자격 조건 확인 필요")
    if readiness(s)["facts_status"] == "legacy_unverified":
        checks.append("공고 원문 자격 대조 필요")
    return {"status": "unknown" if checks else "satisfied", "reasons": reasons, "checks_needed": checks}


def _soft_score(s: Scholarship, profile: UserProfile, now: datetime) -> float:
    """소프트 랭킹 점수(0~1 근사)."""
    score = 0.0

    # 관심분야 텍스트 매칭(간이) - 임베딩 유사도로 대체 가능
    if profile.interests:
        blob = f"{s.title} {s.body_text}".lower()
        hits = sum(1 for kw in profile.interests if kw.lower() in blob)
        score += 0.4 * min(hits / max(len(profile.interests), 1), 1.0)

    # 마감 임박 가중치(가까울수록 높음, 30일 스케일)
    # 주의: SQLite는 타임존을 저장하지 않아 naive datetime이 돌아온다.
    # aware/naive를 섞어 연산하면 TypeError가 나므로 정규화한다.
    deadline = _as_aware(s.deadline_at)
    if deadline:
        days = (deadline - now).total_seconds() / 86400
        if days >= 0:
            score += 0.3 * max(0.0, 1.0 - days / 30.0)

    # 혜택 금액 정규화(1천만원 스케일)
    amount = (s.benefit or {}).get("amount_krw")
    if isinstance(amount, (int, float)) and amount > 0:
        score += 0.2 * min(amount / 10_000_000, 1.0)

    return round(score, 4)


def match_scholarships(
    db: Session,
    profile: UserProfile,
    category: Category = Category.SCHOLARSHIP,
    limit: int = 20,
    benefit_type: str | None = None,
    query: str | None = None,
    consultation: str | None = None,
) -> list[MatchResult]:
    """프로필 기반 매칭. 하드필터 통과분만 소프트랭킹으로 정렬."""
    now = datetime.now(timezone.utc)
    from app.services.consultation_scope import scope_clause
    scope = consultation or ('youth_policy' if category == Category.GOV_BENEFIT else 'scholarship')
    stmt = select(Scholarship).where(
        scope_clause(scope),
        Scholarship.status.in_([PostingStatus.OPEN, PostingStatus.CLOSING_SOON]),
    )
    if query:
        for term in query.split():
            stmt = stmt.where(or_(Scholarship.title.contains(term, autoescape=True),
                                  Scholarship.organization.contains(term, autoescape=True)))
    candidates = db.scalars(stmt).all()

    results: list[MatchResult] = []
    for s in candidates:
        if not readiness(s)["recommendation_ready"]:
            continue
        if benefit_type and not matches_benefit_type(s, benefit_type):
            continue
        ok, reasons = _passes_hard_filter(s, profile)
        if not ok:
            continue
        score = _soft_score(s, profile, now)
        eligibility = evaluate_eligibility(s, profile)
        results.append(
            MatchResult(
                scholarship=ScholarshipOut.model_validate(s),
                match_score=score,
                reasons=reasons,
                eligibility_status=eligibility["status"],
                checks_needed=eligibility["checks_needed"],
            )
        )

    results.sort(key=lambda r: r.match_score, reverse=True)
    return results[:limit]


def matches_query(row, query):
    text = f'{row.title} {row.organization or ""}'.casefold()
    return all(term.casefold() in text for term in (query or '').split())


def matches_benefit_type(s: Scholarship, kind: str) -> bool:
    """명시된 지급 유형·제목·지급 설명만 사용한다. 미확인은 생활비로 추측하지 않는다."""
    benefit = s.benefit or {}
    declared = benefit.get("type")
    if declared in ("living", "tuition", "both"):
        return declared in (kind, "both")
    text = f"{s.title} {benefit.get('amount_desc') or ''}"
    if kind == "living":
        text = re.sub(r"생활비\s*(?:성\s*)?(?:지원\s*)?(?:미지원|불가|제외|아님|아니|지원하지)", "", text)
        return bool(re.search(r"생활비|생활\s*장학", text))
    return "등록금" in text
