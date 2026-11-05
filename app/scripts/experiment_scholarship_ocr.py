"""Read-only OCR experiment on archived scholarship notices (no LLM or DB writes)."""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import time

from sqlalchemy import select
from app.core.db import SessionLocal
from app.core.config import settings
from app.models.scholarship import Scholarship
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services.consultation_scope import scope_clause
from app.services import ocr


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def run(output):
    from pypdf import PdfReader
    from PIL import Image, ImageDraw
    output.mkdir(parents=True, exist_ok=False)
    runtime = ocr.runtime_status()
    write(output / 'runtime.json', runtime)
    if not runtime['ocr_ready'] or not runtime['pdf_backend']:
        raise RuntimeError('OCR runtime is not ready; see runtime.json')
    results = []
    with SessionLocal() as db:
        rows = db.scalars(select(Scholarship).where(scope_clause('scholarship')).order_by(Scholarship.id)).all()
        for row in rows:
            folder = output / f'notice-{row.id}'
            folder.mkdir()
            attempt = db.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.content_key == row.content_key)
                                .order_by(SourceFetchAttempt.id.desc()).limit(1))
            notice = {'id': row.id, 'title': row.title, 'source_url': row.source_url,
                      'attempt_id': attempt.id if attempt else None, 'assets': [], 'pages': []}
            previews = []
            for index, item in enumerate(attempt.documents if attempt else []):
                if item.get('kind') == 'html':
                    continue
                doc = db.get(SourceDocument, item.get('document_id')) if item.get('document_id') else None
                asset = {'index': index, 'filename': item.get('filename'), 'old_error': item.get('error'),
                         'old_ocr_error': item.get('ocr_error'), 'old_issues': (item.get('structured') or {}).get('issues', []),
                         'sha256': hashlib.sha256(doc.content).hexdigest() if doc else None}
                notice['assets'].append(asset)
                if not doc:
                    asset['error'] = 'archived_bytes_missing'
                    continue
                content = doc.content
                is_pdf = content.startswith(b'%PDF')
                is_image = content.startswith((b'\x89PNG', b'\xff\xd8\xff', b'GIF87a', b'GIF89a'))
                if not is_pdf and not is_image:
                    asset['scope'] = 'native_document_not_ocr_target'
                    continue
                reader = PdfReader(io.BytesIO(content)) if is_pdf else None
                asset['page_count'] = len(reader.pages) if reader else 1
                for page in range(1, asset['page_count'] + 1):
                    started = time.monotonic()
                    native = reader.pages[page - 1].extract_text(extraction_mode='layout') or '' if reader else ''
                    blocks, issue = ocr.pdf_page_blocks(content, page) if reader else ocr.image_blocks(content)
                    text = '\n'.join(b['text'] for b in blocks)
                    low = [b for b in blocks if b['confidence'] < settings.ocr_min_confidence]
                    base = f'asset-{index}-page-{page}'
                    (folder / (base + '-ocr.txt')).write_text(text, encoding='utf-8')
                    (folder / (base + '-native.txt')).write_text(native, encoding='utf-8')
                    write(folder / (base + '-blocks.json'), blocks)
                    native_numbers = set(re.findall(r'\d[\d,.]*', native))
                    ocr_numbers = set(re.findall(r'\d[\d,.]*', text))
                    result = {'asset_index': index, 'page': page, 'filename': item.get('filename'),
                              'kind': 'pdf' if reader else 'image', 'issue': issue,
                              'elapsed_seconds': round(time.monotonic() - started, 2),
                              'native_chars': len(native), 'ocr_chars': len(text),
                              'korean_chars': len(re.findall('[가-힣]', text)), 'lines': len(blocks),
                              'low_confidence_lines': len(low),
                              'mean_line_confidence': round(sum(b['confidence'] for b in blocks) / len(blocks), 2) if blocks else None,
                              'low_confidence_examples': [{'text': b['text'], 'confidence': b['confidence']} for b in sorted(low, key=lambda b: b['confidence'])[:8]],
                              'native_number_tokens': sorted(native_numbers),
                              'missing_native_number_tokens': sorted(native_numbers - ocr_numbers),
                              'key_lines': [line for line in text.splitlines() if re.search(r'마감|접수|신청|만원|만 원|장학금|학점|소득|대상|2026', line)][:18]}
                    notice['pages'].append(result)
                    # Preserve every page image for visual verification; PDFs render at the same 200 dpi as OCR.
                    if reader:
                        import pypdfium2 as pdfium
                        with pdfium.PdfDocument(content) as pdf:
                            pdf_page = pdf[page - 1]
                            bitmap = None
                            try:
                                bitmap = pdf_page.render(scale=200 / 72)
                                preview = bitmap.to_pil().copy()
                            finally:
                                if bitmap is not None:
                                    bitmap.close()
                                pdf_page.close()
                    else:
                        preview = Image.open(io.BytesIO(content)).convert('RGB')
                    preview.save(folder / (base + '.png'))
                    thumb = preview.copy()
                    thumb.thumbnail((500, 650))
                    tile = Image.new('RGB', (520, 690), 'white')
                    tile.paste(thumb, (10, 30))
                    ImageDraw.Draw(tile).text((10, 8), f'asset {index} / page {page}', fill='black')
                    previews.append(tile)
                    print(f'notice={row.id} asset={index} page={page} chars={len(text)} issue={issue}', flush=True)
            if previews:
                columns = min(3, len(previews))
                sheet = Image.new('RGB', (520 * columns, 690 * ((len(previews) + columns - 1) // columns)), '#dddddd')
                for i, tile in enumerate(previews):
                    sheet.paste(tile, ((i % columns) * 520, (i // columns) * 690))
                sheet.save(folder / 'contact-sheet.jpg')
            write(folder / 'result.json', notice)
            results.append(notice)
            write(output / 'results.json', results)
    pages = [page for notice in results for page in notice['pages']]
    summary = {'notices': len(results), 'assets': sum(len(n['assets']) for n in results),
               'ocr_pages': len(pages), 'issue_counts': dict(Counter(p['issue'] or 'none' for p in pages)),
               'pages_with_text': sum(bool(p['ocr_chars']) for p in pages),
               'pages_with_korean': sum(bool(p['korean_chars']) for p in pages),
               'confidence_threshold': settings.ocr_min_confidence,
               'database_modified': False, 'llm_called': False,
               'scope': 'Archived scholarship PDF and standalone image pages; HWPX native text/forms excluded. '
                        'Confidence and native-number comparison are diagnostics, not human-gold accuracy.'}
    write(output / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
