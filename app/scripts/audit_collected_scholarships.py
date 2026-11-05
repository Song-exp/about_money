"""Validate archived scholarship data; --apply stores classifications after a SQLite backup."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from sqlalchemy import select
from app.core.db import SessionLocal, engine
from app.crawlers.base import RawPosting
from app.models.scholarship import Scholarship, PostingStatus
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services.consultation_scope import scope_clause
from app.services.posting_validation import validate_posting
from app.services.scholarship_history import record_revision
from app.crawlers.date_parser import KST


def run(output, apply=False):
    output.mkdir(parents=True, exist_ok=False)
    result = {'mode': 'apply' if apply else 'dry_run', 'source': 'existing_archives_not_live_refresh', 'rows': []}
    if apply:
        if engine.dialect.name != 'sqlite':
            raise ValueError('apply requires local SQLite for an automatic backup')
        backup = Path('data/backups') / ('before-document-validation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(engine.url.database) as src, sqlite3.connect(backup) as target:
            src.backup(target)
        result['backup'] = str(backup)
    with SessionLocal() as db:
        for row in db.scalars(select(Scholarship).where(scope_clause('scholarship')).order_by(Scholarship.id)):
            attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == row.content_key)
                                .order_by(SourceFetchAttempt.id.desc()).limit(1))
            documents = []
            for meta in attempt.documents if attempt else []:
                item = deepcopy(meta)
                stored = db.get(SourceDocument, item.get('document_id')) if item.get('document_id') else None
                if stored:
                    item['content'] = stored.content
                documents.append(item)
            raw = RawPosting(title=row.title, source_url=row.source_url, source_platform=row.source_platform,
                source_type=row.source_type, category=row.category, organization=row.organization,
                body_text=row.body_text, deadline_at=row.deadline_at, eligibility=deepcopy(row.eligibility),
                benefit=deepcopy(row.benefit), required_documents=deepcopy(row.required_documents),
                source_documents=documents)
            review = validate_posting(raw)
            # Bind the audit to the retained DB snapshot. It does not claim a fresh crawl.
            review['content_hash'] = row.content_hash
            review['archive_attempt_id'] = attempt.id if attempt else None
            result['rows'].append({'id': row.id, 'title': row.title, 'validation': review})
            if apply:
                record_revision(db, row)
                row.eligibility = {**row.eligibility, 'document_validation_required': True, 'document_validation': review}
                deadline = row.deadline_at.replace(tzinfo=KST) if row.deadline_at and row.deadline_at.tzinfo is None else row.deadline_at
                row.status = (PostingStatus.NEEDS_REVIEW if review['status'] != 'passed' or not deadline else
                              PostingStatus.CLOSED if deadline < datetime.now(KST) else PostingStatus.OPEN)
                record_revision(db, row)
            print(f"id={row.id} status={review['status']} reasons={[x['code'] for x in review['issues']]}", flush=True)
        if apply:
            db.commit()
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(args.output, args.apply)
