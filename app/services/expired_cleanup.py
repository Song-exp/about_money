"""명시적인 마감 공고 삭제. 개인 작성 이력과 원본은 삭제하지 않는다."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select, update
from app.models.application import UserApplication
from app.models.scholarship import Scholarship, ScholarshipEmbedding, PostingStatus
from app.models.scholarship_revision import ScholarshipRevision
from app.models.writing_session import WritingSession
from app.models.source_retry import SourceRetryJob

KST = timezone(timedelta(hours=9))


def remove_expired(db, now=None, verified_deadlines=None):
    now = now or datetime.now(KST)
    now = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    for item in verified_deadlines or []:
        row = db.get(Scholarship, item["id"])
        if row is None or row.source_url != item["source_url"]:
            raise ValueError("verified deadline does not match current posting")
        deadline = datetime.fromisoformat(item["deadline_at"])
        if deadline.tzinfo is None or deadline >= now:
            raise ValueError("verified deadline must be timezone-aware and expired")
        row.deadline_at = deadline.astimezone(KST)
    db.flush()
    rows = db.scalars(select(Scholarship).where(Scholarship.deadline_at.is_not(None), Scholarship.deadline_at < now)).all()
    report = {"cutoff": now.isoformat(), "deleted": [], "retained_writing_history": [], "detached_applications": 0}
    for row in rows:
        item = {"id": row.id, "title": row.title, "source_url": row.source_url, "deadline_at": row.deadline_at.isoformat()}
        if db.scalar(select(WritingSession.id).where(WritingSession.scholarship_id == row.id).limit(1)):
            row.status = PostingStatus.CLOSED
            report["retained_writing_history"].append(item)
            continue
        # FK 활성화 여부와 관계없이 이력의 원래 이름/문서·원본을 보존한다.
        detached = db.execute(update(UserApplication).where(UserApplication.scholarship_id == row.id).values(scholarship_id=None))
        report["detached_applications"] += detached.rowcount
        db.execute(delete(ScholarshipEmbedding).where(ScholarshipEmbedding.scholarship_id == row.id))
        db.execute(delete(ScholarshipRevision).where(ScholarshipRevision.scholarship_id == row.id))
        db.execute(update(SourceRetryJob).where(SourceRetryJob.content_key == row.content_key).values(state="done"))
        db.delete(row)
        report["deleted"].append(item)
    db.commit()
    return report
