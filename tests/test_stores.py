"""지도 서비스 검증(주변 매장 · 최적 결제수단 추천)."""
from __future__ import annotations

KHU_LAT, KHU_LNG = 37.5966, 127.0525


def _by_brand(items):
    return {i["brand"]: i for i in items}


def test_radius_excludes_far_stores(client, seed_stores):
    r = client.get("/stores/nearby",
                   params={"lat": KHU_LAT, "lng": KHU_LNG, "radius_m": 2000})
    assert r.status_code == 200
    branches = {i["branch"] for i in r.json()}
    assert "경희대점" in branches
    assert "부산서면점" not in branches


def test_distance_is_computed_and_sorted(client, seed_stores):
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG,
                               "radius_m": 2000, "sort": "distance"}).json()
    distances = [i["distance_m"] for i in items]
    assert all(d is not None for d in distances)
    assert distances == sorted(distances)


def test_category_filter(client, seed_stores):
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG, "category": "cafe"}).json()
    assert {i["category"] for i in items} == {"cafe"}


def test_unapproved_simple_pay_is_not_best(client, seed_stores):
    """A non-sample rate without approved evidence is not a best deal."""
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG, "radius_m": 2000}).json()
    cu = _by_brand(items)["CU"]
    assert cu["best_deal"] is None
    assert cu["max_discount_rate"] == 0


def test_card_rate_does_not_establish_eligibility(client, seed_stores):
    """Higher rates and input confidence labels do not prove applicability."""
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG, "radius_m": 2000}).json()
    gs = _by_brand(items)["GS25"]
    assert gs["best_deal"] is None
    assert gs["max_card_discount_rate"] == 0
    assert gs["card_benefits"][0]["recommendation_status"] == "unverified"
    detail = client.get(f"/stores/{gs['id']}").json()
    assert detail['best_deal'] is None
    assert detail['max_card_discount_rate'] == 0


def test_card_benefits_hidden_when_user_has_no_such_card(client, seed_stores):
    """보유하지 않은 카드 혜택을 최적 추천으로 보여주면 안 된다."""
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG, "radius_m": 2000,
                               "card_ids": 999999}).json()
    gs = _by_brand(items)["GS25"]
    assert gs["card_benefits"] == []
    assert gs["best_deal"] is None


def test_pay_filter_keeps_stores_without_offers(client, seed_stores):
    """카페처럼 간편결제 혜택이 없는 매장은 pay 필터로 지워지면 안 된다.

    지도에서 매장 자체가 사라지면 사용자는 '문 닫았나?'로 읽는다.
    """
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG,
                               "radius_m": 2000, "pay": "kakao"}).json()
    brands = {i["brand"] for i in items}
    assert "CU" in brands          # 카카오 혜택 보유
    assert "GS25" not in brands    # 네이버만 보유 → 제외
    assert "스타벅스" in brands     # offer 자체가 없음 → 유지


def test_meta_options_available_for_onboarding(client, seed_stores):
    """온보딩 화면이 이 응답에 의존한다. 비면 카드 선택이 불가능해진다."""
    r = client.get("/meta/options")
    assert r.status_code == 200
    body = r.json()
    assert body["cards"], "카드 옵션이 비었다"


def test_card_summaries_merge_categories_and_never_show_fixed_amounts():
    """마트(오프라인)은 마트로 합치고, 단위가 섞인 정액 혜택은 금액을 표시하지 않는다."""
    from types import SimpleNamespace as Benefit
    from app.api.meta import _card_summaries
    from app.models.card import BenefitType

    summary = _card_summaries([
        Benefit(card_id=1, category="마트", benefit_type=BenefitType.PERCENT, value_max=5),
        Benefit(card_id=1, category="마트(오프라인)", benefit_type=BenefitType.PERCENT, value_max=10),
        Benefit(card_id=1, category="카페", benefit_type=BenefitType.PERCENT, value_max=20),
        Benefit(card_id=1, category="편의점", benefit_type=BenefitType.AMOUNT, value_max=500),
        Benefit(card_id=1, category="-", benefit_type=BenefitType.EVENT, value_max=None),
    ])[1]
    assert summary["count"] == 5
    assert summary["categories"] == ["마트", "카페", "편의점"]
    assert summary["highlights"] == ["카페 최대 20%", "마트 최대 10%"]
    only_amount = _card_summaries([Benefit(card_id=2, category="편의점", benefit_type=BenefitType.AMOUNT, value_max=500)])
    assert only_amount[2]["highlights"] == ["편의점 혜택"]



def test_pay_filter_ignores_hidden_sample_offers(client, seed_stores, db):
    """샘플 offer는 화면에 안 나온다. 그것 때문에 매장이 통째로 빠지면 안 된다."""
    from sqlalchemy import update
    from app.models.store import StoreOffer

    db.execute(update(StoreOffer).values(is_sample=True))
    db.commit()
    items = client.get("/stores/nearby",
                       params={"lat": KHU_LAT, "lng": KHU_LNG,
                               "radius_m": 2000, "pay": "naver"}).json()
    brand = _by_brand(items)["CU"]
    assert brand["offers"] == []
