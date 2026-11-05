import json
from datetime import datetime, timedelta, timezone

from app.services import llm
from app.services.posting_extract import extract_structured
from app.services.scholarship_readiness import readiness
from tests.conftest import make_scholarship


def mock_extract(monkeypatch, data):
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: json.dumps(data, ensure_ascii=False))


def test_fact_values_need_exact_evidence_and_semantic_support(monkeypatch):
    body = "평점 3.5 이상\n장학금 100만원\n소득 1~3분위\n서울 거주 학생"
    mock_extract(monkeypatch, {"eligibility": {"gpa_min": 4.5, "income_bracket": [1, 2, 3], "region": ["부산"]},
                              "benefit": {"amount_krw": 1000000}, "required_documents": ["재학증명서"],
                              "fact_evidence": {"eligibility.gpa_min": "평점 3.5 이상", "eligibility.income_bracket": "소득 1~3분위",
                                                "eligibility.region": "서울 거주 학생", "benefit.amount_krw": "장학금 100만원"}})
    result = extract_structured(body, {"sha256": "hash"})
    assert result["eligibility"] == {"income_bracket": [1, 2, 3]}
    assert result["benefit"] == {"amount_krw": 1000000}
    assert set(result["issues"]) == {"unsupported_fact:eligibility.gpa_min", "unsupported_fact:eligibility.region", "unsupported_required_document"}
    assert result["facts"][0]["source"]["sha256"] == "hash"


def test_unevidenced_conditions_and_wrong_date_meaning_are_rejected(monkeypatch):
    mock_extract(monkeypatch, {"eligibility": {"gpa_min": 3.5}, "deadline": "2027-02-10", "deadline_evidence": "학교 추천 마감 2027-02-10"})
    result = extract_structured("학교 추천 마감 2027-02-10")
    assert result["eligibility"] == {} and "deadline_at" not in result
    assert "deadline_not_supported_by_evidence" in result["issues"]


def test_missing_known_form_item_requires_review(monkeypatch):
    mock_extract(monkeypatch, {"application_fields": [{"label": "지원동기", "evidence": "지원동기"}]})
    result = extract_structured("지원동기\n학업계획")
    assert result["coverage"]["missing_labels"] == ["학업계획"]
    assert "missing_application_fields" in result["issues"]


def stored_form(db):
    from app.crawlers.base import RawPosting
    from app.models.scholarship import Category, SourceType
    from app.services.posting_documents import structure_documents
    from app.services.source_archive import archive_attempt
    from app.crawlers.registry import _enrich_from_body, upsert_posting
    from sqlalchemy import select
    from app.models.scholarship import Scholarship
    raw = RawPosting(title="검토 대상", source_url="https://x.test/form", source_platform="test",
                     source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
                     deadline_at=datetime.now(timezone.utc) + timedelta(days=10), body_text="지원동기 (500자 이내)")
    raw.source_documents = [{"url": raw.source_url, "filename": "신청서.txt", "content": raw.body_text.encode(),
                             "extracted_text": raw.body_text, "status": "parsed"}]
    structure_documents(raw)
    _enrich_from_body(raw)
    attempt = archive_attempt(db, raw)
    upsert_posting(db, raw)
    db.commit()
    return db.scalar(select(Scholarship)), attempt


def test_recommendation_and_writing_readiness_are_separate(db):
    row, _ = stored_form(db)
    assert readiness(row)["recommendation_ready"] is True
    assert readiness(row)["writing_ready"] is False


def test_operator_review_uses_latest_original_and_invalidates_on_change(client, db):
    row, attempt = stored_form(db)
    url = f"/admin/crawl/scholarships/{row.id}/review"
    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get(url).status_code in (401, 403)
    context = client.get(url, headers=headers).json()
    assert context["documents"][0]["extracted_text"] == row.body_text
    payload = {"attempt_id": attempt.id, "fingerprint": context["fingerprint"], "reviewed_all_documents": True}
    assert client.post(url, headers=headers, json={**payload, "fingerprint": "stale"}).status_code == 409
    assert client.post(url, headers=headers, json={**payload, "reviewed_all_documents": False}).status_code == 409
    approved = client.post(url, headers=headers, json=payload)
    assert approved.status_code == 200 and approved.json()["writing_ready"] is True
    db.refresh(row)
    row.content_hash = "changed-original"
    db.commit()
    assert readiness(row)["writing_ready"] is False
    assert client.post(url, headers=headers, json=payload).status_code == 409


