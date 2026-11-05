"""Run due official store observations without publishing stores or benefits."""
import argparse
import json
from pathlib import Path
from app.core.db import SessionLocal
from app.services.store_directory_collection import collect_store_directories


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = collect_store_directories(db, force=args.force)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
