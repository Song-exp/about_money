"""Retain validated query-scoped captures without approving store classification."""
import argparse
import json
from pathlib import Path
from app.core.db import SessionLocal
from app.services.issuer_merchant_capture import import_capture, capture_matches, merchant_archive_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    try:
        with SessionLocal() as db:
            root = merchant_archive_root()
            capture = import_capture(db,args.manifest,archive_root=root)
            result = capture_matches(db,capture,archive_root=root)
            db.commit()
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='matches'},ensure_ascii=False))


if __name__ == '__main__':
    main()
