"""보관 원본을 현재 파서로 다시 처리한다. 네트워크 호출은 하지 않는다."""
from datetime import datetime

from app.crawlers.base import BaseCrawler, RawPosting
from app.crawlers.registry import run_crawler
from app.models.scholarship import Category, SourceType
from app.models.source_document import SourceDocument
from app.services.source_assets import parse_asset


def reparse_attempt(db, attempt):
    metadata = attempt.listing_metadata or {}
    source_type = SourceType.PUBLIC
    category = Category.SCHOLARSHIP
    from sqlalchemy import select
    from app.models.scholarship import Scholarship
    scholarship = db.scalar(select(Scholarship).where(Scholarship.content_key == attempt.content_key))
    if scholarship:
        source_type, category = scholarship.source_type, scholarship.category
    raw = RawPosting(title=attempt.title, source_url=attempt.source_url, source_platform=attempt.source_platform,
                     source_type=source_type, category=category, organization=metadata.get("organization"),
                     eligibility=metadata.get("eligibility") or {},
                     posted_at=datetime.fromisoformat(metadata["posted_at"]) if metadata.get("posted_at") else None,
                     deadline_at=datetime.fromisoformat(metadata["deadline_at"]) if metadata.get("deadline_at") else None)
    for item in attempt.documents:
        if item.get("parent_sha256"):
            continue
        document = db.get(SourceDocument, item.get("document_id")) if item.get("document_id") else None
        if document is None:
            raw.source_documents.append({**item, "status": "needs_review", "error": "source_bytes_unavailable"})
        elif item.get("kind") in ("html", "external_page"):
            # HTML 원문은 기존 본문 선택자의 추출을 보존한다. 외부 양식은 아직 검증 대기다.
            raw.source_documents.append({**{k: v for k, v in item.items() if k != "structured"}, "content": document.content})
        else:
            raw.source_documents.extend(parse_asset(document.content, item.get("detected_filename") or item.get("filename") or "document", item["url"]))
    raw.fetch_status = "complete" if raw.source_documents and all(d.get("status") == "parsed" for d in raw.source_documents) else "partial"
    raw.body_text = "\n".join(d.get("extracted_text", "") for d in raw.source_documents)

    class StoredCrawler(BaseCrawler):
        archives_sources = True
        platform = attempt.source_platform
        include_pending_attempts = False

        def preflight(self, respect_robots=True):
            pass

        def fetch(self):
            return [raw]

    # 명시적 관리자 재파싱은 큐 대기 상태와 별도로 실행한다.
    from app.models.source_retry import SourceRetryJob
    job = db.scalar(select(SourceRetryJob).where(SourceRetryJob.content_key == raw.content_key()))
    if job:
        job.state = "done"
        job.failures = 0
    return run_crawler(StoredCrawler(), db)
