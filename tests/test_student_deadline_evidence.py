import json

import pytest

from app.services import llm
from app.services.posting_extract import extract_structured


@pytest.mark.parametrize("evidence,expected", [
    ("신청기간 : 2026. 9. 28.(월) 09:00 ~ 10. 30.(금) 18:00 / 33일간", "2026-10-30"),
    ("ㅁ 신 청 기간 : 2026. 9. 1.( 화 ) ~ 9. 30.( 화 ) 까 지", "2026-09-30"),
    ("공고 및 접 수 기간 : 2026 년 9 월 28 일 (월) ~ 2026 년 10 월 30 일 ( 금 )", "2026-10-30"),
    ("2026.10.12.(월)까지 인포21 신청", "2026-10-12"),
    ("신청 마감: 2026-10-12", "2026-10-12"),
    ("접수기간\x00 :\x002026.\x009.\x0024.(월)\x00 ~\x002026.\x0010.\x008.(목)\x0018:00", "2026-10-08"),
])
def test_source_deadline_handles_inherited_year_and_ocr_spacing(monkeypatch, evidence, expected):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    result = extract_structured(evidence)
    assert result["deadline_at"].date().isoformat() == expected
    assert result["deadline_source"]["evidence"] == evidence


@pytest.mark.parametrize("evidence", [
    "학교 추 천 신청 마감: 2026-10-12",
    "결과 발 표 신청 기간: 2026-10-12",
    "신청기간: 2026.9.28 ~",
    "신청기간: 2026.9.28 ~ 추후 안내 (발송 2026.10.30)",
    "신청기간: 2026.12.28 ~ 1.5",  # 다음 연도라고 추정하지 않는다.
    "신청기간: 2026.9.28 ~ 2.30",
    "신청기간: 9.28 ~ 10.30",  # 현재 연도를 빌리지 않는다.
    "신청기간: 2026.9.28 ~ 10.30 ~ 11.1",
    "신청기간: 2026.9.28 ~ 10.30, 제출 2026.11.1",
])
def test_uncertain_or_nonstudent_dates_are_rejected(monkeypatch, evidence):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    assert "deadline_at" not in extract_structured(evidence)


def test_model_endpoint_is_supported_but_start_date_is_not(monkeypatch):
    body = "신청기간 : 2026.9.28 ~ 10.30"
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    for proposed, accepted in [("2026-10-30", True), ("2026-09-28", False)]:
        monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps(
            {"deadline": proposed, "deadline_evidence": body}))
        result = extract_structured(body)
        assert result['deadline_at'].date().isoformat() == '2026-10-30'
        assert result['deadline_source']['evidence'] == body
        assert ('deadline_not_supported_by_evidence' in result['issues']) is not accepted


def test_model_changed_quote_remains_rejected(monkeypatch):
    body = "접수기간 : 2026.9.24.(목) ~ 2026.10.8.(목)"
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps({
        "deadline": "2026-10-08", "deadline_evidence": body.replace("24.(목)", "24.(월)")}))
    result = extract_structured(body)
    # 변조한 모델 인용은 거부하고, 별도로 검증한 원문 날짜/인용만 보존한다.
    assert result['deadline_at'].date().isoformat() == '2026-10-08'
    assert result['deadline_source']['evidence'] == body
    assert "deadline_not_supported_by_evidence" in result["issues"]