def test_draft_is_blocked_before_review_and_rejects_invented_question(auth_client, db):
    row, attempt = stored_form(db)
    url = "/ai/draft"
    assert auth_client.post(url, json={"scholarship_id": row.id, "questions": ["지원동기"]}).status_code == 409
    headers = {"X-Admin-Token": "test-admin-token"}
    review_url = f"/admin/crawl/scholarships/{row.id}/review"
    context = auth_client.get(review_url, headers=headers).json()
    auth_client.post(review_url, headers=headers, json={"attempt_id": attempt.id, "fingerprint": context["fingerprint"], "reviewed_all_documents": True})
    assert auth_client.post(url, json={"scholarship_id": row.id, "questions": ["만든 문항"]}).status_code == 422
    assert auth_client.post(url, json={"scholarship_id": row.id, "questions": ["지원동기"]}).status_code == 200


def test_expired_or_invalid_data_is_not_recommended(db):
    from app.services.matching import match_scholarships
    from app.schemas.schemas import UserProfile
    db.add(make_scholarship(title="만료", deadline_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.add(make_scholarship(title="오류", eligibility={"extraction_issues": ["unsupported_fact:benefit.amount_krw"]}))
    db.commit()
    assert match_scholarships(db, UserProfile()) == []


def test_partial_latest_attempt_cannot_be_approved(client, db):
    from app.models.source_document import SourceFetchAttempt
    row, attempt = stored_form(db)
    failure = SourceFetchAttempt(content_key=row.content_key, source_platform="test", source_url=row.source_url,
                                 title=row.title, status="partial", documents=attempt.documents, listing_metadata={})
    db.add(failure)
    db.commit()
    url = f"/admin/crawl/scholarships/{row.id}/review"
    headers = {"X-Admin-Token": "test-admin-token"}
    context = client.get(url, headers=headers).json()
    assert client.post(url, headers=headers, json={"attempt_id": failure.id, "fingerprint": context["fingerprint"], "reviewed_all_documents": True}).status_code == 409


def test_initial_failure_is_in_review_queue_and_quality_metrics(client, db):
    from app.models.source_document import SourceFetchAttempt
    attempt = SourceFetchAttempt(content_key="not-published", source_platform="test", source_url="https://x.test/fail",
                                 title="처음 수집 실패", status="partial", error="missing_file", listing_metadata={},
                                 documents=[{"url": "https://x.test/fail", "status": "needs_review", "error": "structured_validation_failed",
                                             "structured": {"issues": ["unsupported_fact:benefit.amount_krw"]}}])
    db.add(attempt)
    db.commit()
    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get("/admin/crawl/review-queue").status_code in (401, 403)
    queue = client.get("/admin/crawl/review-queue", headers=headers).json()
    assert len(queue) == 1 and queue[0]["scholarship_id"] is None
    context = client.get(f"/admin/crawl/source-attempts/{attempt.id}/review", headers=headers).json()
    assert context["readiness"]["writing_ready"] is False
    assert context["issues"] == ["unsupported_fact:benefit.amount_krw"]
    quality = client.get("/admin/crawl/quality", headers=headers).json()
    assert quality["total"] == 0 and quality["source_statuses"] == {"partial": 1}
    assert quality["source_issues"]["unsupported_fact:benefit.amount_krw"] == 1
    assert quality["accuracy_measured"] is False


def test_complex_exceptions_do_not_become_simple_eligibility_filters(monkeypatch):
    mock_extract(monkeypatch, {"eligibility": {"region": ["서울"], "grade_level": ["4"]},
                              "fact_evidence": {"eligibility.region": "서울 거주자 제외", "eligibility.grade_level": "1~4학년"}})
    result = extract_structured("서울 거주자 제외\n1~4학년")
    assert result["eligibility"] == {}
    assert set(result["issues"]) == {"unsupported_fact:eligibility.region", "unsupported_fact:eligibility.grade_level"}
