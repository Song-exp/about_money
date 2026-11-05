"""Capture a bounded release corpus and replay it through the real DB pipeline.

Public source files only. Human gold and release approval are never synthesized.
"""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from sqlalchemy import select, func
from app.core.db import SessionLocal, engine
from app.crawlers.base import BaseCrawler, RawPosting
from app.crawlers.public import KhuScholarshipCrawler
from app.crawlers.registry import run_crawler
from app.models.scholarship import Scholarship, SourceType, Category
from app.models.application import UserApplication, ApplicationDocument
from app.services.expired_cleanup import remove_expired
from app.scripts.measure_scholarships import reference_listing


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def capture(root, since, limit, offset=0):
    crawler = KhuScholarshipCrawler()
    crawler.preflight()
    rows = reference_listing(root, since)
    rows = sorted(rows, key=lambda r: (r['posted_at'], r['board_id']), reverse=True)[offset:offset+limit]
    write(root / 'selected.json', {'rows': rows, 'target_notices': limit, 'offset': offset, 'selection': 'latest_in_date_window_no_accuracy_claim'})
    summary = []
    for i, row in enumerate(rows):
        folder = root / ('notice-' + row['board_id'])
        folder.mkdir(exist_ok=True)
        if (folder / 'capture.json').exists():
            saved = read(folder / 'capture.json')
        else:
            raw = RawPosting(title=row['title'], source_url=row['source_url'], source_platform=crawler.platform,
                             source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
                             posted_at=datetime.fromisoformat(row['posted_at']).replace(tzinfo=timezone.utc))
            try:
                crawler.fetch_detail(raw)
                docs = []
                for j, d in enumerate(raw.source_documents):
                    item = {k: v for k, v in d.items() if k != 'content'}
                    if 'content' in d:
                        digest = hashlib.sha256(d['content']).hexdigest()
                        local = f'asset-{j}-{digest[:12]}.bin'
                        (folder / local).write_bytes(d['content'])
                        item.update(sha256=digest, local_file=local)
                    docs.append(item)
                saved = {'listing': row, 'fetch_status': raw.fetch_status, 'fetch_error': raw.fetch_error, 'documents': docs}
            except Exception as exc:
                saved = {'listing': row, 'fetch_status': 'failed', 'fetch_error': type(exc).__name__, 'documents': []}
            write(folder / 'capture.json', saved)
        summary.append({'board_id': row['board_id'], 'status': saved['fetch_status'], 'documents': len(saved['documents']), 'error': saved.get('fetch_error')})
        print(f"capture {i+1}/{len(rows)} id={row['board_id']} status={saved['fetch_status']}", flush=True)
    assets = [d for row in rows for d in read(root / ('notice-' + row['board_id']) / 'capture.json')['documents']]
    formats = sorted({d.get('kind') for d in assets if d.get('kind')})
    form_candidates = {d.get('sha256') for d in assets if any(w in d.get('filename', '') for w in ('양식', '서식', '신청서', '지원서')) and d.get('sha256')}
    write(root / 'capture-summary.json', {'notices': len(rows), 'target_notices': limit, 'results': summary,
          'formats': formats, 'distinct_form_source_candidates': len(form_candidates), 'verified_whole_forms': 0,
          'target_forms': 20, 'human_gold_complete': False, 'database_written': False})


