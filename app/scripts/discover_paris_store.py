"""Review a captured official Paris Baguette identity against the Kakao SDK."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.scripts.discover_starbucks_store import review_candidate
from app.services.paris_store_identity import paris_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir', required=True)
    parser.add_argument('--official-id', required=True)
    parser.add_argument('--official-name', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(args.capture_dir)
    candidate = paris_candidate((root / 'search.html').read_text(encoding='utf-8'),
        json.loads((root / 'receipt.json').read_text(encoding='utf-8')),
        args.official_id, args.official_name, datetime.now(timezone.utc),
        area_raw=(root / 'areas.json').read_text(encoding='utf-8') if (root / 'areas.json').exists() else None)
    review_candidate(candidate, args.output, args.apply)


if __name__ == '__main__':
    main()
