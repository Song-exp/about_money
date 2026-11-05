"""Read-only collection freshness; repeated successful runs are observable evidence."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func

from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.source_retry import SourceRetryJob
from app.models.source_document import SourceFetchAttempt


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def crawl_health(db, now=None):
    now = now or datetime.now(timezone.utc)
    runs = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(10)).all()
    latest = select(func.max(SourceFetchAttempt.id)).group_by(SourceFetchAttempt.content_key)
    attempts = db.scalars(select(SourceFetchAttempt).where(SourceFetchAttempt.id.in_(latest))).all()
    jobs = db.scalars(select(SourceRetryJob)).all()
    success = next((r for r in runs if r.status == CrawlRunStatus.SUCCESS), None)
    age = (now - utc(success.finished_at)).total_seconds() if success and success.finished_at else None
    return {'observed_at': now.isoformat(), 'last_success_at': utc(success.finished_at).isoformat() if success and success.finished_at else None,
            'success_age_seconds': age, 'collection_within_24h': age is not None and 0 <= age <= 86400,
            'two_latest_runs_successful': len(runs) >= 2 and all(r.status == CrawlRunStatus.SUCCESS for r in runs[:2]),
            'latest_runs': [{'id': r.id, 'trigger': r.trigger, 'status': r.status.value,
                             'started_at': utc(r.started_at).isoformat(), 'finished_at': utc(r.finished_at).isoformat() if r.finished_at else None,
                             'fetched': r.total_fetched, 'saved': r.total_saved} for r in runs],
            'latest_source_statuses': dict(Counter(a.status for a in attempts)),
            'retry_states': dict(Counter(j.state for j in jobs)),
            'retry_due': sum(j.state == 'pending' and utc(j.next_attempt_at) <= now for j in jobs),
            'accuracy_measured': False, 'target_detection_delay_seconds': 86400}
