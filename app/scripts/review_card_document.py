"""Export a read-only review package; does not approve imported documents."""
import argparse
import json
from pathlib import Path
from app.core.db import SessionLocal
from app.services.card_document_review import document_review_pack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-id', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.import_id <= 0:
        parser.error('--import-id must be positive')
    try:
        with SessionLocal() as db:
            pack = document_review_pack(db, args.import_id)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'import_id': args.import_id, 'output': str(args.output),
        'review_evidence_ready': pack['review_evidence_ready'], 'issues': pack['issues'],
        'public_approval': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
