"""크롤러 레지스트리 및 일간 파이프라인.

매일(기본 00:10 KST, 날짜가 바뀐 직후):
  purge_expired_postings(마감 상태 변경) -> 소스별 수집·대조 -> CrawlRun 기록

소스 유형(BaseCrawler.full_sync):
  - API(True): 모집중 전체를 받아 upsert하고, 목록에서 빠진 공고는 마감 상태로 보존한다.
  - 게시판(False): 목록의 기존 글도 상세·첨부를 재확인하며 실패 건은 별도 재시도한다.
    내용이 같은 공고의 LLM 구조화는 생략한다. 마감일 미확인 게시글은
    검토 상태로 보존한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crawlers.base import BaseCrawler, ComplianceError, RawPosting
from app.crawlers.date_parser import KST, normalize_deadline
from app.crawlers.public import KhuScholarshipCrawler, OnjungchoungnyeonCrawler
from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.scholarship import PostingStatus, Scholarship
from app.models.source_document import SourceFetchAttempt
from app.utils.text import strip_nul
from app.services.source_archive import archive_attempt, pending_attempts, source_is_due
from app.services.scholarship_history import record_revision
from app.services.posting_extract import EXTRACTOR_VERSION
from app.services.scholarship_readiness import VALIDATION_VERSION

logger = logging.getLogger(__name__)

# 등록된 모든 어댑터
REGISTRY: list[type[BaseCrawler]] = [
    OnjungchoungnyeonCrawler,
    KhuScholarshipCrawler,
]


def _deadline_blocked(issues):
    return bool(set(issues) & {'conflicting_student_deadlines', 'conflicting_student_deadline_times',
                               'invalid_student_deadline_time', 'invalid_student_deadline'})


def _enrich_from_body(raw: RawPosting) -> None:
    """첨부에서 추출한 본문이 충분하면 구조화 정보를 병합한다.

    본문에서 마감일·자격·지급액·신청문항을 뽑아 RawPosting의 정형 필드를
    보강한다. 신청문항(application_questions)은 초안 생성 시 '공고 양식'으로
    쓰이므로 eligibility에 함께 보관한다.
    """
    from app.services.posting_documents import merge_posting_documents
    data = merge_posting_documents(raw)
    if not data:
        return

    if data.get("deadline_at"):
        raw.deadline_at = data["deadline_at"]

    elig = dict(raw.eligibility or {})
    elig.pop("form_review", None)
    for key in ("income_bracket", "gpa_min", "grade_level", "region", "target_desc", "deadline_source"):
        elig.pop(key, None)
    elig.update({k: v for k, v in (data.get("eligibility") or {}).items() if v})
    elig["application_questions"] = data.get("application_questions", [])
    elig["application_fields"] = data.get("application_fields", [])
    elig["form_groups"] = data.get("form_groups", [])
    elig["form_exclusions"] = data.get("form_exclusions", [])
    elig["field_merge_decisions"] = data.get("merge_decisions", [])
    elig["extraction_issues"] = data.get("issues", [])
    elig["extractor_version"] = EXTRACTOR_VERSION
    elig["validation_version"] = VALIDATION_VERSION
    elig["fact_sources"] = data.get("facts", [])
    elig["form_coverage"] = data.get("coverage", [])
    elig["form_status"] = "extracted_unverified" if elig["application_fields"] else "missing"
    if data.get("deadline_source"):
        elig["deadline_source"] = data["deadline_source"]
    if _deadline_blocked(data.get('issues', [])):
        raw.deadline_at = None
    raw.eligibility = elig

    raw.benefit = data.get("benefit") or {}
    raw.required_documents = data.get("required_documents", [])


def _to_model(raw: RawPosting) -> Scholarship:
    return Scholarship(
        content_key=raw.content_key(),
        title=raw.title,
        organization=raw.organization,
        source_type=raw.source_type,
        category=raw.category,
        source_platform=raw.source_platform,
        source_url=raw.source_url,
        deadline_at=raw.deadline_at,
        posted_at=raw.posted_at,
        eligibility=raw.eligibility,
        benefit=raw.benefit,
        required_documents=raw.required_documents,
        body_text=raw.body_text,
        content_hash=raw.content_hash(),
        status=PostingStatus.OPEN,
    )


def _scrub(raw: RawPosting) -> None:
    """저장 전에 NUL 바이트를 지운다.

    PostgreSQL 은 text·jsonb 에 NUL 을 담지 못해 트랜잭션이 통째로 실패한다.
    PDF·한글 문서 추출과 외부 API 응답에서 섞여 들어온다. 여기서 한 번 걸러야
    신규 저장과 갱신, 변경 감지 해시가 모두 같은 값을 보게 된다.
    """
    for field in ("title", "organization", "body_text", "source_url", "deadline_raw"):
        value = getattr(raw, field, None)
        if isinstance(value, str):
            setattr(raw, field, strip_nul(value))
    for field in ("eligibility", "benefit", "required_documents"):
        value = getattr(raw, field, None)
        if value is not None:
            setattr(raw, field, strip_nul(value))


def upsert_posting(db: Session, raw: RawPosting, status: PostingStatus = PostingStatus.OPEN) -> bool:
    """content_key 기준 멱등 upsert. 신규 저장 시 True."""
    _scrub(raw)
    existing = db.scalar(
        select(Scholarship).where(Scholarship.content_key == raw.content_key())
    )
    if existing is None:
        existing = _to_model(raw)
        existing.status = status
        db.add(existing)
        record_revision(db, existing)
        return True
    # API는 본문이 같아도 금액·자격·일정이 바뀔 수 있다.
    record_revision(db, existing)
    for name in ("title", "organization", "category", "deadline_at", "posted_at", "body_text",
                 "eligibility", "benefit", "required_documents"):
        setattr(existing, name, getattr(raw, name))
    existing.content_hash = raw.content_hash()
    existing.status = status
    existing.crawled_at = datetime.now(timezone.utc)
    record_revision(db, existing)
    return False


def _change_status(db: Session, rows, status: PostingStatus) -> int:
    changed = 0
    for row in rows:
        if row.status != status:
            record_revision(db, row)
            row.status = status
            record_revision(db, row)
            changed += 1
    return changed


def purge_expired_postings(db: Session) -> int:
    """호환용 함수명. 마감 공고를 보존하며 새로 마감 전환한 건수를 반환."""
    # SQLite는 tz를 버리고 KST 벽시계 시각으로 저장하므로 KST로 비교한다(PG도 동일 결과).
    closed = _change_status(db, db.scalars(select(Scholarship).where(
            Scholarship.deadline_at.is_not(None),
            Scholarship.deadline_at < datetime.now(KST),
            Scholarship.status != PostingStatus.CLOSED,
        )), PostingStatus.CLOSED)
    db.commit()
    return closed


def run_crawler(crawler: BaseCrawler, db: Session) -> dict:
    """단일 어댑터 실행. 준수 체크·수집 실패는 스킵으로 리포트."""
    report = {
        "platform": crawler.platform, "fetched": 0, "saved": 0, "removed": 0, "skipped": False,
        "detail_failed": 0, "detail_partial": 0, "retried": 0,
        "retry_deferred": 0,
        "closed": 0, "needs_review": 0, "missing_from_source": 0, "listing_complete": False,
    }
    try:
        crawler.preflight(respect_robots=settings.crawl_respect_robots)
    except ComplianceError as e:
        logger.warning("compliance skip: %s", e)
        report["skipped"] = True
        report["reason"] = str(e)
        return report

    try:
        postings = crawler.fetch()
    except Exception as e:  # noqa: BLE001 - 한 소스 장애가 다른 소스 수집을 막지 않게
        logger.exception("fetch failed: %s", crawler.platform)
        report["skipped"] = True
        # 예외 메시지에는 요청 URL(API 키 포함)이 섞일 수 있어 타입만 남긴다.
        report["reason"] = f"fetch 실패: {type(e).__name__}"
        report["listing_pages"] = getattr(crawler, "last_listing_pages", None)
        return report
    report["fetched"] = len(postings)
    report["listing_pages"] = getattr(crawler, "last_listing_pages", None)
    report["listing_complete"] = crawler.full_sync or crawler.complete_listing
    listed_keys = {raw.content_key() for raw in postings}
    verified_keys = set(listed_keys)
    if crawler.archives_sources and getattr(crawler, 'include_pending_attempts', True):
        keys = {raw.content_key() for raw in postings}
        for failed in pending_attempts(db, crawler.platform):
            if failed.content_key not in keys:
                metadata = failed.listing_metadata or {}
                postings.append(RawPosting(
                    title=failed.title, source_url=failed.source_url,
                    source_platform=crawler.platform, source_type=crawler.source_type,
                    category=crawler.category,
                    posted_at=datetime.fromisoformat(metadata["posted_at"]) if metadata.get("posted_at") else None,
                    deadline_at=datetime.fromisoformat(metadata["deadline_at"]) if metadata.get("deadline_at") else None,
                    deadline_raw=metadata.get("deadline_raw"), organization=metadata.get("organization"),
                    eligibility=metadata.get("eligibility") or {},
                ))
                keys.add(failed.content_key)
                report["retried"] += 1

    now = datetime.now(timezone.utc)
    for raw in postings:
        existing = db.scalar(select(Scholarship).where(Scholarship.content_key == raw.content_key()))
        if not crawler.full_sync:
            if crawler.archives_sources and not source_is_due(db, raw.content_key()):
                report["retry_deferred"] += 1
                continue
            if existing and raw.posted_at is None:
                raw.posted_at = existing.posted_at
            try:
                crawler.fetch_detail(raw)
            except Exception as exc:
                raw.fetch_status = "failed"
                raw.fetch_error = type(exc).__name__
            if crawler.archives_sources:
                from app.services.posting_documents import structure_documents
                previous = db.scalar(select(SourceFetchAttempt).where(
                    SourceFetchAttempt.content_key == raw.content_key(),
                ).order_by(SourceFetchAttempt.id.desc()).limit(1))
                structure_documents(raw, previous.documents if previous else None)
                archive_attempt(db, raw)
                if raw.source_platform == 'khu_janghak':
                    from app.services.posting_validation import validate_posting
                    _enrich_from_body(raw)
                    if raw.deadline_at:
                        raw.deadline_at = (raw.deadline_at.replace(tzinfo=KST) if raw.deadline_at.tzinfo is None
                                           else raw.deadline_at.astimezone(KST))
                    _scrub(raw)
                    review = validate_posting(raw, (existing.eligibility or {}).get('document_validation') if existing else None)
                    raw.eligibility = {**raw.eligibility, 'document_validation_required': True, 'document_validation': review}
                    deadline = raw.deadline_at
                    if deadline and deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=KST)
                    passed = review['status'] == 'passed'
                    status = (PostingStatus.NEEDS_REVIEW if not passed or not deadline else
                              PostingStatus.CLOSED if deadline < now else PostingStatus.OPEN)
                    verified_keys.add(raw.content_key())
                    if raw.fetch_status != 'complete':
                        report['detail_failed' if raw.fetch_status == 'failed' else 'detail_partial'] += 1
                    if existing and (raw.fetch_status == 'failed' or any(issue['code'] in
                            ('source_unreadable_or_unresolved', 'original_documents_missing', 'no_readable_documents')
                            for issue in review['issues'])):
                        # Preserve last readable values while withholding them from recommendation.
                        record_revision(db, existing)
                        existing.eligibility = {**existing.eligibility, 'document_validation_required': True,
                                                'document_validation': review}
                        existing.status = PostingStatus.NEEDS_REVIEW
                        record_revision(db, existing)
                    else:
                        if existing and existing.content_hash == raw.content_hash() and existing.eligibility.get('form_review'):
                            raw.eligibility['form_review'] = existing.eligibility['form_review']
                        if upsert_posting(db, raw, status=status):
                            report['saved'] += 1
                    if passed:
                        from app.models.source_retry import SourceRetryJob
                        job = db.scalar(select(SourceRetryJob).where(SourceRetryJob.content_key == raw.content_key()))
                        if job:
                            job.state, job.error = 'done', None
                    if status == PostingStatus.NEEDS_REVIEW:
                        report['needs_review'] += 1
                    elif status == PostingStatus.CLOSED:
                        report['closed'] += 1
                    db.commit()
                    continue
            if raw.fetch_status != "complete":
                report["detail_failed" if raw.fetch_status == "failed" else "detail_partial"] += 1
                # 정상 스냅샷을 보존하되 재확인 중인 공고는 추천에서 제외한다.
                if existing:
                    report["needs_review"] += _change_status(db, [existing], PostingStatus.NEEDS_REVIEW)
                db.commit()
                continue
            verified_keys.add(raw.content_key())
            # 같은 본문은 LLM으로 다시 구조화하지 않는다. 파일 변경은 위 원본에 남는다.
            if (existing and existing.content_hash == raw.content_hash()
                    and existing.eligibility.get("extractor_version") == EXTRACTOR_VERSION
                    and not existing.eligibility.get("extraction_issues")):
                deadline = existing.deadline_at
                if deadline and deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=KST)
                status = (PostingStatus.CLOSED if deadline and deadline < now else
                          PostingStatus.OPEN if deadline else PostingStatus.NEEDS_REVIEW)
                changed = _change_status(db, [existing], status)
                if status == PostingStatus.CLOSED:
                    report["closed"] += changed
                elif status == PostingStatus.NEEDS_REVIEW:
                    report["needs_review"] += changed
                existing.crawled_at = now
                db.commit()
                continue
            _enrich_from_body(raw)

        policy_review = None
        if raw.source_platform == 'onjungchoungnyeon':
            from app.services.policy_validation import queue
            archive_attempt(db, raw)
            policy_review = queue(raw, existing)
        if raw.deadline_at is None and raw.deadline_raw and not _deadline_blocked(raw.eligibility.get('extraction_issues', [])):
            raw.deadline_at = normalize_deadline(raw.deadline_raw)

        deadline = raw.deadline_at
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=KST)
        status = (PostingStatus.CLOSED if deadline and deadline < now else
                  PostingStatus.NEEDS_REVIEW if (deadline is None and not crawler.full_sync) or raw.eligibility.get("extraction_issues") or
                  (policy_review is not None and policy_review['status'] != 'passed') else PostingStatus.OPEN)
        previous_status = existing.status if existing else None
        if upsert_posting(db, raw, status=status):
            report["saved"] += 1
        if status != previous_status:
            if status == PostingStatus.CLOSED:
                report["closed"] += 1
            elif status == PostingStatus.NEEDS_REVIEW:
                report["needs_review"] += 1
        db.commit()

    if crawler.full_sync or crawler.complete_listing:
        missing = list(db.scalars(select(Scholarship).where(
            Scholarship.source_platform == crawler.platform,
            Scholarship.content_key.not_in(listed_keys if crawler.full_sync else verified_keys),
            Scholarship.status.in_([PostingStatus.OPEN, PostingStatus.CLOSING_SOON]),
        )))
        report["missing_from_source"] = len(missing)
        status = PostingStatus.CLOSED if crawler.full_sync else PostingStatus.NEEDS_REVIEW
        changed = _change_status(db, missing, status)
        report["closed" if crawler.full_sync else "needs_review"] += changed

    db.commit()
    return report


def run_daily_update(db: Session, trigger: str = "scheduled") -> CrawlRun:
    """일간 갱신: 마감 상태 변경 -> 소스별 수집·대조 -> 실행 이력 기록."""
    # 크롤 도중 프로세스가 죽으면 기록이 RUNNING 으로 영원히 남는다. 정상 실행은
    # 몇 분이면 끝나므로 1시간 넘게 RUNNING 인 것은 죽은 실행으로 본다.
    db.execute(
        update(CrawlRun)
        .where(
            CrawlRun.status == CrawlRunStatus.RUNNING,
            CrawlRun.started_at < datetime.now(timezone.utc) - timedelta(hours=1),
        )
        .values(
            status=CrawlRunStatus.FAILED,
            error="실행 도중 중단됨(프로세스 종료)",
            finished_at=datetime.now(timezone.utc),
        )
    )
    run = CrawlRun(trigger=trigger, status=CrawlRunStatus.RUNNING)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        expired = purge_expired_postings(db)
        reports = [run_crawler(cls(), db) for cls in REGISTRY]
        if any(r['platform'] == 'onjungchoungnyeon' and not r.get('skipped') for r in reports):
            from app.services.policy_validation import run_batch
            for report in reports:
                if report['platform'] == 'onjungchoungnyeon':
                    report['policy_validation'] = run_batch(db, limit=settings.policy_validation_batch_size)

        run.total_expired = expired + sum(r["closed"] for r in reports)
        run.total_fetched = sum(r.get("fetched", 0) for r in reports)
        run.total_saved = sum(r.get("saved", 0) for r in reports)
        run.per_platform = reports
        run.finished_at = datetime.now(timezone.utc)
        run.status = (
            CrawlRunStatus.PARTIAL
            if any(r.get("skipped") or r.get("detail_failed") or r.get("detail_partial") or r.get("retry_deferred") or
                   (r.get('policy_validation') or {}).get('needs_review') or
                   (r.get('policy_validation') or {}).get('pending') for r in reports)
            else CrawlRunStatus.SUCCESS
        )
    except Exception as e:  # noqa: BLE001 - 실행 이력에 실패를 기록해야 함
        logger.exception("daily crawl failed")
        db.rollback()
        run.status = CrawlRunStatus.FAILED
        run.error = str(e)
        run.finished_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(run)
    return run
