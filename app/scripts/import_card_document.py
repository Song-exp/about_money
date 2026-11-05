"""Import an operator-downloaded K-pass PDF without making network requests."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from app.core.db import SessionLocal
from app.services.card_document_import import import_document, MAX_BYTES
from app.services.card_linked_documents import DOCUMENTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document-id', required=True, choices=DOCUMENTS)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--title', required=True)
    parser.add_argument('--revision', required=True, help='Declared revision, or explicitly unknown')
    parser.add_argument('--obtained-at', required=True, type=datetime.fromisoformat)
    parser.add_argument('--operator', required=True)
    parser.add_argument('--origin-url', required=True)
    args = parser.parse_args()
    if not args.file.is_file() or args.file.stat().st_size > MAX_BYTES:
        parser.error('A local PDF of at most 20 MiB is required')
    try:
        with SessionLocal() as db:
            result = import_document(db, args.document_id, args.file.read_bytes(), declared_title=args.title,
                declared_revision=args.revision, obtained_at=args.obtained_at, operator=args.operator,
                origin_url=args.origin_url)
            db.commit()
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
