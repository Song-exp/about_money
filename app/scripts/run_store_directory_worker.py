"""Run only the official-store scheduler in a dedicated local worker process."""
import logging
from threading import Event

from app.core.config import settings
from app.services.scheduler import start_scheduler, shutdown_scheduler


def main():
    if not settings.store_collection_enabled:
        raise RuntimeError('store_collection_disabled')
    # These changes are process-local. FastAPI/other workers retain their settings.
    settings.crawl_schedule_enabled = False
    settings.map_sync_enabled = False
    settings.card_collection_enabled = False
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    start_scheduler()
    print('Official store worker started; due checks run every '
        + str(settings.store_collection_interval_sec) + ' seconds.', flush=True)
    try:
        stop = Event()
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_scheduler()


if __name__ == '__main__':
    main()
