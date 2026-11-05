"""Compare automatic layout vs single-block OCR on six visually checked sample pages."""
import argparse
import csv
import io
import json
from pathlib import Path
import re

from app.services import ocr
from app.core.config import settings

# Small diagnostic anchors transcribed from the saved page images, not a whole-corpus gold set.
SAMPLES = [
    (5, 1, 3, ['신청기간', '2026.10.31', '사례관리기관']),
    (9, 1, 1, ['고속도로', '500만원', '2026.9.14', '10.11']),
    (16, 1, 1, ['최종학년', '500만원', '300만원', '100만원']),
    (20, 1, 2, ['가족관계증명서', '3개월이내', '2026년도9월이후발급분', '성적증명서']),
    (24, 1, 1, ['소득분위가낮은자', '다자녀가구', '직전학기평균평점', '익월12일']),
    (881, 3, 1, ['2003년1월1일이후출생자', '1인150만원생활비', '10.30', '18:00', '대학원']),
]


def normalized(text):
    return re.sub(r'[^가-힣A-Za-z0-9]', '', text)


def run(root):
    results = []
    executable = ocr.executable_path()
    for notice, asset, page, anchors in SAMPLES:
        folder = root / f'notice-{notice}'
        base = folder / f'asset-{asset}-page-{page}'
        source = base.with_suffix('.png')
        result = ocr._run([executable, ocr._filesystem_path(source), 'stdout',
                           *ocr._language_args(executable), '-l', settings.ocr_languages, '--psm', '6', 'tsv'])
        groups = {}
        for row in csv.DictReader(io.StringIO(result.stdout.decode('utf-8')), delimiter='\t', quoting=csv.QUOTE_NONE):
            if row.get('text', '').strip() and float(row['conf']) >= 0:
                groups.setdefault(tuple(row[k] for k in ('page_num', 'block_num', 'par_num', 'line_num')), []).append(row)
        text = '\n'.join(' '.join(w['text'] for w in words) for words in groups.values())
        (folder / (base.name + '-psm6.txt')).write_text(text, encoding='utf-8')
        outputs = {'automatic': Path(str(base) + '-ocr.txt').read_text(encoding='utf-8'),
                   'single_block': text, 'native_pdf': Path(str(base) + '-native.txt').read_text(encoding='utf-8')}
        item = {'notice': notice, 'asset': asset, 'page': page, 'anchors': anchors,
                'anchor_matches': {mode: [anchor for anchor in anchors if normalized(anchor) in normalized(body)]
                                   for mode, body in outputs.items()},
                'characters': {mode: len(body) for mode, body in outputs.items()}}
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    (root / 'mode-comparison.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    run(parser.parse_args().input)