def sync(root, cycles):
    if engine.url.get_backend_name() != 'sqlite':
        raise ValueError('this backup/apply command requires a local SQLite database')
    source = Path(engine.url.database).resolve()
    backup_root = Path('data/backups').resolve()
    backup_root.mkdir(exist_ok=True)
    backup = backup_root / ('app.pre-completion-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.db')
    if backup.exists():
        raise ValueError('backup already exists')
    # Opening an existing database in rw mode lets SQLite recover a hot journal
    # left by an interrupted writer before taking a consistent backup.
    with sqlite3.connect(source.as_uri() + '?mode=rw', uri=True) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    rows = read(root / 'selected.json')['rows']
    summary = {'backup': str(backup), 'cycles': [], 'input_scope': 'saved_live_window_not_complete_global_listing'}
    with SessionLocal() as db:
        before = {name: db.scalar(select(func.count()).select_from(model)) for name, model in
                  [('applications', UserApplication), ('user_documents', ApplicationDocument)]}
        for i in range(cycles):
            class CapturedCrawler(BaseCrawler):
                platform = 'khu_janghak'
                archives_sources = True
                source_type = SourceType.PUBLIC
                category = Category.SCHOLARSHIP
                complete_listing = False
                include_pending_attempts = False

                def preflight(self, respect_robots=True):
                    pass

                def fetch(self):
                    return [RawPosting(title=r['title'], source_url=r['source_url'], source_platform=self.platform,
                                       source_type=self.source_type, category=self.category,
                                       posted_at=datetime.fromisoformat(r['posted_at']).replace(tzinfo=timezone.utc)) for r in rows]

                def fetch_detail(self, raw):
                    print('db processing: ' + raw.title, flush=True)
                    row = next(r for r in rows if r['source_url'] == raw.source_url)
                    folder = root / ('notice-' + row['board_id'])
                    saved = read(folder / 'capture.json')
                    raw.source_documents = []
                    for d in saved['documents']:
                        item = dict(d)
                        if d.get('local_file'):
                            content = (folder / d['local_file']).read_bytes()
                            if hashlib.sha256(content).hexdigest() != d['sha256']:
                                raise ValueError('source checksum mismatch')
                            item['content'] = content
                        raw.source_documents.append(item)
                    raw.fetch_status, raw.fetch_error = saved['fetch_status'], saved.get('fetch_error')
                    raw.body_text = '\n'.join(d.get('extracted_text', '') for d in raw.source_documents)

            report = run_crawler(CapturedCrawler(), db)
            summary['cycles'].append(report)
            write(root / 'db-sync.json', summary)
            print(f"db cycle={i+1} saved={report['saved']} partial={report['detail_partial']} failed={report['detail_failed']}", flush=True)
        from app.models.source_document import SourceFetchAttempt
        for row in rows:
            attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.source_url == row['source_url']).order_by(SourceFetchAttempt.id.desc()).limit(1))
            if attempt:
                fields = [f for d in attempt.documents for f in d.get('structured', {}).get('application_fields', [])]
                write(root / ('notice-' + row['board_id']) / 'validated-documents.json',
                      {'attempt_id': attempt.id, 'complete': attempt.status == 'complete', 'fields': fields,
                       'issues': [x for d in attempt.documents for x in d.get('structured', {}).get('issues', [])]})
        cleanup = remove_expired(db)
        write(root / 'expired-cleanup.json', cleanup)
        after = {name: db.scalar(select(func.count()).select_from(model)) for name, model in
                 [('applications', UserApplication), ('user_documents', ApplicationDocument)]}
        if before != after:
            raise ValueError('user history count changed')
        summary.update(user_history_counts_before=before, user_history_counts_after=after,
                       history_counts_preserved=True, deleted_expired=len(cleanup['deleted']),
                       retained_writing_history=len(cleanup['retained_writing_history']),
                       notice_count=db.scalar(select(func.count()).select_from(Scholarship)),
                       regular_scheduler_two_cycles_observed=False)
    write(root / 'db-sync.json', summary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--phase', choices=['capture', 'sync'], required=True)
    parser.add_argument('--since', default='2026-08-18')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    if args.phase == 'capture':
        if not 1 <= args.limit <= 100:
            raise ValueError('capture limit must be between 1 and 100')
        if args.offset < 0:
            raise ValueError('offset must be nonnegative')
        capture(root, date.fromisoformat(args.since), args.limit, args.offset)
    elif args.apply:
        if not 1 <= args.cycles <= 3:
            raise ValueError('sync cycles must be between 1 and 3')
        sync(root, args.cycles)
    else:
        raise ValueError('sync writes the development DB and requires --apply')


if __name__ == '__main__':
    main()
