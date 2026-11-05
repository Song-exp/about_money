"""운영 관리 API: 수동 크롤 트리거 + 실행 이력 조회."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_admin
from app.crawlers.registry import run_daily_update
from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.models.scholarship_revision import ScholarshipRevision
from app.models.source_retry import SourceRetryJob
from app.models.scholarship import Scholarship, PostingStatus
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.services.scholarship_history import record_revision

router = APIRouter(
    prefix="/admin/crawl",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.post('/map-sources/sync')
def sync_map_sources(db: Session = Depends(get_db)):
    from app.services.map_sync import sync_sources
    return sync_sources(db, force=True)


@router.post('/map-sources/promotions/{source_key}')
def update_map_promotions(source_key: str, payload: dict, db: Session = Depends(get_db)):
    import json
    from app.core.config import settings
    from app.services.map_sync import push_promotions
    if len(json.dumps(payload).encode()) > settings.map_source_max_bytes:
        raise HTTPException(413, 'Feed too large')
    try:
        return push_promotions(db, source_key, payload)
    except ValueError:
        raise HTTPException(422, 'Invalid feed, unconfigured source, or source conflict')


class CrawlRunOut(BaseModel):
    id: int
    trigger: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    target_months: list = []
    total_fetched: int
    total_saved: int
    total_expired: int
    per_platform: list = []
    status: CrawlRunStatus
    error: str | None = None

    class Config:
        from_attributes = True


@router.post("/run", response_model=CrawlRunOut)
def trigger_run(db: Session = Depends(get_db)) -> CrawlRunOut:
    """일간 갱신(마감 상태 변경 + 수집·대조)을 수동 실행(동기)."""
    return CrawlRunOut.model_validate(run_daily_update(db, trigger="manual"))


@router.get("/runs", response_model=list[CrawlRunOut])
def list_runs(limit: int = 20, db: Session = Depends(get_db)) -> list[CrawlRunOut]:
    """최근 실행 이력 조회(최신순)."""
    rows = db.scalars(
        select(CrawlRun).order_by(CrawlRun.id.desc()).limit(limit)
    ).all()
    return [CrawlRunOut.model_validate(r) for r in rows]


@router.get("/source-attempts")
def list_source_attempts(
    platform: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    """원문 바이너리/본문을 제외한 시도별 수집 품질과 원본 식별자."""
    stmt = select(SourceFetchAttempt).order_by(SourceFetchAttempt.id.desc()).limit(limit)
    if platform:
        stmt = stmt.where(SourceFetchAttempt.source_platform == platform)
    return [{
        "id": row.id, "content_key": row.content_key, "source_url": row.source_url,
        "platform": row.source_platform, "title": row.title, "status": row.status,
        "error": row.error, "created_at": row.created_at,
        "documents": [{k: v for k, v in item.items() if k != "extracted_text"} for item in row.documents],
    } for row in db.scalars(stmt)]


@router.get("/source-documents/{document_id}")
def download_source_document(document_id: int, db: Session = Depends(get_db)) -> Response:
    """검토·재파싱용 원본. HTML도 실행하지 않고 첨부로 내려준다."""
    doc = db.get(SourceDocument, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="원본을 찾을 수 없습니다.")
    return Response(content=doc.content, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{doc.sha256}.bin"',
        "X-Content-Type-Options": "nosniff",
    })


@router.post("/source-attempts/{attempt_id}/reparse")
def reparse_source_attempt(attempt_id: int, db: Session = Depends(get_db)) -> dict:
    attempt = db.get(SourceFetchAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, "수집 기록을 찾을 수 없습니다.")
    from app.services.source_reparse import reparse_attempt
    return reparse_attempt(db, attempt)


@router.get("/retry-jobs")
def list_retry_jobs(
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(SourceRetryJob).order_by(SourceRetryJob.next_attempt_at, SourceRetryJob.id).limit(limit)
    if state:
        stmt = stmt.where(SourceRetryJob.state == state)
    return [{"id": job.id, "content_key": job.content_key, "platform": job.source_platform,
             "state": job.state, "failures": job.failures, "next_attempt_at": job.next_attempt_at,
             "error": job.error} for job in db.scalars(stmt)]


@router.post("/retry-jobs/{job_id}/retry", status_code=204)
def retry_source_job(job_id: int, db: Session = Depends(get_db)) -> Response:
    job = db.get(SourceRetryJob, job_id)
    if job is None:
        raise HTTPException(404, "재처리 작업을 찾을 수 없습니다.")
    job.state, job.failures, job.error = "pending", 0, None
    job.next_attempt_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


class FormReviewRequest(BaseModel):
    attempt_id: int
    fingerprint: str
    reviewed_all_documents: bool
    note: str = ""


def _review_context(db, scholarship_id):
    scholarship = db.get(Scholarship, scholarship_id)
    if scholarship is None:
        raise HTTPException(404, "장학금을 찾을 수 없습니다.")
    attempt = db.scalar(select(SourceFetchAttempt).where(
        SourceFetchAttempt.content_key == scholarship.content_key,
    ).order_by(SourceFetchAttempt.id.desc()).limit(1))
    return scholarship, attempt


@router.get("/scholarships/{scholarship_id}/review")
def scholarship_review(scholarship_id: int, db: Session = Depends(get_db)):
    scholarship, attempt = _review_context(db, scholarship_id)
    return {"scholarship_id": scholarship.id, "title": scholarship.title,
            "content_hash": scholarship.content_hash, "condition_rules": (scholarship.eligibility or {}).get('condition_rules'),
            "readiness": readiness(scholarship), "fingerprint": form_fingerprint(scholarship),
            "facts": (scholarship.eligibility or {}).get("fact_sources", []),
            "fields": (scholarship.eligibility or {}).get("application_fields", []),
            "form_groups": (scholarship.eligibility or {}).get("form_groups", []),
            "form_exclusions": (scholarship.eligibility or {}).get("form_exclusions", []),
            "coverage": (scholarship.eligibility or {}).get("form_coverage", []),
            "issues": (scholarship.eligibility or {}).get("extraction_issues", []),
            "attempt_id": attempt.id if attempt else None,
            "source_status": attempt.status if attempt else "missing",
            "documents": attempt.documents if attempt else []}


@router.post("/scholarships/{scholarship_id}/review")
def approve_scholarship_form(scholarship_id: int, req: FormReviewRequest, db: Session = Depends(get_db)):
    scholarship, attempt = _review_context(db, scholarship_id)
    eligibility = scholarship.eligibility or {}
    if not attempt or attempt.id != req.attempt_id or form_fingerprint(scholarship) != req.fingerprint:
        raise HTTPException(409, "원본 또는 양식이 변경되었습니다. 최신 자료를 다시 검토하세요.")
    if (not req.reviewed_all_documents or attempt.status != "complete" or
            not readiness(scholarship)["recommendation_ready"] or
            eligibility.get("validation_version") != VALIDATION_VERSION or
            "application_form_conflicts" in readiness(scholarship)["reasons"] or
            not eligibility.get("application_fields") or
            any(not f.get("source", {}).get("sha256") for f in eligibility["application_fields"])):
        raise HTTPException(409, "수집·추출 오류를 먼저 해결하고 모든 원본의 문항을 대조해야 합니다.")
    archived_hashes = {d.get("sha256") for d in attempt.documents if d.get("sha256")}
    if any(f["source"]["sha256"] not in archived_hashes for f in eligibility["application_fields"]):
        raise HTTPException(409, "문항의 원본이 최신 수집 자료와 일치하지 않습니다.")
    if any(g.get("source", {}).get("sha256") not in archived_hashes for g in eligibility.get("form_groups", [])):
        raise HTTPException(409, "공유 작성 규칙의 원본이 최신 수집 자료와 일치하지 않습니다.")
    record_revision(db, scholarship)
    scholarship.eligibility = {**eligibility, "form_review": {
        "fingerprint": req.fingerprint, "attempt_id": attempt.id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(), "note": req.note,
        "reviewed_all_documents": True}}
    record_revision(db, scholarship)
    db.commit()
    return readiness(scholarship)


@router.get("/quality")
def quality_report(db: Session = Depends(get_db)):
    from app.services.crawl_health import crawl_health
    from collections import Counter
    rows = db.scalars(select(Scholarship)).all()
    issues = Counter(issue for row in rows for issue in (row.eligibility or {}).get("extraction_issues", []))
    states = [readiness(row) for row in rows]
    attempts = _latest_source_attempts(db)
    source_issues = Counter()
    for attempt in attempts:
        if attempt.error:
            source_issues[attempt.error] += 1
        for document in attempt.documents:
            if document.get("error"):
                source_issues[document["error"]] += 1
            for issue in document.get("structured", {}).get("issues", []):
                source_issues[issue] += 1
            for issue in document.get("issues", []):
                source_issues[issue["error"] if isinstance(issue, dict) else issue] += 1
    return {"total": len(rows), "recommendation_ready": sum(s["recommendation_ready"] for s in states),
            "writing_ready": sum(s["writing_ready"] for s in states), "issues": dict(issues),
            "source_statuses": dict(Counter(a.status for a in attempts)), "source_issues": dict(source_issues),
            "accuracy_measured": False, "freshness": crawl_health(db)}


@router.post('/scholarships/{scholarship_id}/condition-rules')
def review_condition_rules(scholarship_id: int, payload: dict, db: Session = Depends(get_db)):
    from app.services.eligibility_rules import review_tree
    scholarship, attempt = _review_context(db, scholarship_id)
    if not attempt or payload.get('attempt_id') != attempt.id or payload.get('content_hash') != scholarship.content_hash:
        raise HTTPException(409, '최신 공고와 수집 시도를 다시 확인하세요.')
    try:
        rules = review_tree(payload.get('tree'), attempt.documents, scholarship.content_hash, payload.get('reviewed_logic'))
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    rules.update(attempt_id=attempt.id, reviewed_at=datetime.now(timezone.utc).isoformat())
    record_revision(db, scholarship)
    scholarship.eligibility = {**scholarship.eligibility, 'condition_rules': rules}
    record_revision(db, scholarship)
    db.commit()
    return {'condition_rules': rules, 'writing_sessions_require_reconfirmation': True}


def _latest_source_attempts(db):
    latest_ids = select(func.max(SourceFetchAttempt.id)).group_by(SourceFetchAttempt.content_key)
    return db.scalars(select(SourceFetchAttempt).where(SourceFetchAttempt.id.in_(latest_ids))
                      .order_by(SourceFetchAttempt.id.desc())).all()


@router.get("/review-queue")
def review_queue(limit: int = Query(default=50, ge=1, le=100), db: Session = Depends(get_db)):
    scholarships = {row.content_key: row for row in db.scalars(select(Scholarship))}
    items = []
    for attempt in _latest_source_attempts(db):
        scholarship = scholarships.get(attempt.content_key)
        if scholarship and scholarship.status == PostingStatus.CLOSED and attempt.status == "complete":
            continue
        state = readiness(scholarship) if scholarship else None
        policy = bool(scholarship and scholarship.source_platform == 'onjungchoungnyeon')
        review = (scholarship.eligibility or {}).get('policy_validation') if policy else None
        needs_review = (not review or review.get('status') != 'passed' or not state['recommendation_ready']) if policy else (not state or not state['writing_ready'])
        if attempt.status != "complete" or not scholarship or needs_review:
            items.append({"attempt_id": attempt.id, "scholarship_id": scholarship.id if scholarship else None,
                          "title": attempt.title, "platform": attempt.source_platform,
                          "source_status": attempt.status, "error": attempt.error, "readiness": state,
                          "document_validation": (scholarship.eligibility or {}).get('document_validation') if scholarship else None})
            items[-1]['policy_validation'] = review
            items[-1]['consultation'] = 'youth_policy' if policy else 'scholarship'
        if len(items) >= limit:
            break
    return items


@router.get("/source-attempts/{attempt_id}/review")
def source_attempt_review(attempt_id: int, db: Session = Depends(get_db)):
    attempt = db.get(SourceFetchAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, "수집 기록을 찾을 수 없습니다.")
    results = [d["structured"] for d in attempt.documents if d.get("structured")]
    posting = db.scalar(select(Scholarship).where(Scholarship.content_key == attempt.content_key))
    return {"scholarship_id": None, "title": attempt.title, "fingerprint": None,
            "document_validation": (posting.eligibility or {}).get('document_validation') if posting else None,
            "policy_validation": (posting.eligibility or {}).get('policy_validation') if posting else None,
            "readiness": {"recommendation_ready": False, "writing_ready": False},
            "attempt_id": attempt.id, "source_status": attempt.status, "documents": attempt.documents,
            "facts": [f for r in results for f in r.get("facts", [])],
            "fields": [f for r in results for f in r.get("application_fields", [])],
            "coverage": [r.get("coverage", {}) for r in results],
            "issues": [i for r in results for i in r.get("issues", [])]}


# --- 공고 버전 조회 ---
@router.get("/scholarships/{scholarship_id}/revisions")
def list_scholarship_revisions(
    scholarship_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(select(ScholarshipRevision).where(
        ScholarshipRevision.scholarship_id == scholarship_id,
    ).order_by(ScholarshipRevision.id.desc()).limit(limit))
    return [{"id": row.id, "scholarship_id": row.scholarship_id, "sha256": row.sha256,
             "created_at": row.created_at, "snapshot": row.snapshot} for row in rows]


# --- 끝난 혜택 제보 확인 ---
# 별도 라우터를 두는 이유는 prefix 가 /admin/crawl 이 아니기 때문이다.
offer_report_router = APIRouter(
    prefix="/admin/offer-reports",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


class OfferReportRow(BaseModel):
    offer_id: int
    store_id: int
    store_label: str
    pay_method: str
    discount_rate: int
    report_count: int
    is_active: bool
    flagged: bool


@offer_report_router.get("", response_model=list[OfferReportRow])
def list_offer_reports(
    min_count: int = 1,
    db: Session = Depends(get_db),
) -> list[OfferReportRow]:
    """제보가 쌓인 혜택 목록. 많이 제보된 순.

    자동 비활성을 하지 않기로 했으므로 이 목록이 사람의 판단 지점이다.
    확인 후 store_offers.is_active 를 내린다.
    """
    from sqlalchemy import func

    from app.core.config import settings
    from app.models.community import Report
    from app.models.store import Store, StoreOffer

    rows = db.execute(
        select(StoreOffer, Store, func.count(Report.id).label("cnt"))
        .join(Report, Report.store_offer_id == StoreOffer.id)
        .join(Store, Store.id == StoreOffer.store_id)
        .group_by(StoreOffer.id, Store.id)
        .having(func.count(Report.id) >= min_count)
        .order_by(func.count(Report.id).desc())
    ).all()

    threshold = settings.store_offer_report_threshold
    return [
        OfferReportRow(
            offer_id=offer.id,
            store_id=store.id,
            store_label=f"{store.brand} {store.branch}",
            pay_method=offer.pay_method.value,
            discount_rate=offer.discount_rate,
            report_count=cnt,
            is_active=offer.is_active,
            flagged=threshold > 0 and cnt >= threshold,
        )
        for offer, store, cnt in rows
    ]


# --- 커뮤니티 신고 처리 ---
# 신고는 쌓이기만 하고 볼 방법이 없었다. 익명 게시판에서는 이 목록과 삭제
# 권한이 운영자의 유일한 개입 수단이다. 자동 숨김은 하지 않는다. 몇 명이
# 몰려 신고하면 정상 글이 사라지기 때문에 마지막 판단은 사람이 한다.
moderation_router = APIRouter(
    prefix="/admin/moderation",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


class ReportedItem(BaseModel):
    kind: str  # "post" | "comment"
    id: int
    post_id: int
    title: str | None = None
    excerpt: str
    report_count: int
    reasons: list[str]
    created_at: datetime | None = None


@moderation_router.get("/reports", response_model=list[ReportedItem])
def list_reported(min_count: int = 1, db: Session = Depends(get_db)) -> list[ReportedItem]:
    """신고가 쌓인 글·댓글. 많이 신고된 순. 이미 삭제된 것은 빼고 보여준다."""
    from sqlalchemy import func

    from app.models.community import Comment, Post, Report

    items: list[ReportedItem] = []
    for model, fk, kind in ((Post, Report.post_id, "post"), (Comment, Report.comment_id, "comment")):
        rows = db.execute(
            select(model, func.count(Report.id))
            .join(Report, fk == model.id)
            .where(model.deleted_at.is_(None))
            .group_by(model.id)
            .having(func.count(Report.id) >= min_count)
        ).all()
        for row, cnt in rows:
            reasons = db.scalars(select(Report.reason).where(fk == row.id)).all()
            items.append(
                ReportedItem(
                    kind=kind,
                    id=row.id,
                    post_id=row.id if kind == "post" else row.post_id,
                    title=getattr(row, "title", None),
                    excerpt=(row.body or "")[:200],
                    report_count=cnt,
                    reasons=list(reasons),
                    created_at=row.created_at,
                )
            )
    items.sort(key=lambda i: i.report_count, reverse=True)
    return items


@moderation_router.delete("/posts/{post_id}", status_code=204, response_class=Response)
def remove_post(post_id: int, db: Session = Depends(get_db)) -> Response:
    """글 삭제. 작성자 본인 삭제와 같은 소프트 삭제라 댓글 흐름은 남는다."""
    from datetime import timezone

    from fastapi import HTTPException

    from app.api.community import _image_path
    from app.models.community import Post

    post = db.get(Post, post_id)
    if post is None or post.deleted_at is not None:
        raise HTTPException(404, "글을 찾을 수 없습니다.")
    post.deleted_at = datetime.now(timezone.utc)
    names, post.image_names = post.image_names or [], []
    db.commit()
    for name in names:
        _image_path(name).unlink(missing_ok=True)
    return Response(status_code=204)


@moderation_router.delete("/comments/{comment_id}", status_code=204, response_class=Response)
def remove_comment(comment_id: int, db: Session = Depends(get_db)) -> Response:
    from datetime import timezone

    from fastapi import HTTPException

    from app.models.community import Comment, Post

    comment = db.get(Comment, comment_id)
    if comment is None or comment.deleted_at is not None:
        raise HTTPException(404, "댓글을 찾을 수 없습니다.")
    comment.deleted_at = datetime.now(timezone.utc)
    post = db.get(Post, comment.post_id)
    if post and post.comment_count > 0:
        post.comment_count -= 1
    db.commit()
    return Response(status_code=204)


@moderation_router.delete("/{kind}/{item_id}/reports", status_code=204, response_class=Response)
def dismiss_reports(kind: str, item_id: int, db: Session = Depends(get_db)) -> Response:
    """문제없는 글이면 신고를 기각한다. 작성자 등급 강등도 함께 풀린다."""
    from fastapi import HTTPException
    from sqlalchemy import delete

    from app.models.community import Report

    column = {"posts": Report.post_id, "comments": Report.comment_id}.get(kind)
    if column is None:
        raise HTTPException(404, "posts 또는 comments 만 가능합니다.")
    db.execute(delete(Report).where(column == item_id))
    db.commit()
    return Response(status_code=204)
