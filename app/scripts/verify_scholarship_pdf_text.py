"""Replay the production PDF parser against archived PDFs, without LLM/DB writes."""
import argparse
import io
import json
from pathlib import Path
from collections import Counter

from pypdf import PdfReader
from sqlalchemy import select
from app.core.db import SessionLocal
from app.models.scholarship import Scholarship
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services.consultation_scope import scope_clause
from app.services.document_parser import parse_document
from app.scripts.compare_scholarship_ocr_modes import SAMPLES, normalized


def run(root):
    root.mkdir(parents=True, exist_ok=False)
    results = []
    with SessionLocal() as db:
        for notice in db.scalars(select(Scholarship).where(scope_clause('scholarship')).order_by(Scholarship.id)):
            attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == notice.content_key)
                                .order_by(SourceFetchAttempt.id.desc()).limit(1))
            for index, item in enumerate(attempt.documents if attempt else []):
                source = db.get(SourceDocument, item.get('document_id')) if item.get('document_id') else None
                if source is None or not source.content.startswith(b'%PDF'):
                    continue
                folder = root / f'notice-{notice.id}-asset-{index}'
                folder.mkdir()
                parsed = parse_document(source.content, item['filename'])
                # This exact text is the next-stage extract_structured input, not a live LLM response.
                (folder / 'llm-input-text.txt').write_text(parsed['extracted_text'], encoding='utf-8')
                (folder / 'parsed.json').write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')
                reader = PdfReader(io.BytesIO(source.content))
                pages = []
                for number, page in enumerate(reader.pages, 1):
                    native = page.extract_text(extraction_mode='layout') or ''
                    blocks = [b for b in parsed['blocks'] if b.get('page') == number]
                    pages.append({'page': number, 'native_chars': len(native), 'images': len(page.images),
                                  'parser_methods': dict(Counter(b.get('method') for b in blocks)),
                                  'native_text_preserved': any(b.get('method') == 'native' and b['text'] == native for b in blocks)
                                      if native.strip() else None})
                    (folder / f'page-{number}-native.txt').write_text(native, encoding='utf-8')
                samples = [{'page': p, 'anchors': anchors,
                            'native_matches': [a for a in anchors if normalized(a) in normalized(reader.pages[p-1].extract_text(extraction_mode='layout') or '')],
                            'pipeline_matches': [a for a in anchors if normalized(a) in normalized(parsed['extracted_text'])]}
                           for n, asset, p, anchors in SAMPLES if n == notice.id and asset == index]
                result = {'notice_id': notice.id, 'title': notice.title, 'filename': item['filename'],
                          'sha256': source.sha256, 'pages': pages, 'issues': parsed['issues'], 'samples': samples,
                          'reconciliation_counts': dict(Counter(x['reason'] for x in parsed['reconciliation'])),
                          'llm_called': False, 'database_modified': False}
                results.append(result)
                (root / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
