"""구조화 공고의 버전 보관. 원문 다운로드 기록과 별도로 유지한다."""
import hashlib
import json
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scholarship import Scholarship
from app.models.scholarship_revision import ScholarshipRevision


def record_revision(db: Session, scholarship: Scholarship) -> ScholarshipRevision:
    db.flush()
    def iso(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(hours=9)))
        return value.astimezone(timezone.utc).isoformat()

    snapshot = {
        "title": scholarship.title, "organization": scholarship.organization,
        "source_url": scholarship.source_url, "source_platform": scholarship.source_platform,
        "category": scholarship.category.value, "source_type": scholarship.source_type.value,
        "deadline_at": iso(scholarship.deadline_at),
        "posted_at": iso(scholarship.posted_at),
        "eligibility": scholarship.eligibility or {}, "benefit": scholarship.benefit or {},
        "required_documents": scholarship.required_documents or [],
        "body_text": scholarship.body_text or "", "status": scholarship.status.value,
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    revision = db.scalar(select(ScholarshipRevision).where(
        ScholarshipRevision.scholarship_id == scholarship.id, ScholarshipRevision.sha256 == digest,
    ))
    if revision is None:
        revision = ScholarshipRevision(scholarship_id=scholarship.id, sha256=digest, snapshot=json.loads(canonical))
        db.add(revision)
        db.flush()
    return revision
