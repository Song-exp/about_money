from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.source_document import SourceFetchAttempt
from app.models.source_retry import SourceRetryJob
from app.services.source_archive import source_is_due
from app.crawlers.registry import run_crawler
from tests.test_source_archive import setup_crawler


def test_retry_delay_defers_listed_sources_then_recovers(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    monkeypatch.setattr(settings, "crawl_retry_base_sec", 300)
    state["attachment_status"] = 503
    run_crawler(crawler, db)
    job = db.scalar(select(SourceRetryJob))
    assert job.state == "pending" and job.failures == 1
    assert not source_is_due(db, job.content_key)
    assert run_crawler(crawler, db)["retry_deferred"] == 1
    assert len(list(db.scalars(select(SourceFetchAttempt)))) == 1
    job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    run_crawler(crawler, db)
    assert job.failures == 2
    due = job.next_attempt_at.replace(tzinfo=timezone.utc)
    assert due > datetime.now(timezone.utc) + timedelta(seconds=550)
    job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    state["attachment_status"] = 200
    db.commit()
    run_crawler(crawler, db)
    assert job.state == "done" and job.failures == 0


def test_bounded_replay_does_not_include_unselected_pending_sources(db, monkeypatch):
    from sqlalchemy import func
    crawler, state = setup_crawler(monkeypatch)
    state['attachment_status'] = 503
    run_crawler(crawler, db)
    state['listed'] = False
    crawler.include_pending_attempts = False
    crawler.complete_listing = False
    before = db.scalar(select(func.count()).select_from(SourceFetchAttempt))
    report = run_crawler(crawler, db)
    assert report['retried'] == 0 and not report['listing_complete']
    assert db.scalar(select(func.count()).select_from(SourceFetchAttempt)) == before


def test_max_failures_require_review_and_admin_can_retry(client, db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    monkeypatch.setattr(settings, "crawl_retry_max_attempts", 2)
    state["attachment_status"] = 503
    run_crawler(crawler, db)
    run_crawler(crawler, db)
    job = db.scalar(select(SourceRetryJob))
    assert job.state == "review"
    assert run_crawler(crawler, db)["retry_deferred"] == 1
    url = f"/admin/crawl/retry-jobs/{job.id}/retry"
    assert client.post(url).status_code in (401, 403)
    assert client.post(url, headers={"X-Admin-Token": "test-admin-token"}).status_code == 204
    db.refresh(job)
    assert job.state == "pending" and job.failures == 0
    assert source_is_due(db, job.content_key)


def test_crawler_discovers_image_zip_and_external_application_link(db, monkeypatch):
    from tests.test_source_assets import archive_bytes
    crawler, state = setup_crawler(monkeypatch)
    state["html"] = '''<html><meta charset="utf-8"><div class="bbs-view_c">공고
      <img src="/poster.png"><a href="https://foundation.test/apply">신청 페이지</a>
      </div><a href="/fileDown/1">압축자료</a></html>'''
    state["body"] = archive_bytes({"신청서.txt": "학업계획"})
    state["filename"] = "forms.zip"
    state["assets"] = {"/poster.png": (b"\x89PNG\r\n\x1a\nimage", "image/png")}
    run_crawler(crawler, db)
    attempt = db.scalar(select(SourceFetchAttempt))
    assert any(d.get("kind") == "external_page" and d["status"] == "unresolved" for d in attempt.documents)
    assert any(d.get("filename") == "poster.png" for d in attempt.documents)
    assert any(d.get("error") == "ocr_required" for d in attempt.documents)
    assert any(d.get("member_path") == "신청서.txt" and d["extracted_text"] == "학업계획" for d in attempt.documents)
    assert db.scalar(select(SourceRetryJob)).state == "review"


def test_registered_external_page_is_archived_and_linked_documents_are_read(db, monkeypatch):
    crawler, state = setup_crawler(monkeypatch)
    monkeypatch.setattr(settings, "crawl_external_hosts", "foundation.test")
    state["html"] = '''<html><meta charset="utf-8"><div class="bbs-view_c">
       <a href="https://foundation.test/apply">신청 페이지</a></div></html>'''
    state["assets"] = {
        "/apply": ('<html><meta charset="utf-8"><main>재단 신청 안내<a href="/form.txt">양식</a></main></html>'.encode(), "text/html"),
        "/form.txt": ("학업계획을 작성하세요.".encode(), "text/plain"),
    }
    run_crawler(crawler, db)
    attempt = db.scalar(select(SourceFetchAttempt))
    page = next(d for d in attempt.documents if d["kind"] == "external_page")
    assert page["document_id"] and "재단 신청 안내" in page["extracted_text"]
    assert any(d.get("extracted_text") == "학업계획을 작성하세요." for d in attempt.documents)


def test_retry_queue_migration_roundtrip():
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    migration = importlib.import_module("migrations.versions.c53e8a721094_add_source_retry_queue")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert inspect(connection).get_table_names() == ["source_retry_jobs"]
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()


def test_retry_worker_does_not_fetch_list_or_mark_unrelated_posts_missing(db, monkeypatch):
    from app.scripts.retry_sources import RetryOnlyCrawler
    from app.models.scholarship import Scholarship, PostingStatus

    crawler, state = setup_crawler(monkeypatch)
    state["attachment_status"] = 503
    run_crawler(crawler, db)
    state["attachment_status"] = 200
    monkeypatch.setattr(crawler, "fetch", lambda: (_ for _ in ()).throw(AssertionError("list must not run")))
    report = run_crawler(RetryOnlyCrawler(crawler), db)
    assert report["retried"] == 1 and report["missing_from_source"] == 0
    assert db.scalar(select(Scholarship)).status == PostingStatus.NEEDS_REVIEW
    assert db.scalar(select(Scholarship)).eligibility['document_validation']['issues'][0]['code'] == 'validation_model_not_configured'
