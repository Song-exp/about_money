from app.services import llm
from app.services.posting_extract import extract_structured
from app.services.posting_documents import structure_documents, merge_posting_documents
from app.crawlers.base import RawPosting
from app.crawlers.registry import _enrich_from_body
from app.models.scholarship import Category, SourceType


def raw_posting():
    return RawPosting(title="장학 공고", source_url="https://x.test/post", source_platform="test",
                      source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP)


def test_long_document_tail_is_processed_with_all_constraints():
    body = "안내문\n" * 5000 + "지원동기 (필수, 공백 포함, 500자 이내)\n학업계획\n1000자 이내\n성명:"
    fields = extract_structured(body)["application_fields"]
    assert [f["label"] for f in fields] == ["지원동기", "학업계획", "성명"]
    assert fields[0]["required"] is True and fields[0]["max_length"] == 500
    assert fields[0]["includes_spaces"] is True
    assert fields[1]["max_length"] == 1000 and fields[1]["required"] is None


def test_student_deadline_is_distinguished_from_result_and_nomination_dates():
    result = extract_structured("신청 마감: 2027-01-10\n학교 추천 마감: 2027-01-20\n결과 발표: 2027-02-15")
    assert result["deadline_at"].date().isoformat() == "2027-01-10"
    result = extract_structured("신청 마감: 2027-01-10\n신청 마감: 2027-01-15")
    assert "deadline_at" not in result and "conflicting_student_deadlines" in result["issues"]


def test_grounded_fields_have_file_hash_and_cell_location():
    raw = raw_posting()
    text = "지원동기 (500자 이내)"
    raw.source_documents = [{"url": "https://x.test/form", "filename": "신청서.docx", "content": b"original",
                             "extracted_text": text, "blocks": [{"text": text, "location": "table:1/row:1/cell:2"}]}]
    structure_documents(raw)
    _enrich_from_body(raw)
    field = raw.eligibility["application_fields"][0]
    assert field["source"]["location"] == "table:1/row:1/cell:2"
    assert len(field["source"]["sha256"]) == 64
    assert raw.eligibility["application_questions"] == ["지원동기"]


def test_hallucinated_field_or_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: '''{"application_fields":[
      {"label":"지원동기", "max_length":1000, "length_unit":"characters", "evidence":"지원동기 (500자 이내)"},
      {"label":"봉사경험", "evidence":"봉사경험 100자"}]}''')
    result = extract_structured("지원동기 (500자 이내)")
    assert result["application_fields"] == []
    assert set(result["issues"]) == {"length_not_supported_by_evidence", "field_not_supported_by_evidence", "missing_application_fields"}


def test_conflicting_files_do_not_pick_arbitrary_deadline():
    raw = raw_posting()
    raw.source_documents = [{"structured": {"deadline_at": "2027-01-10T23:59:00+09:00"}},
                            {"structured": {"deadline_at": "2027-01-20T23:59:00+09:00"}}]
    result = merge_posting_documents(raw)
    assert "deadline_at" not in result and "conflicting_student_deadlines" in result["issues"]


def test_file_extraction_result_is_cached_by_bytes_and_parser_version(monkeypatch):
    import hashlib
    raw = raw_posting()
    raw.source_documents = [{"url": "https://x.test/form", "filename": "form.txt", "content": b"data",
                             "parser_version": "blocks-v1", "extracted_text": "지원동기"}]
    structure_documents(raw)
    previous = [{**raw.source_documents[0], "sha256": hashlib.sha256(b"data").hexdigest()}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must use cache")))
    structure_documents(raw, previous)
    assert raw.source_documents[0]["structured"]["application_questions"] == ["지원동기"]


def test_changed_original_with_same_text_changes_hash_and_preserves_new_source():
    raw = raw_posting()
    raw.body_text = "지원동기"
    raw.source_documents = [{"url": "https://x.test/form", "filename": "form.txt", "content": b"version1", "extracted_text": "지원동기"}]
    before = raw.content_hash()
    raw.source_documents[0]["content"] = b"version2"
    assert raw.content_hash() != before


def test_invalid_structured_result_is_reviewable_not_published(monkeypatch):
    raw = raw_posting()
    raw.source_documents = [{"url": "https://x.test/form", "content": b"data", "extracted_text": "지원동기"}]
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *args, **kwargs: '{"application_fields":[{"label":"없는 문항", "evidence":"없는 문항"}]}')
    structure_documents(raw)
    assert raw.fetch_status == "partial"
    assert raw.source_documents[0]["status"] == "needs_review"


def test_saved_original_can_be_reparsed_without_network(client, db, monkeypatch):
    from sqlalchemy import select
    from app.models.source_document import SourceFetchAttempt
    from app.models.scholarship import Scholarship, PostingStatus
    from app.services import ocr
    from tests.test_source_archive import setup_crawler
    from app.crawlers.registry import run_crawler

    crawler, state = setup_crawler(monkeypatch)
    state["html"] = '<html><meta charset="utf-8"><div class="bbs-view_c"><img src="/poster.png"></div></html>'
    state["assets"] = {"/poster.png": (b"\x89PNG\r\n\x1a\nimage", "image/png")}
    run_crawler(crawler, db)
    attempt = db.scalar(select(SourceFetchAttempt))
    monkeypatch.setattr(ocr, "image_blocks", lambda content: ([{"text": "지원동기 (500자 이내)", "location": "ocr:1", "method": "ocr", "confidence": 98}], None))
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network prohibited")))
    url = f"/admin/crawl/source-attempts/{attempt.id}/reparse"
    assert client.post(url).status_code in (401, 403)
    response = client.post(url, headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert response.json()["detail_partial"] == 0
    row = db.scalar(select(Scholarship))
    assert row.status == PostingStatus.NEEDS_REVIEW and row.eligibility["application_fields"][0]["max_length"] == 500
    assert row.eligibility['document_validation']['issues'][0]['code'] == 'validation_model_not_configured'
