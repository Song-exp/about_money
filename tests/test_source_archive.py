"""실제 네트워크 없이 공식 원본 보존·첨부 장애 복구를 검증한다."""
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.core.config import settings
from app.crawlers.base import RawPosting
from app.crawlers.public import KhuScholarshipCrawler
from app.crawlers.registry import purge_expired_postings, run_crawler
from app.models.scholarship import Category, PostingStatus, Scholarship, SourceType
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services.source_archive import pending_attempts
from tests.conftest import KST


def setup_crawler(monkeypatch):
    state = {"attachment_status": 200, "body": "지원 동기를 작성하세요.", "listed": True, "html": None}
    html = '<html><meta charset="utf-8"><div class="bbs-view_c">장학 공고</div><a href="/fileDown/1">신청서</a></html>'

    def respond(request):
        if request.url.path in state.get("assets", {}):
            content, mime = state["assets"][request.url.path]
            return httpx.Response(200, content=content, headers={"content-type": mime})
        if "fileDown" in request.url.path:
            content = state["body"] if isinstance(state["body"], bytes) else state["body"].encode("utf-8")
            return httpx.Response(state["attachment_status"], content=content, headers={
                "content-disposition": f'attachment; filename="{state.get("filename", "application.txt")}"',
            })
        return httpx.Response(200, content=(state["html"] or html).encode("utf-8"))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(settings, "crawl_default_delay_sec", 0)
    monkeypatch.setattr(settings, "crawl_retry_base_sec", 0)
    crawler = KhuScholarshipCrawler()
    monkeypatch.setattr(crawler, "preflight", lambda **kwargs: None)

    def fetch():
        if not state["listed"]:
            return []
        return [RawPosting(
            title="장학 공고", source_url="https://janghak.khu.ac.kr/post/1",
            source_platform=crawler.platform, source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
            posted_at=datetime.now(KST), deadline_at=datetime.now(KST) + timedelta(days=10),
            eligibility={"school": "경희대학교"},
        )]

    monkeypatch.setattr(crawler, "fetch", fetch)
    return crawler, state


