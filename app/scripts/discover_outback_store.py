"""Cross-check one captured official Outback store against the Kakao SDK."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.scripts.discover_starbucks_store import review_candidate
from app.services.outback_store_identity import outback_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir', required=True)
    parser.add_argument('--official-id', required=True)
    parser.add_argument('--official-name', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(args.capture_dir)
    receipt = json.loads((root / 'receipt.json').read_text(encoding='utf-8'))
    filename = 'search.json' if urlparse(receipt['source_url']).path == '/store/storeJsonList.do' else 'locator.html'
    candidate = outback_candidate((root / filename).read_text(encoding='utf-8'), receipt,
        args.official_id, args.official_name, datetime.now(timezone.utc))
    review_candidate(candidate, args.output, args.apply)


if __name__ == '__main__':
    main()
