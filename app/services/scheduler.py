"""일간 파이프라인 스케줄러(APScheduler).

매일 00:10 KST(날짜가 바뀐 직후)에 run_daily_update를 실행한다:
마감 상태 변경 -> 공고 수집·대조. FastAPI lifespan에서 start/shutdown 한다.
설정으로 on/off.
"""
from __future__ import annotations

import logging
from threading import Lock
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.db import SessionLocal
from app.crawlers.registry import run_daily_update
from app.services.reminders import send_deadline_reminders

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_JOB_ID = "daily_crawl_update"
_REMINDER_JOB_ID = "daily_deadline_reminder"

_scheduler: BackgroundScheduler | None = None
_crawl_lock = Lock()


def _scheduled_job() -> None:
    """스케줄러가 호출하는 작업. 자체 DB 세션을 열고 닫는다."""
    if not _crawl_lock.acquire(blocking=False):
        logger.info('daily crawl deferred: retry worker running')
        return
    db = None
    try:
        db = SessionLocal()
        run = run_daily_update(db, trigger="scheduled")
        logger.info(
            "daily crawl done: run_id=%s status=%s saved=%s closed=%s",
            run.id, run.status, run.total_saved, run.total_expired,
        )
        # 만료·폐기된 세션 정리. 검증이 만료를 확인하므로 기능상 필수는
        # 아니지만, 두면 auth_sessions 가 무한정 자란다.
        from app.core.security import purge_expired_sessions

        purged = purge_expired_sessions(db)
        logger.info("expired sessions purged: %s", purged)
    except Exception:  # noqa: BLE001
        logger.exception("scheduled daily crawl failed")
    finally:
        if db is not None:
            db.close()
        _crawl_lock.release()


def _retry_job():
    if not _crawl_lock.acquire(blocking=False):
        return
    try:
        from app.scripts.retry_sources import run_pending_sources
        with SessionLocal() as db:
            reports = run_pending_sources(db)
            logger.info('source retry worker finished: platforms=%s', len(reports))
    except Exception:
        logger.exception('source retry worker failed')
    finally:
        _crawl_lock.release()


def _policy_validation_job():
    if not _crawl_lock.acquire(blocking=False):
        return
    try:
        from app.services.policy_validation import run_batch
        with SessionLocal() as db:
            report = run_batch(db, limit=settings.policy_validation_batch_size)
            logger.info('policy validation batch: processed=%s passed=%s review=%s',
                        report['processed'], report['passed'], report['needs_review'])
    except Exception:
        logger.exception('policy validation batch failed')
    finally:
        _crawl_lock.release()


def start_scheduler() -> BackgroundScheduler | None:
    """스케줄러 시작. 비활성화 설정이면 None 반환."""
    global _scheduler
    if not any((settings.crawl_schedule_enabled, settings.map_sync_enabled, settings.card_collection_enabled,
                settings.store_collection_enabled)):
        logger.info("crawl scheduler disabled by settings")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=KST)
    trigger = CronTrigger(
        hour=settings.crawl_schedule_hour,
        minute=settings.crawl_schedule_minute,
        timezone=KST,
    )
    if settings.crawl_schedule_enabled:
        _scheduler.add_job(
            _scheduled_job,
            trigger=trigger,
            id=_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
        )
    if settings.crawl_schedule_enabled and settings.crawl_retry_schedule_enabled:
        _scheduler.add_job(_retry_job, trigger='interval', seconds=max(30, settings.crawl_retry_schedule_interval_sec),
                           id='source_retry_worker', replace_existing=True, max_instances=1, coalesce=True)
    if settings.crawl_schedule_enabled:
        _scheduler.add_job(_policy_validation_job, trigger='interval',
                           seconds=max(30, settings.policy_validation_interval_sec),
                           id='policy_validation_worker', replace_existing=True, max_instances=1, coalesce=True)
    if settings.crawl_schedule_enabled and settings.deadline_reminder_enabled:
        _scheduler.add_job(
            _reminder_job,
            trigger=CronTrigger(
                hour=settings.deadline_reminder_hour, minute=0, timezone=KST
            ),
            id=_REMINDER_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )

    if settings.map_sync_enabled:
        _scheduler.add_job(_map_sync_job, trigger='interval', seconds=max(60, settings.map_sync_interval_sec),
                           id='map_source_sync', replace_existing=True, max_instances=1, coalesce=True,
                           next_run_time=datetime.now(KST), misfire_grace_time=300)
    if settings.card_collection_enabled:
        _scheduler.add_job(_card_collection_job, trigger='interval', seconds=settings.card_collection_interval_sec,
                           id='card_source_collection', replace_existing=True, max_instances=1, coalesce=True,
                           next_run_time=datetime.now(KST), misfire_grace_time=300)
    if settings.store_collection_enabled:
        _scheduler.add_job(_store_collection_job, trigger='interval', seconds=settings.store_collection_interval_sec,
                           id='store_directory_collection', replace_existing=True, max_instances=1, coalesce=True,
                           next_run_time=datetime.now(KST), misfire_grace_time=300)
    _scheduler.start()
    logger.info('scheduler started: %s', [(job.id, str(job.trigger)) for job in _scheduler.get_jobs()])
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("crawl scheduler stopped")


def _map_sync_job():
    from app.services.map_sync import sync_sources
    try:
        with SessionLocal() as db:
            logger.info('map pipeline: %s', sync_sources(db, trigger='scheduled'))
    except Exception:
        logger.exception('map source sync failed')
    finally:
        # Both interval jobs can start together. Hand off after releasing the map
        # lease so the card job cannot starve on every identical five-minute tick.
        if settings.card_collection_enabled:
            _card_collection_job()


def _card_collection_job():
    from app.services.card_collection import collect_due
    try:
        with SessionLocal() as db:
            logger.info('card collection: %s', collect_due(db, trigger='scheduled',
                max_sources=settings.card_collection_max_sources))
    except Exception:
        logger.exception('card source collection failed')


def _store_collection_job():
    from app.services.store_directory_collection import collect_store_directories
    try:
        with SessionLocal() as db:
            logger.info('store directory collection: %s', collect_store_directories(db, trigger='scheduled'))
    except Exception:
        logger.exception('store directory collection failed')
    # Separate session: a collection failure cannot roll back place-check monitoring.
    from app.services.store_place_scheduler import run_due
    try:
        with SessionLocal() as db:
            logger.info('store place revalidation: %s', run_due(db))
    except Exception:
        logger.exception('store place revalidation failed')


def _reminder_job() -> None:
    """마감 임박 공고를 메일로 알린다(매일 09:00 KST).

    부품이 이미 다 있어서 잇기만 하면 되는 작업이다 —
    deadline_at(공고) + smtplib(mailer) + APScheduler(여기).
    """
    db = SessionLocal()
    try:
        sent = send_deadline_reminders(db)
        logger.info("deadline reminders sent: %s", sent)
    except Exception:  # noqa: BLE001
        logger.exception("deadline reminder job failed")
    finally:
        db.close()