def test_preserves_bytes_and_deduplicates_unchanged_sources(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    assert run_crawler(crawler, db)["saved"] == 1
    run_crawler(crawler, db)
    assert db.scalar(select(func.count()).select_from(SourceDocument)) == 2
    assert db.scalar(select(func.count()).select_from(SourceFetchAttempt)) == 2
    attempt = db.scalar(select(SourceFetchAttempt).order_by(SourceFetchAttempt.id.desc()))
    attachment = attempt.documents[1]
    assert attachment["filename"] == "application.txt"
    assert db.get(SourceDocument, attachment["document_id"]).content.decode("utf-8") == state["body"]


def test_admin_reparse_only_processes_selected_archived_notice(db, monkeypatch):
    from app.services.source_reparse import reparse_attempt
    from app.crawlers import registry
    crawler, _ = setup_crawler(monkeypatch)
    run_crawler(crawler, db)
    attempt = db.scalar(select(SourceFetchAttempt))
    def unexpected_queue(*args, **kwargs):
        raise AssertionError('single-notice reparse must not load unrelated retries')
    monkeypatch.setattr(registry, 'pending_attempts', unexpected_queue)
    report = reparse_attempt(db, attempt)
    assert report['fetched'] == 1 and report['detail_failed'] == 0
    assert db.scalar(select(func.count()).select_from(SourceFetchAttempt)) == 2


def test_completed_notice_survives_interruption_of_next_notice(db, monkeypatch):
    from copy import deepcopy
    import pytest
    from app.core.db import SessionLocal
    from app.services import posting_documents
    crawler, _ = setup_crawler(monkeypatch)
    first = crawler.fetch()[0]
    second = deepcopy(first)
    second.source_url = 'https://janghak.khu.ac.kr/post/2'
    monkeypatch.setattr(crawler, 'fetch', lambda: [first, second])
    original = posting_documents.structure_documents
    calls = []
    def interrupt(raw, *args, **kwargs):
        calls.append(raw.source_url)
        if len(calls) == 2:
            raise KeyboardInterrupt('simulated worker interruption')
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(posting_documents, 'structure_documents', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_crawler(crawler, db)
    db.rollback()
    with SessionLocal() as other:
        assert other.scalar(select(func.count()).select_from(SourceFetchAttempt)) == 1
        assert other.scalar(select(func.count()).select_from(Scholarship)) == 1


def test_same_url_new_file_preserves_both_versions(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    run_crawler(crawler, db)
    state["body"] = "변경된 학업 계획을 작성하세요."
    run_crawler(crawler, db)
    assert db.scalar(select(func.count()).select_from(SourceDocument)) == 3
    assert state["body"] in db.scalar(select(Scholarship)).body_text


def test_attachment_failure_keeps_good_data_and_retries_off_listing(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    run_crawler(crawler, db)
    scholarship = db.scalar(select(Scholarship))
    original = scholarship.body_text
    state["attachment_status"] = 503
    report = run_crawler(crawler, db)
    assert report["detail_partial"] == 1
    assert scholarship.body_text == original
    assert scholarship.status == PostingStatus.NEEDS_REVIEW
    assert len(pending_attempts(db, crawler.platform)) == 1
    state.update(attachment_status=200, listed=False)
    assert run_crawler(crawler, db)["retried"] == 1
    # Source recovery succeeds, but no validation LLM is configured in this test.
    assert scholarship.status == PostingStatus.NEEDS_REVIEW
    assert scholarship.eligibility['document_validation']['issues'][0]['code'] == 'validation_model_not_configured'
    assert pending_attempts(db, crawler.platform) == []


def test_first_failure_is_not_published_and_later_recovers(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    state["attachment_status"] = 503
    run_crawler(crawler, db)
    assert db.scalar(select(Scholarship)).status == PostingStatus.NEEDS_REVIEW
    state.update(attachment_status=200, listed=False)
    assert run_crawler(crawler, db)["saved"] == 0  # update the retained review record
    assert db.scalar(select(Scholarship)).eligibility["school"] == "경희대학교"


def test_invalid_html_and_empty_attachment_are_visible_failures(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    state["html"] = "<html>점검중</html>"
    assert run_crawler(crawler, db)["detail_failed"] == 1
    assert db.scalar(select(func.count()).select_from(SourceDocument)) == 1
    from app.models.source_retry import SourceRetryJob
    # 의도적으로 재검토하는 테스트. 자동 검토 대기는 관리자가 해제한다.
    job = db.scalar(select(SourceRetryJob))
    if job:
        job.state = "pending"
        db.commit()
    state.update(html=None, body="   ")
    assert run_crawler(crawler, db)["detail_partial"] == 1
    attempt = db.scalar(select(SourceFetchAttempt).order_by(SourceFetchAttempt.id.desc()))
    assert attempt.documents[1]["status"] == "needs_review"
    assert db.scalar(select(Scholarship)).status == PostingStatus.NEEDS_REVIEW


def test_source_evidence_survives_expired_posting_cleanup(db, monkeypatch):
    crawler, _ = setup_crawler(monkeypatch)
    run_crawler(crawler, db)
    scholarship = db.scalar(select(Scholarship))
    scholarship.deadline_at = datetime.now(KST) - timedelta(days=1)
    db.commit()
    purge_expired_postings(db)
    assert db.scalar(select(Scholarship)).status == PostingStatus.CLOSED
    assert db.scalar(select(func.count()).select_from(SourceDocument)) == 2
    assert db.scalar(select(SourceFetchAttempt)) is not None


def test_source_attempt_api_requires_admin_and_omits_content(client, db, monkeypatch):
    crawler, _ = setup_crawler(monkeypatch)
    run_crawler(crawler, db)
    url = "/admin/crawl/source-attempts"
    assert client.get(url).status_code in (401, 403)
    response = client.get(url, headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    doc = response.json()[0]["documents"][0]
    assert "document_id" in doc
    assert "content" not in doc and "extracted_text" not in doc
    source_url = f"/admin/crawl/source-documents/{doc['document_id']}"
    assert client.get(source_url).status_code in (401, 403)
    download = client.get(source_url, headers={"X-Admin-Token": "test-admin-token"})
    assert download.content == db.get(SourceDocument, doc["document_id"]).content
    assert download.headers["content-disposition"].startswith("attachment;")


def test_source_archive_migration_roundtrip():
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    migration = importlib.import_module("migrations.versions.a31f920c4e11_add_source_archive")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {"source_documents", "source_fetch_attempts"}
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()
