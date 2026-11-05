"""Run the daily pipeline once with a dated board window and a local backup."""
import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time

import httpx
from sqlalchemy import select, func

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.crawlers import registry
from app.crawlers.public import KhuScholarshipCrawler, OnjungchoungnyeonCrawler, KST
from app.crawlers.base import RawPosting
from app.models.scholarship import Scholarship
from app.services.consultation_scope import scope_clause


class WindowedScholarships(KhuScholarshipCrawler):
    # An incremental listing must never close omitted historical notices.
    complete_listing = False
    since = None
    retained = []

    def fetch(self):
        selected, seen = [], set()
        self.last_listing_pages = 0
        with httpx.Client(timeout=25, follow_redirects=True, headers={'User-Agent': self.user_agent}) as client:
            for page in range(1, settings.crawl_board_max_pages + 1):
                self.last_listing_pages = page
                response = client.get(f'{self.base_url}{self.bbs_path}/list.do',
                                      params={'menuNo': self.menu_no, 'pageIndex': page})
                response.raise_for_status()
                rows, finished = self.parse_listing_page(response.content, seen)
                selected.extend(r for r in rows if r.posted_at is None or r.posted_at >= self.since)
                if finished or rows and all(r.posted_at and r.posted_at < self.since for r in rows):
                    break
                time.sleep(settings.crawl_default_delay_sec)
            else:
                raise RuntimeError('board date window not completed before pagination limit')
        urls = {r.source_url for r in selected}
        selected.extend(r for r in self.retained if r.source_url not in urls)
        return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True, help='KST board posting date, inclusive YYYY-MM-DD')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if engine.dialect.name != 'sqlite':
        raise ValueError('this one-off runner requires local SQLite for backup')
    args.output.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=args.output / 'pipeline.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s', encoding='utf-8')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    backup = Path('data/backups') / ('before-live-collection-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(engine.url.database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    WindowedScholarships.since = datetime.fromisoformat(args.since).replace(tzinfo=KST)
    with SessionLocal() as db:
        rows = db.scalars(select(Scholarship).where(scope_clause('scholarship'))).all()
        WindowedScholarships.retained = [RawPosting(title=r.title, source_url=r.source_url,
            source_platform=r.source_platform, category=r.category, source_type=r.source_type,
            posted_at=r.posted_at, deadline_at=r.deadline_at, organization=r.organization,
            eligibility={k: v for k, v in (r.eligibility or {}).items() if k in ('school', 'scope')}) for r in rows]
        before = {'total': db.scalar(select(func.count()).select_from(Scholarship)), 'scholarships': len(rows)}
        plan = {'started_at': datetime.now(timezone.utc).isoformat(), 'backup': str(backup), 'since': args.since,
                'retained_ids': [r.id for r in rows], 'before': before, 'board_listing_complete': False}
        (args.output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        registry.REGISTRY = [OnjungchoungnyeonCrawler, WindowedScholarships]
        run = registry.run_daily_update(db, trigger='manual')
        summary = {**plan, 'run_id': run.id, 'status': run.status.value,
                   'finished_at': run.finished_at.isoformat() if run.finished_at else None,
                   'fetched': run.total_fetched, 'saved_new': run.total_saved,
                   'closed': run.total_expired, 'platforms': run.per_platform, 'error': run.error,
                   'counts': [{'source': a, 'status': b.value, 'count': c} for a, b, c in db.execute(
                       select(Scholarship.source_platform, Scholarship.status, func.count())
                       .group_by(Scholarship.source_platform, Scholarship.status))]}
        (args.output / 'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
