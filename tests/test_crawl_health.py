from datetime import datetime, timedelta, timezone

from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.source_retry import SourceRetryJob
from app.services.crawl_health import crawl_health


def test_freshness_two_runs_staleness_and_due_queue(db):
    now = datetime.now(timezone.utc)
    assert not crawl_health(db, now)['collection_within_24h']
    for delta in (30, 10):
        db.add(CrawlRun(trigger='manual', status=CrawlRunStatus.SUCCESS, started_at=now-timedelta(minutes=delta),
                        finished_at=now-timedelta(minutes=delta-1), total_fetched=2, total_saved=1))
    db.add(SourceRetryJob(content_key='x', source_platform='test', state='pending', next_attempt_at=now-timedelta(minutes=1)))
    db.commit()
    health = crawl_health(db, now)
    assert health['two_latest_runs_successful'] and health['collection_within_24h'] and health['retry_due'] == 1
    assert not crawl_health(db, now+timedelta(days=2))['collection_within_24h']
    db.add(CrawlRun(trigger='manual', status=CrawlRunStatus.PARTIAL, started_at=now, finished_at=now))
    db.commit()
    assert not crawl_health(db, now)['two_latest_runs_successful']


def test_retry_worker_and_daily_share_process_lock(monkeypatch):
    from app.services import scheduler
    called = []
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: called.append(True))
    scheduler._crawl_lock.acquire()
    try:
        scheduler._retry_job()
        scheduler._scheduled_job()
    finally:
        scheduler._crawl_lock.release()
    assert called == []


def test_daily_db_open_failure_releases_worker_lock(monkeypatch):
    from app.services import scheduler
    def fail():
        raise RuntimeError('test database unavailable')
    monkeypatch.setattr(scheduler, 'SessionLocal', fail)
    scheduler._scheduled_job()
    assert scheduler._crawl_lock.acquire(blocking=False)
    scheduler._crawl_lock.release()
