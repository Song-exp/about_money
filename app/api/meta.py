"""온보딩 선택 옵션 API.

첫 화면에서 사용자가 자기 정보를 고를 때 쓰는 목록을 제공한다.
카드·간편결제는 DB 실데이터를 기반으로 하고, 통신사는 아직 혜택
데이터가 없어 정적 목록만 제공한다(선택은 저장되지만 필터에는 미반영).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.card import BenefitType, Card
from app.models.store import PayMethod, StoreCategory

router = APIRouter(prefix="/meta", tags=["meta"])

_PAY_LABELS = {
    PayMethod.KAKAO: "카카오페이",
    PayMethod.TOSS: "토스페이",
    PayMethod.NAVER: "네이버페이",
}

_CATEGORY_LABELS = {
    StoreCategory.CONVENIENCE: "편의점",
    StoreCategory.CAFE: "카페",
    StoreCategory.RESTAURANT: "음식점",
    StoreCategory.MART: "마트",
    StoreCategory.BAKERY: "베이커리",
    StoreCategory.HNB: "H&B",
    StoreCategory.OTHER: "기타",
}

# 실제 카드가 아니라 시드 자료의 묶음 항목이다.
_HIDDEN_CARD_KEYS = ("KB국민(나라사랑·펭수 노리)",)

# 통신사: 현재 혜택 데이터가 없어 선택용 정적 목록만 제공한다.
_TELECOMS = ["SKT", "KT", "LG U+", "알뜰폰", "해당 없음"]
_STUDENT_CREDENTIALS = [
    ("student_card", "학생증"),
    ("student_tok", "톡학생증"),
]
_BENEFIT_PROGRAMS = [
    ("khu_alliance", "경희대 제휴혜택"),
    ("onnuri", "온누리상품권"),
    ("seoulpay", "서울Pay+"),
    ("zeropay", "제로페이"),
]


class ProfileOption(BaseModel):
    value: str
    label: str


class CardOption(BaseModel):
    id: int
    card_name: str
    issuer: str | None = None
    benefit_count: int = 0
    categories: list[str] = []
    highlights: list[str] = []
    card_type: str | None = None
    image_url: str | None = None


class PayOption(BaseModel):
    value: PayMethod
    label: str


class CategoryOption(BaseModel):
    value: StoreCategory
    label: str


class OptionsOut(BaseModel):
    cards: list[CardOption]
    pay_methods: list[PayOption]
    categories: list[CategoryOption]
    telecoms: list[str]
    telecom_supported: bool = False
    student_credentials: list[ProfileOption]
    benefit_programs: list[ProfileOption]


def _card_summaries(benefits) -> dict[int, dict]:
    """카드별 혜택 건수·업종·대표 혜택. 입력은 지도가 쓰는 유효 혜택이어야 한다."""
    rates: dict[int, dict[str, float]] = {}
    counts: dict[int, int] = {}
    for benefit in benefits:
        counts[benefit.card_id] = counts.get(benefit.card_id, 0) + 1
        category = re.sub(r"\(.*\)", "", benefit.category or "").strip()
        if not category or category == "-":
            continue
        by_category = rates.setdefault(benefit.card_id, {})
        # 정액 혜택은 단위가 원·엔으로 섞여 있어 금액을 표시하지 않는다.
        rate = benefit.value_max if benefit.benefit_type == BenefitType.PERCENT else None
        by_category[category] = max(by_category.get(category, 0), rate or 0)
    result = {}
    for card_id, count in counts.items():
        by_category = rates.get(card_id, {})
        top = sorted(by_category.items(), key=lambda item: (-item[1], item[0]))[:2]
        result[card_id] = {"count": count, "categories": sorted(by_category),
                           "highlights": [f"{name} 최대 {rate:g}%" if rate else f"{name} 혜택" for name, rate in top]}
    return result


@router.get("/options", response_model=OptionsOut)
def get_options(db: Session = Depends(get_db)) -> OptionsOut:
    """온보딩에서 고를 수 있는 항목 목록."""
    from app.services.card_publication import load_public_programs
    program_counts = {}
    for publication, version, _, _ in load_public_programs(db):
        program_counts[publication.card_id] = program_counts.get(publication.card_id, 0) + len(version.payload['rules'])
    from app.api.stores import _load_card_benefits
    # 브랜드별로 묶여 오므로 같은 혜택이 여러 번 나오지 않게 id로 합친다.
    visible = {b.id: b for items in _load_card_benefits(db, False).values() for b in items}
    summaries = _card_summaries(visible.values())

    cards = []
    for c in db.scalars(select(Card).where(Card.card_key.not_in(_HIDDEN_CARD_KEYS)).order_by(Card.card_name)):
        summary = summaries.get(c.id, {})
        cards.append(CardOption(
            id=c.id,
            card_name=c.card_name,
            issuer=c.issuer,
            benefit_count=summary.get("count", 0) + program_counts.get(c.id, 0),
            categories=summary.get("categories", []),
            highlights=summary.get("highlights", []),
            card_type=c.card_type,
            image_url=c.image_url,
        ))

    return OptionsOut(
        cards=cards,
        pay_methods=[PayOption(value=m, label=_PAY_LABELS[m]) for m in PayMethod],
        categories=[
            CategoryOption(value=cat, label=_CATEGORY_LABELS[cat]) for cat in StoreCategory
        ],
        telecoms=_TELECOMS,
        telecom_supported=False,
        student_credentials=[
            ProfileOption(value=value, label=label)
            for value, label in _STUDENT_CREDENTIALS
        ],
        benefit_programs=[
            ProfileOption(value=value, label=label)
            for value, label in _BENEFIT_PROGRAMS
        ],
    )
