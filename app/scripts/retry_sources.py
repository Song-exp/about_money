"""기존 목록 수집과 독립적으로 실행하는 재시도 큐 워커.

사용: python -m app.scripts.retry_sources
현재는 단일 워커로 실행한다. 큐 상태는 DB에 유지되므로 재실행해도 간격을 준수한다.
"""
import json

from app.core.db import SessionLocal
from app.crawlers.base import BaseCrawler
from app.crawlers.registry import REGISTRY, run_crawler


class RetryOnlyCrawler(BaseCrawler):
    archives_sources = True

    def __init__(self, delegate):
        self.delegate = delegate
        self.platform = delegate.platform
        self.source_type = delegate.source_type
        self.category = delegate.category

    def preflight(self, respect_robots=True):
        return self.delegate.preflight(respect_robots=respect_robots)

    def fetch(self):
        return []

    def fetch_detail(self, raw):
        return self.delegate.fetch_detail(raw)


def run_pending_sources(db):
    return [run_crawler(RetryOnlyCrawler(crawler()), db) for crawler in REGISTRY if crawler.archives_sources]


if __name__ == "__main__":
    with SessionLocal() as db:
        print(json.dumps(run_pending_sources(db), ensure_ascii=False))
