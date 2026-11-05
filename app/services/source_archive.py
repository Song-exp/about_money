"""공식 공고 원본 저장 및 최신 실패 목록. 원본을 외부 LLM에 보내지 않는다."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.crawlers.base import RawPosting
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.models.source_retry import SourceRetryJob
from app.core.config import settings
from app.utils.text import strip_nul


def archive_attempt(db: Session, raw: RawPosting) -> SourceFetchAttempt:
    documents = []
    for item in raw.source_documents:
        meta = {k: v for k, v in item.items() if k != "content"}
        content = item.get("content")
        if content is not None:
            digest = hashlib.sha256(content).hexdigest()
            doc = db.scalar(select(SourceDocument).where(SourceDocument.sha256 == digest))
            if doc is None:
                doc = SourceDocument(sha256=digest, content=content)
                db.add(doc)
                db.flush()
            meta.update(document_id=doc.id, sha256=digest, byte_size=len(content))
        documents.append(strip_nul(meta))
    attempt = SourceFetchAttempt(
        content_key=raw.content_key(), source_platform=raw.source_platform,
        source_url=raw.source_url, title=strip_nul(raw.title),
        status=raw.fetch_status, error=raw.fetch_error, documents=documents,
        listing_metadata=strip_nul({
            "posted_at": raw.posted_at.isoformat() if raw.posted_at else None,
            "deadline_at": raw.deadline_at.isoformat() if raw.deadline_at else None,
            "deadline_raw": raw.deadline_raw, "organization": raw.organization,
            "eligibility": raw.eligibility,
        }),
    )
    db.add(attempt)
    db.flush()
    job = db.scalar(select(SourceRetryJob).where(SourceRetryJob.content_key == raw.content_key()))
    if raw.fetch_status != "complete" or job is not None:
        if job is None:
            job = SourceRetryJob(content_key=raw.content_key(), source_platform=raw.source_platform, failures=0)
            db.add(job)
        now = datetime.now(timezone.utc)
        if raw.fetch_status == "complete":
            job.state, job.failures, job.error = "done", 0, None
            job.next_attempt_at = now
        else:
            job.failures += 1
            review = any(d.get("status") in ("needs_review", "unresolved") or
                         d.get("error") == "UnsupportedFileType" for d in documents)
            job.state = "review" if review or job.failures >= settings.crawl_retry_max_attempts else "pending"
            delay = min(settings.crawl_retry_max_sec, settings.crawl_retry_base_sec * 2 ** min(job.failures - 1, 20))
            job.next_attempt_at = now + timedelta(seconds=delay)
            job.error = raw.fetch_error or next((d.get("error") for d in documents if d.get("error")), "partial")
        db.flush()
    return attempt


def source_is_due(db: Session, content_key: str) -> bool:
    job = db.scalar(select(SourceRetryJob).where(SourceRetryJob.content_key == content_key))
    if job is None or job.state == "done":
        return True
    if job.state == "review":
        return False
    due = job.next_attempt_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= datetime.now(timezone.utc)


def pending_attempts(db: Session, platform: str, limit: int = 100) -> list[SourceFetchAttempt]:
    latest = select(func.max(SourceFetchAttempt.id)).where(
        SourceFetchAttempt.source_platform == platform
    ).group_by(SourceFetchAttempt.content_key)
    return list(db.scalars(select(SourceFetchAttempt).where(
        SourceFetchAttempt.id.in_(latest), SourceFetchAttempt.status != "complete",
    ).outerjoin(SourceRetryJob, SourceRetryJob.content_key == SourceFetchAttempt.content_key).where(
        (SourceRetryJob.id.is_(None)) | ((SourceRetryJob.state == "pending") &
         (SourceRetryJob.next_attempt_at <= datetime.now(timezone.utc))),
    ).order_by(SourceFetchAttempt.id).limit(limit)))
